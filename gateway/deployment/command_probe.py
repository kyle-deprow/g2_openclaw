"""Probe supervisor wake commands inside the managed Codex sandbox.

The probe extracts the first command from each supervisor wake-message constant
and executes that command through the embedded Codex app-server with
``sandbox_mode="workspace-write"`` from the PM's default model-workspaces cwd.
A real deployment must return zero.  The guard-suite's isolated HOME has no
autoresearch state fixture, so this module also accepts exactly gateway-cli's
state-path validation failure: exit code 2 with the ``autoresearch-next`` usage
and the matching ``STATE_PATH`` file ``does not exist`` diagnostic, but only
after an independent unsandboxed check confirms that the state path is absent
and its parent is traversable.  This rejects Click's identical EACCES
masquerade.  Any other nonzero result is a deployment failure.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

SUPERVISOR_MESSAGE_NAMES: tuple[str, ...] = (
    "WAKE_MESSAGE",
    "FINALIZED_MEMORY_WAKE_MESSAGE",
    "RECOVERY_MESSAGE",
    "MISSING_VERIFICATION_ARTIFACT_RECOVERY_MESSAGE",
)
COMMAND_CONTRACT_PREFIX = "First run exactly: "
_COMMAND_PATTERN = re.compile(
    re.escape(COMMAND_CONTRACT_PREFIX) + r"(?P<command>(?:(?:cd\s+\S+\s+&&\s+)?\S+\s+"
    r"autoresearch-next\s+\S+?\.json))\.\s"
)
_STATE_PATH_MISSING_EXIT_CODE = 2
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
_PM_WORKING_DIRECTORY = Path(".openclaw/autoresearch/model-workspaces")
_EMBEDDED_CODEX_SUFFIX = (
    "node_modules",
    "@openclaw",
    "codex",
    "node_modules",
    "@openai",
    "codex",
    "bin",
    "codex.js",
)


class CommandProbeError(RuntimeError):
    """Raised when a supervisor command contract cannot be safely probed."""


def _extract_message_command(name: str, message: object) -> str:
    if not isinstance(message, str):
        raise CommandProbeError(f"{name} is not a wake-message string")
    match = _COMMAND_PATTERN.search(message)
    if match is None:
        raise CommandProbeError(
            f"{name} is missing the required '{COMMAND_CONTRACT_PREFIX}' command pattern"
        )
    command = match.group("command")
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise CommandProbeError(f"{name} has an invalid commanded invocation: {exc}") from exc
    if not tokens or Path(tokens[-1]).suffix != ".json":
        raise CommandProbeError(f"{name} has an invalid autoresearch state-path command")
    command_start = 3 if tokens[0] == "cd" else 0
    if tokens[0] == "cd" and (len(tokens) != 6 or tokens[2] != "&&"):
        raise CommandProbeError(f"{name} has an unsupported shell command contract")
    if len(tokens) - command_start != 3 or Path(tokens[command_start]).name != "gateway-cli":
        raise CommandProbeError(f"{name} has an unsupported gateway-cli command contract")
    if tokens[command_start + 1] != "autoresearch-next":
        raise CommandProbeError(f"{name} has an unsupported gateway-cli command contract")
    return command


def extract_commanded_invocations() -> tuple[str, ...]:
    """Extract the four exact invocations published in supervisor wake messages."""

    import gateway.autoresearch_supervisor as supervisor

    commands: list[str] = []
    for name in SUPERVISOR_MESSAGE_NAMES:
        commands.append(_extract_message_command(name, getattr(supervisor, name, None)))
    return tuple(commands)


def resolve_embedded_codex_binary(cli_path: Path) -> tuple[str, str]:
    """Resolve the Node command and embedded Codex CLI like the doctor step."""

    if tuple(cli_path.parts[-len(_EMBEDDED_CODEX_SUFFIX) :]) != _EMBEDDED_CODEX_SUFFIX:
        raise CommandProbeError(
            "embedded Codex CLI must be bin/codex.js under "
            "@openclaw/codex/node_modules/@openai/codex"
        )
    if not cli_path.is_file():
        raise CommandProbeError(f"embedded Codex CLI not found at {cli_path}")
    return "node", str(cli_path)


def _sandbox_command(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise CommandProbeError(f"invalid commanded invocation: {exc}") from exc
    if "&&" in tokens:
        if len(tokens) < 4 or tokens[0] != "cd" or tokens[2] != "&&":
            raise CommandProbeError(f"unsupported shell command contract: {command}")
        return ["bash", "-lc", command]
    return tokens


def _state_path(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise CommandProbeError(f"invalid commanded invocation: {exc}") from exc
    if not tokens:
        raise CommandProbeError("commanded invocation is empty")
    return tokens[-1]


def _is_gateway_cli_missing_state_signature(
    command: str, completed: subprocess.CompletedProcess[str]
) -> bool:
    if completed.returncode != _STATE_PATH_MISSING_EXIT_CODE:
        return False
    stderr = _ANSI_ESCAPE_PATTERN.sub("", completed.stderr).replace("│", " ")
    state_path = re.escape(_state_path(command))
    return (
        completed.stdout == ""
        and "Usage: gateway-cli autoresearch-next [OPTIONS] STATE_PATH" in stderr
        and re.search(
            rf"Invalid value for 'STATE_PATH': File\s+['\"]?{state_path}['\"]?\s+does not exist\.",
            stderr,
        )
        is not None
    )


def _existing_parent_is_traversable(parent: Path) -> bool:
    try:
        parent_stat = os.stat(parent)
    except OSError:
        return False
    if not stat.S_ISDIR(parent_stat.st_mode):
        return False

    mode = stat.S_IMODE(parent_stat.st_mode)
    effective_uid = os.geteuid()
    effective_gid = os.getegid()
    execute_bit: int
    if parent_stat.st_uid == effective_uid:
        execute_bit = stat.S_IXUSR
    elif parent_stat.st_gid == effective_gid or parent_stat.st_gid in os.getgroups():
        execute_bit = stat.S_IXGRP
    else:
        execute_bit = stat.S_IXOTH
    return bool(mode & execute_bit) and os.access(parent, os.X_OK)


def _state_file_is_missing_and_parent_traversable(state_path: str) -> bool:
    try:
        if os.path.lexists(state_path):
            return False
    except OSError:
        return False
    parent = Path(state_path).parent
    while True:
        try:
            if os.path.lexists(parent):
                return _existing_parent_is_traversable(parent)
        except OSError:
            return False
        next_parent = parent.parent
        if next_parent == parent:
            return False
        parent = next_parent


def _stderr_tail(stderr: str, *, line_count: int = 20) -> str:
    lines = stderr.strip().splitlines()
    return "\n".join(lines[-line_count:]) if lines else "<empty>"


def run_probe(codex_home: Path, embedded_codex_cli: Path) -> int:
    """Run all extracted commands and return a deployment-compatible status."""

    commands = extract_commanded_invocations()
    node_binary, cli = resolve_embedded_codex_binary(embedded_codex_cli)
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    failures: list[tuple[str, subprocess.CompletedProcess[str], str | None]] = []

    working_directory = Path.home() / _PM_WORKING_DIRECTORY
    # The runtime treats the model-workspaces dir as create-on-demand (it is the
    # sandbox defaultWorkspaceDir); the probe provisions it the same way.
    working_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for command in commands:
        completed = subprocess.run(
            [
                node_binary,
                cli,
                "sandbox",
                "-c",
                'sandbox_mode="workspace-write"',
                *_sandbox_command(command),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            cwd=working_directory,
        )
        if completed.returncode == 0:
            print(f"command-contract probe passed: {command}")
        elif _is_gateway_cli_missing_state_signature(command, completed):
            if _state_file_is_missing_and_parent_traversable(_state_path(command)):
                print(f"command-contract probe accepted state-file-missing failure: {command}")
            else:
                failures.append(
                    (
                        command,
                        completed,
                        "state-file-missing signature rejected: independent filesystem check "
                        "found the state path present or its parent untraversable",
                    )
                )
        else:
            failures.append((command, completed, None))

    if failures:
        for command, completed, rejection in failures:
            print("command-contract probe failed:", file=sys.stderr)
            print(f"  command: {command}", file=sys.stderr)
            print(f"  exit code: {completed.returncode}", file=sys.stderr)
            if rejection is not None:
                print(f"  rejection: {rejection}", file=sys.stderr)
            print("  stderr tail:", file=sys.stderr)
            print(_stderr_tail(completed.stderr), file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("codex_home")
    probe_parser.add_argument("embedded_codex_cli")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the command-contract probe."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "probe":
        try:
            return run_probe(Path(args.codex_home), Path(args.embedded_codex_cli))
        except (CommandProbeError, OSError) as exc:
            print(f"command-contract probe failed: {exc}", file=sys.stderr)
            return 1
    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
