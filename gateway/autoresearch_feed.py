"""Read-only autoresearch status and assistant-feed snapshots for the G2 display."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosed

from gateway.autoresearch.constants import DEFAULT_AUTORESEARCH_STATE_PATH
from gateway.autoresearch_checkpoint import SupervisorCheckpoint
from gateway.autoresearch_shared import (
    AUTORESEARCH_OWNER_AGENT_ID,
    AUTORESEARCH_OWNER_SESSION_KEY,
)
from gateway.session_history import read_history
from gateway.task_status import read_task_status

logger = logging.getLogger(__name__)

FEED_LIMIT = 10
FEED_TEXT_MAX = 500
POLL_INTERVAL_SECONDS = 5.0
HISTORY_READ_LIMIT = 40
LIVENESS_THRESHOLD_SECONDS = 300.0

DEFAULT_CHECKPOINT_PATH: Path = DEFAULT_AUTORESEARCH_STATE_PATH.parent / "owner-recovery.json"


@dataclass(frozen=True, slots=True)
class FeedEntry:
    role: str
    text: str
    ts: int


@dataclass(frozen=True, slots=True)
class AutoresearchSnapshot:
    running: bool
    header_ok: bool
    phase: str
    iteration: int
    suspended: bool
    campaign_review_required: bool
    supervisor_outcome: str | None
    supervisor_detail: str | None
    last_cycle_at_ms: int | None
    task_headline: str | None
    feed: tuple[FeedEntry, ...] | None

    @property
    def header_key(self) -> tuple[object, ...]:
        return (
            self.running,
            self.phase,
            self.iteration,
            self.suspended,
            self.campaign_review_required,
            self.supervisor_outcome,
            self.supervisor_detail,
            self.last_cycle_at_ms,
            self.task_headline,
        )


def read_snapshot(
    *,
    state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
    checkpoint_path: Path = DEFAULT_CHECKPOINT_PATH,
    session_key: str = AUTORESEARCH_OWNER_SESSION_KEY,
    agent_id: str = AUTORESEARCH_OWNER_AGENT_ID,
    base_path: Path | None = None,
    now: float | None = None,
) -> AutoresearchSnapshot:
    """Read the autoresearch display state, degrading each source independently."""
    header_ok = True
    phase = "not running"
    iteration = 0
    suspended = False
    campaign_review_required = False

    try:
        raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except Exception as exc:
        header_ok = False
        logger.warning("Failed to read autoresearch state %s: %s", state_path, exc)
    else:
        if not isinstance(raw_state, dict):
            header_ok = False
            logger.warning("Autoresearch state is not a JSON object: %s", state_path)
        else:
            raw_phase = raw_state.get("phase", "unknown")
            phase = raw_phase if isinstance(raw_phase, str) else "unknown"
            raw_iteration = raw_state.get("iteration", 0)
            if isinstance(raw_iteration, int) and not isinstance(raw_iteration, bool):
                iteration = raw_iteration
            raw_suspended = raw_state.get("suspended", False)
            suspended = raw_suspended if isinstance(raw_suspended, bool) else False
            raw_campaign_review_required = raw_state.get("campaign_review_required", False)
            campaign_review_required = (
                raw_campaign_review_required
                if isinstance(raw_campaign_review_required, bool)
                else False
            )

    supervisor_outcome: str | None = None
    supervisor_detail: str | None = None
    last_cycle_at_ms: int | None = None
    checkpoint: SupervisorCheckpoint | None = None
    try:
        checkpoint = SupervisorCheckpoint.load(checkpoint_path)
    except FileNotFoundError:
        pass
    except Exception:
        header_ok = False
        logger.debug(
            "Failed to read autoresearch checkpoint %s",
            checkpoint_path,
            exc_info=True,
        )

    if checkpoint is not None:
        supervisor_outcome = checkpoint.last_cycle_outcome
        if checkpoint.last_cycle_detail is not None:
            supervisor_detail = checkpoint.last_cycle_detail[:160]
        if checkpoint.last_cycle_at is not None:
            last_cycle_at_ms = int(checkpoint.last_cycle_at * 1000)

    running = (
        checkpoint is not None
        and checkpoint.last_cycle_at is not None
        and (time.time() if now is None else now) - checkpoint.last_cycle_at
        <= LIVENESS_THRESHOLD_SECONDS
    )

    feed: tuple[FeedEntry, ...] | None = None
    try:
        history = read_history(
            session_key=session_key,
            agent_id=agent_id,
            limit=HISTORY_READ_LIMIT,
            base_path=base_path,
        )
        assistant_entries = [entry for entry in history if entry.role == "assistant"]
        feed = tuple(
            FeedEntry(role=entry.role, text=entry.text[:FEED_TEXT_MAX], ts=entry.ts)
            for entry in assistant_entries[-FEED_LIMIT:]
        )
    except Exception:
        logger.debug("Failed to read autoresearch history", exc_info=True)

    task_headline: str | None = None
    try:
        task_info = read_task_status(session_key, agent_id=agent_id)
        if task_info is not None:
            task_headline = f"[{task_info.status.upper()}] {task_info.description}"[:120]
    except Exception:
        logger.debug("Failed to read autoresearch task status", exc_info=True)

    return AutoresearchSnapshot(
        running=running,
        header_ok=header_ok,
        phase=phase,
        iteration=iteration,
        suspended=suspended,
        campaign_review_required=campaign_review_required,
        supervisor_outcome=supervisor_outcome,
        supervisor_detail=supervisor_detail,
        last_cycle_at_ms=last_cycle_at_ms,
        task_headline=task_headline,
        feed=feed,
    )


def build_status_frame(snapshot: AutoresearchSnapshot) -> dict[str, Any]:
    """Build an outbound autoresearch status frame."""
    frame: dict[str, Any] = {
        "type": "autoresearch_status",
        "running": snapshot.running,
        "phase": snapshot.phase,
        "iteration": snapshot.iteration,
        "suspended": snapshot.suspended,
        "campaignReviewRequired": snapshot.campaign_review_required,
    }
    optional_fields = {
        "supervisorOutcome": snapshot.supervisor_outcome,
        "supervisorDetail": snapshot.supervisor_detail,
        "lastCycleAt": snapshot.last_cycle_at_ms,
        "taskHeadline": snapshot.task_headline,
    }
    frame.update({key: value for key, value in optional_fields.items() if value is not None})
    return frame


def build_feed_frame(snapshot: AutoresearchSnapshot) -> dict[str, Any]:
    """Build an outbound autoresearch assistant-feed frame."""
    entries = snapshot.feed if snapshot.feed is not None else ()
    return {
        "type": "autoresearch_feed",
        "entries": [{"role": entry.role, "text": entry.text, "ts": entry.ts} for entry in entries],
    }


class AutoresearchFeedPublisher:
    """Push changed autoresearch snapshots to one connected G2 client."""

    def __init__(
        self,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        poll_interval: float = POLL_INTERVAL_SECONDS,
        read: Callable[[], AutoresearchSnapshot] = read_snapshot,
    ) -> None:
        self._send = send
        self._poll_interval = poll_interval
        self._read = read
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start polling, if the publisher is not already running."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop polling and wait for the publisher task to finish."""
        if self._task is None:
            return
        task = self._task
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _run(self) -> None:
        last_header: tuple[object, ...] | None = None
        last_feed: tuple[FeedEntry, ...] | None = None

        while True:
            try:
                snapshot = await asyncio.to_thread(self._read)
                if snapshot.header_ok:
                    header_key = snapshot.header_key
                    if last_header is None or header_key != last_header:
                        await self._send(build_status_frame(snapshot))
                        last_header = header_key
                if snapshot.feed is not None and (last_feed is None or snapshot.feed != last_feed):
                    await self._send(build_feed_frame(snapshot))
                    last_feed = snapshot.feed
            except ConnectionClosed:
                return
            except Exception as exc:
                logger.warning("Autoresearch feed publisher iteration failed: %s", exc)

            await asyncio.sleep(self._poll_interval)
