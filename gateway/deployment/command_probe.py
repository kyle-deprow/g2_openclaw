"""Probe the read-only research status command in the managed Codex sandbox.

This check exercises a command boundary, not an agent/model turn.  It uses the
installed Codex CLI's named, command-only permission profile and a private
missing-root fixture.  The replacement status command must return an explicit
unavailable frame without creating a database, lock, or root directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

COMMAND_PROFILE = "probe"
COMMAND_TIMEOUT_SECONDS = 20.0
MAX_OUTPUT_CHARS = 8_000
_COMMAND_PROFILE_CONFIG = """[permissions.probe.filesystem]
"/" = "read"

[permissions.probe.network]
enabled = false
"""
_COMMAND_PROGRAM = "gateway-cli"
_COMMAND_NAME = "research-status"
_STATUS_FIELDS = frozenset(
    {
        "type",
        "hypothesisId",
        "hypothesisState",
        "attemptId",
        "attemptState",
        "stage",
        "lastAstraDecision",
        "campaignStatus",
        "boundaryFailure",
        "lastEventAt",
        "ownerState",
        "updatedAt",
        "available",
        "unavailableReason",
    }
)


class CommandProbeError(RuntimeError):
    """Raised when the status command contract cannot be safely probed."""


def extract_commanded_invocations(probe_root: Path | None = None) -> tuple[str, ...]:
    """Return the one replacement invocation used by deployment checks.

    ``probe_root`` is deliberately supplied by :func:`run_probe` from a
    private temporary directory.  The optional value keeps this helper useful
    to static callers without reintroducing a shared or world-writable path.
    """
    root = probe_root or Path("<private-probe-root>")
    return (f"{_COMMAND_PROGRAM} {_COMMAND_NAME} --root {shlex.quote(str(root))}",)


def resolve_embedded_codex_binary(
    package_root: Path,
    cli_path: Path,
) -> tuple[str, str]:
    """Resolve the Node command and CLI under the verified package root."""
    expected_cli_path = package_root / "bin/codex.js"
    if cli_path != expected_cli_path:
        raise CommandProbeError(
            f"embedded Codex CLI {cli_path} does not match verified package root {package_root}"
        )
    if not cli_path.is_file():
        raise CommandProbeError(f"embedded Codex CLI not found at {cli_path}")
    return "node", str(cli_path)


def _sandbox_command(command: str, probe_root: Path | None = None) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise CommandProbeError(f"invalid commanded invocation: {exc}") from exc
    root = probe_root or Path("<private-probe-root>")
    expected = [_COMMAND_PROGRAM, _COMMAND_NAME, "--root", str(root)]
    if tokens != expected:
        raise CommandProbeError("unsupported research status command contract")
    return tokens


def _bounded_text(value: str, *, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "…<bounded>"


def _stderr_tail(stderr: str, *, line_count: int = 20) -> str:
    lines = _bounded_text(stderr).strip().splitlines()
    return "\n".join(lines[-line_count:]) if lines else "<empty>"


def _validate_unavailable_frame(stdout: str) -> str | None:
    """Validate the exact structured unavailable frame emitted by the CLI."""
    try:
        frame = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return f"status command did not emit JSON: {exc}"
    if not isinstance(frame, Mapping):
        return "status command JSON result is not an object"
    if set(frame) != _STATUS_FIELDS:
        return "status command returned an unexpected status frame shape"
    if frame.get("type") != "autoresearch_status":
        return "status command returned the wrong frame type"
    if frame.get("available") is not False:
        return "status command did not report an unavailable result"
    reason = frame.get("unavailableReason")
    if not isinstance(reason, str) or not reason.strip():
        return "status command unavailableReason is empty"
    if frame.get("stage") != "idle":
        return "unavailable status did not use the idle stage"
    return None


def _creation_error(probe_root: Path) -> str | None:
    if probe_root.exists():
        return "status command created the private missing root"
    if any(
        path.exists()
        for path in (
            probe_root / "state.sqlite3",
            probe_root / "state.sqlite3-wal",
            probe_root / "state.sqlite3-shm",
            probe_root / "owner.lock",
        )
    ):
        return "status command created research state files"
    return None


def _provision_command_profile(parent: Path) -> Path:
    """Create the probe-only Codex home without touching production config."""
    codex_home = parent / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(_COMMAND_PROFILE_CONFIG, encoding="utf-8")
    return codex_home


def run_probe(
    codex_home: Path,
    verified_package_root: Path,
    embedded_codex_cli: Path,
) -> int:
    """Run the replacement status command and return a deployment status."""
    node_binary, cli = resolve_embedded_codex_binary(verified_package_root, embedded_codex_cli)
    failures: list[tuple[str, str, str]] = []

    with tempfile.TemporaryDirectory(prefix="g2-research-status-probe-") as temp_parent:
        private_parent = Path(temp_parent)
        probe_root = private_parent / "missing-root"
        isolated_codex_home = _provision_command_profile(private_parent)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(isolated_codex_home)
        environment.pop("NODE_OPTIONS", None)
        commands = extract_commanded_invocations(probe_root)
        for command in commands:
            reason: str | None = None
            try:
                completed = subprocess.run(
                    [
                        node_binary,
                        cli,
                        "sandbox",
                        "-P",
                        COMMAND_PROFILE,
                        "-C",
                        str(isolated_codex_home),
                        "--",
                        *_sandbox_command(command, probe_root),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                    cwd=isolated_codex_home,
                    timeout=COMMAND_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                reason = "timed out"
                creation_error = _creation_error(probe_root)
                if creation_error is not None:
                    reason = f"{reason}; {creation_error}"
                failures.append((command, reason, _bounded_text(str(exc))))
                continue
            except OSError as exc:
                reason = "could not execute"
                creation_error = _creation_error(probe_root)
                if creation_error is not None:
                    reason = f"{reason}; {creation_error}"
                failures.append((command, reason, _bounded_text(str(exc))))
                continue

            if completed.returncode != 0:
                reason = (
                    f"exit code {completed.returncode}; stderr={_stderr_tail(completed.stderr)}"
                )
                creation_error = _creation_error(probe_root)
                if creation_error is not None:
                    reason = f"{reason}; {creation_error}"
            else:
                reason = _validate_unavailable_frame(completed.stdout.strip())
                if reason is None:
                    reason = _creation_error(probe_root)
            if reason is None:
                print(f"command-contract probe passed: {command}")
            else:
                failures.append((command, reason, _stderr_tail(completed.stderr)))

    for command, reason, stderr in failures:
        print("command-contract probe failed:", file=sys.stderr)
        print(f"  command: {command}", file=sys.stderr)
        print(f"  reason: {reason}", file=sys.stderr)
        if stderr and stderr != "<empty>":
            print("  stderr tail:", file=sys.stderr)
            print(stderr, file=sys.stderr)
    return 1 if failures else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("codex_home", type=Path)
    probe_parser.add_argument("verified_package_root", type=Path)
    probe_parser.add_argument("embedded_codex_cli", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return run_probe(args.codex_home, args.verified_package_root, args.embedded_codex_cli)
    except (CommandProbeError, OSError) as exc:
        print(f"command-contract probe failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
