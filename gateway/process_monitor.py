"""Gateway-side Copilot process monitor.

Polls for Copilot CLI sessions working on target repos and auto-notifies
OpenClaw when they exit.

Runs as a background asyncio task inside the gateway server. Notifications
go through the gateway's existing OpenClaw WebSocket client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gateway.copilot_sessions import CopilotSessionInfo, list_copilot_sessions

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 30  # seconds
_QUANTIPY_DIR = str(Path.home() / "repos" / "quantipy")


@dataclass
class TrackedProcess:
    """A Copilot process we're monitoring."""

    pid: int
    session_id: str
    cwd: str
    summary: str


@dataclass
class DeathReport:
    """Report generated when a tracked Copilot process exits."""

    pid: int
    cwd: str
    summary: str
    recent_commits: str
    sanity_output: str
    has_uncommitted: bool


class CopilotProcessMonitor:
    """Watches Copilot processes and notifies when they die.

    Usage::

        monitor = CopilotProcessMonitor(notify_callback=my_async_fn)
        task = asyncio.create_task(monitor.run())
        # ...
        monitor.stop()
        await task
    """

    def __init__(
        self,
        notify_callback: object,  # async callable(str) -> None
        target_dirs: list[str] | None = None,
    ) -> None:
        self._notify = notify_callback
        self._target_dirs = target_dirs or [_QUANTIPY_DIR]
        self._tracked: dict[int, TrackedProcess] = {}
        self._running = False

    async def run(self) -> None:
        """Main polling loop. Runs until stop() is called."""
        self._running = True
        logger.info("Process monitor started (targets=%s)", self._target_dirs)
        while self._running:
            try:
                await self._poll()
            except Exception:
                logger.exception("Process monitor poll error")
            await asyncio.sleep(_POLL_INTERVAL)

    def stop(self) -> None:
        """Signal the monitor to stop."""
        self._running = False

    @property
    def tracked_pids(self) -> list[int]:
        """Currently tracked PIDs."""
        return list(self._tracked)

    async def _poll(self) -> None:
        """Single poll iteration: discover new, detect dead."""
        running = list_copilot_sessions(running_only=True)
        running_pids = {s.pid for s in running}

        # Discover new target-repo Copilot processes
        for session in running:
            if session.pid not in self._tracked and self._is_target(session):
                self._tracked[session.pid] = TrackedProcess(
                    pid=session.pid,
                    session_id=session.session_id,
                    cwd=session.cwd,
                    summary=session.summary,
                )
                logger.info(
                    "Tracking Copilot PID %d (cwd=%s, summary=%s)",
                    session.pid,
                    session.cwd,
                    session.summary[:80],
                )

        # Check for dead processes
        dead_pids = [pid for pid in self._tracked if pid not in running_pids]
        for pid in dead_pids:
            tp = self._tracked.pop(pid)
            logger.info("Copilot PID %d exited — building death report", pid)
            report = await asyncio.to_thread(self._build_report, tp)
            message = self._format_message(report)
            try:
                await self._notify(message)  # type: ignore[operator]
                logger.info("Notified OpenClaw about PID %d exit", pid)
            except Exception:
                logger.exception("Failed to notify OpenClaw about PID %d", pid)

    def _is_target(self, session: CopilotSessionInfo) -> bool:
        """Check if a session is working on one of our target repos."""
        cwd = session.cwd or ""
        return any(target in cwd for target in self._target_dirs)

    def _build_report(self, tp: TrackedProcess) -> DeathReport:
        """Build a death report for a dead Copilot process (runs in thread)."""
        recent_commits = self._git_log(tp.cwd)
        sanity_output = self._check_notebook_sanity(tp.cwd)
        has_uncommitted = self._has_dirty_tree(tp.cwd)

        return DeathReport(
            pid=tp.pid,
            cwd=tp.cwd,
            summary=tp.summary,
            recent_commits=recent_commits,
            sanity_output=sanity_output,
            has_uncommitted=has_uncommitted,
        )

    def _format_message(self, report: DeathReport) -> str:
        """Format a death report as a message for OpenClaw."""
        parts: list[str] = []

        if report.has_uncommitted:
            parts.append(f"[TASK:failed] Copilot PID {report.pid} exited with uncommitted changes.")
        else:
            parts.append(f"[TASK:complete] Copilot PID {report.pid} has exited.")

        if report.summary:
            parts.append(f"Task: {report.summary}")

        if report.recent_commits:
            parts.append(f"Recent commits:\n{report.recent_commits}")

        if report.sanity_output:
            parts.append(f"Notebook sanity output:\n{report.sanity_output}")

        if report.has_uncommitted:
            parts.append(
                "WARNING: Dirty tree detected — Copilot may have died mid-work. "
                "Check git status and decide whether to commit or discard."
            )

        parts.append("Continue autoresearch — evaluate results and proceed to the next phase.")

        return "\n\n".join(parts)

    @staticmethod
    def _git_log(cwd: str, count: int = 5) -> str:
        """Get recent git log."""
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", f"-{count}"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    @staticmethod
    def _has_dirty_tree(cwd: str) -> bool:
        """Check if git working tree has uncommitted changes."""
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return False
            # Filter to only modified (not untracked) files
            for line in result.stdout.strip().splitlines():
                if line and not line.startswith("??"):
                    return True
            return False
        except Exception:
            return False

    @staticmethod
    def _check_notebook_sanity(cwd: str) -> str:
        """Extract sanity check output from the most recent experiment notebook."""
        notebooks_dir = Path(cwd) / "notebooks" / "experiments"
        if not notebooks_dir.is_dir():
            return ""

        # Find the most recently modified notebook
        notebooks = sorted(
            notebooks_dir.glob("*.ipynb"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not notebooks:
            return ""

        try:
            nb = json.loads(notebooks[0].read_text(encoding="utf-8"))
        except Exception:
            return ""

        # Look for sanity check outputs
        for cell in nb.get("cells", []):
            if cell.get("cell_type") != "code":
                continue
            for out in cell.get("outputs", []):
                text = "".join(out.get("text", []))
                if "SANITY CHECK" in text or "BUG" in text or "Strategy Evaluation" in text:
                    # Return last 600 chars of this output
                    return text[-600:]

        return ""
