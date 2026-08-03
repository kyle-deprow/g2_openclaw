"""Pinned OpenClaw and Codex runtime checks used by the push script."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REQUIRED_OPENCLAW_VERSION = "2026.7.1-2"
REQUIRED_CODEX_PLUGIN_VERSION = "2026.7.1-1"
REQUIRED_CODEX_APP_SERVER_VERSION = "0.144.3"


def expand_user_path(path: str, home: str) -> str:
    """Expand the two literal path forms accepted by the bash helper."""

    if path == "~":
        return home
    if path.startswith("~/"):
        return f"{home}/{path[2:]}"
    return path


def resolve_openclaw_bin(
    openclaw_bin: str | None = None,
    *,
    home: str | None = None,
    path: str | None = None,
) -> str:
    """Resolve an executable using the push script's exact candidate order."""

    resolved_home = os.environ.get("HOME", "") if home is None else home
    resolved_path = os.environ.get("PATH", "") if path is None else path
    explicit = openclaw_bin if openclaw_bin else None
    if explicit is not None:
        candidates = [expand_user_path(explicit, resolved_home)]
    else:
        candidates = [
            f"{resolved_home}/.local/share/pnpm/openclaw",
            f"{resolved_home}/.local/bin/openclaw",
        ]
        candidates.extend(
            f"{path_entry}/openclaw" for path_entry in resolved_path.split(":") if path_entry
        )

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        candidate_path = Path(candidate)
        if candidate_path.is_file() and os.access(candidate_path, os.X_OK):
            return candidate

    if explicit is not None:
        expanded = expand_user_path(explicit, resolved_home)
        raise RuntimeError(
            f"ERROR: OPENCLAW_BIN points to a missing or non-executable path: {expanded}"
        )
    raise RuntimeError(
        "ERROR: OpenClaw executable not found. Checked "
        f"{resolved_home}/.local/share/pnpm/openclaw, "
        f"{resolved_home}/.local/bin/openclaw, and PATH entries."
    )


def _openclaw_environment(config_path: str, state_dir: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "OPENCLAW_HOME",
        "OPENCLAW_PUSH_HOME",
        "OPENCLAW_STATE_DIR",
        "OPENCLAW_CONFIG_PATH",
        "NODE_OPTIONS",
    ):
        environment.pop(name, None)
    environment["OPENCLAW_STATE_DIR"] = state_dir
    environment["OPENCLAW_CONFIG_PATH"] = config_path
    return environment


def _run_openclaw(
    executable: str,
    config_path: str,
    state_dir: str,
    arguments: Sequence[str],
    *,
    merge_stderr: bool,
) -> subprocess.CompletedProcess[str]:
    if merge_stderr:
        return subprocess.run(
            [executable, *arguments],
            check=False,
            stdout=subprocess.PIPE,
            text=True,
            env=_openclaw_environment(config_path, state_dir),
            stderr=subprocess.STDOUT,
        )
    return subprocess.run(
        [executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=_openclaw_environment(config_path, state_dir),
    )


def _version_from_line(version_line: str) -> str | None:
    match = re.search(
        r"(?:^|(?<=[ \t\n\v\f\r]))(\d+\.\d+\.\d+[^ \t\n\v\f\r]*)(?=[ \t\n\v\f\r]|$)",
        version_line,
    )
    return None if match is None else match.group(1)


def require_openclaw_supported(
    executable: str,
    config_path: str,
    state_dir: str,
) -> str:
    """Run the exact OpenClaw version check and return its parsed version."""

    completed = _run_openclaw(executable, config_path, state_dir, ("--version",), merge_stderr=True)
    if completed.returncode != 0:
        raise RuntimeError(f"ERROR: OpenClaw version check failed for {executable}")
    version_line = completed.stdout.split("\n", 1)[0]
    version = _version_from_line(version_line)
    if version is None:
        raise RuntimeError(
            f"ERROR: Could not parse OpenClaw version from {executable}: "
            f"{version_line or '<empty>'}"
        )
    if version != REQUIRED_OPENCLAW_VERSION:
        raise RuntimeError(
            f"ERROR: OpenClaw {version} at {executable} is unsupported; "
            f"need exactly {REQUIRED_OPENCLAW_VERSION}."
        )
    return version


def _jq_raw(value: object, *, null_as_empty: bool = False) -> str:
    if value is None:
        return "" if null_as_empty else "null"
    if isinstance(value, str):
        return value
    if value is True:
        return "true"
    if value is False:
        return "false"
    return json.dumps(value, separators=(",", ":"))


def _codex_dependency_values(plugin: object) -> tuple[str, str]:
    if not isinstance(plugin, dict):
        return "", ""
    dependency_status = plugin.get("dependencyStatus")
    if not isinstance(dependency_status, dict):
        return "", ""
    dependencies = dependency_status.get("dependencies")
    if not isinstance(dependencies, list):
        return "", ""
    for dependency in dependencies:
        if isinstance(dependency, dict) and dependency.get("name") == "@openai/codex":
            return (
                _jq_raw(dependency.get("spec")),
                _jq_raw(dependency.get("resolvedPath"), null_as_empty=True),
            )
    return "", ""


def require_codex_runtime_exact(
    executable: str,
    config_path: str,
    state_dir: str,
) -> tuple[str, str, str]:
    """Validate the exact OpenClaw Codex plugin tuple."""

    completed = _run_openclaw(
        executable,
        config_path,
        state_dir,
        ("plugins", "inspect", "codex", "--json"),
        merge_stderr=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("ERROR: Could not inspect the required Codex plugin.")
    try:
        inspect_json: object = json.loads(completed.stdout)
    except json.JSONDecodeError:
        inspect_json = None
    plugin = inspect_json.get("plugin") if isinstance(inspect_json, dict) else None
    if not (
        isinstance(plugin, dict)
        and plugin.get("id") == "codex"
        and plugin.get("enabled") is True
        and plugin.get("status") == "loaded"
    ):
        raise RuntimeError(
            "ERROR: Required Codex plugin is not installed, enabled, and loaded.\n"
            "       Run bootstrap to reconcile the exact OpenClaw/Codex runtime tuple."
        )
    plugin_version = _jq_raw(plugin.get("version"), null_as_empty=True)
    app_server_version, app_server_path = _codex_dependency_values(plugin)
    if plugin_version != REQUIRED_CODEX_PLUGIN_VERSION:
        raise RuntimeError(
            f"ERROR: Codex plugin {plugin_version or '<unknown>'} is unsupported; "
            f"need exactly {REQUIRED_CODEX_PLUGIN_VERSION}.\n"
            "       Run bootstrap to reinstall the pinned plugin and gateway service."
        )
    if app_server_version != REQUIRED_CODEX_APP_SERVER_VERSION:
        raise RuntimeError(
            "ERROR: Embedded @openai/codex "
            f"{app_server_version or '<unknown>'} is unsupported; "
            f"need exactly {REQUIRED_CODEX_APP_SERVER_VERSION}.\n"
            "       Run bootstrap to reinstall the pinned plugin and gateway service."
        )
    cli_path = f"{app_server_path}/bin/codex.js"
    if not Path(cli_path).is_file():
        raise RuntimeError(f"ERROR: Embedded Codex CLI not found at {cli_path}.")
    return plugin_version, app_server_version, app_server_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_parser = subparsers.add_parser("resolve-openclaw-bin")
    resolve_parser.add_argument("--openclaw-bin", default=None)
    resolve_parser.add_argument("--home", default=None)
    resolve_parser.add_argument("--path", default=None)

    supported_parser = subparsers.add_parser("require-openclaw-supported")
    supported_parser.add_argument("executable")
    supported_parser.add_argument("config_path")
    supported_parser.add_argument("state_dir")

    exact_parser = subparsers.add_parser("require-codex-runtime-exact")
    exact_parser.add_argument("executable")
    exact_parser.add_argument("config_path")
    exact_parser.add_argument("state_dir")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "resolve-openclaw-bin":
            print(
                resolve_openclaw_bin(
                    args.openclaw_bin or os.environ.get("OPENCLAW_BIN"),
                    home=args.home,
                    path=args.path,
                )
            )
        elif args.command == "require-openclaw-supported":
            print(require_openclaw_supported(args.executable, args.config_path, args.state_dir))
        else:
            plugin, app_server, app_path = require_codex_runtime_exact(
                args.executable, args.config_path, args.state_dir
            )
            print(f"{plugin}\t{app_server}\t{app_path}")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
