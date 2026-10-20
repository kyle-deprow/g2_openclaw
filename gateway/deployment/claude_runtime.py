"""Render and validate the gateway's dedicated Claude Code settings.json.

The gateway runs Claude Code with ``CLAUDE_CONFIG_DIR=<state>/claude-code``. Native
Write/Edit/Bash calls are not confined by OpenClaw, so path-level confinement comes from
the permission deny rules and Bash sandbox rendered here (Option B: only the trusted
research CLI runs outside the sandbox, via ``sandbox.excludedCommands``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gateway import agent_profiles

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "openclaw_config"
    / "claude-code"
    / "settings.json.template"
)
DROPIN_PATH = TEMPLATE_PATH.parent.parent / "openclaw-gateway-claude-config.conf"
SCALAR_VARS = (
    "HOME",
    "OPENCLAW_STATE_DIR",
    "RESEARCH_V2_ROOT",
    "HYPOTHESIS_WORKTREES_ROOT",
    "REPO_ROOT",
    "OPERATOR_ENV_FILE",
)
LIST_VARS = ("OTHER_RESEARCH_ROOTS", "OTHER_WORKSPACES")
KNOWN_VARS = SCALAR_VARS + LIST_VARS
REQUIRED_VARS = ("RESEARCH_V2_ROOT", "REPO_ROOT", "OPERATOR_ENV_FILE")
OWNER_WORKSPACE = "workspace-research-orchestrator"
# Child workspaces the template denies literally (present or not); discovery skips them.
LITERAL_WORKSPACES = (
    "workspace",
    "workspace-research-implementer",
    "workspace-research-runner",
    "workspace-research-reviewer",
)
# The research store's real file names (gateway/research/store.py opens root / "state.sqlite3").
STORE_FILENAME = "state.sqlite3"
STORE_FILENAMES = (STORE_FILENAME, f"{STORE_FILENAME}-wal", f"{STORE_FILENAME}-shm")
# Entries use Bash(...) rule syntax: no wildcard means exact match (argument-less), so the
# trailing " *" is required for "research <subcommand> ..." to leave the sandbox.
EXCLUDED_CLI_SUFFIX = "/.venv/bin/gateway-cli research *"
_PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")
_GLOB_CHARS = frozenset("*?[]")
_FORBIDDEN_PATH_CHARS = frozenset("*?[]()")

# Literal suffixes every rendered file must deny (sandbox denyWrite entries).
_REQUIRED_DENY_WRITE_SUFFIXES = (
    *(f"/{name}" for name in STORE_FILENAMES),
    "/hypotheses",
    "/review-bundles",
    "/native-runtime-record-registration.json",
    "/repos",
    "/.config",
    "/.local",
    "/.ssh",
    "/openclaw.json",
    "/agents",
    "/state",
    "/extensions",
    "/credentials",
    "/identity",
    "/devices",
    "/cron",
    "/g2-agent-profile.json",
    "/claude-code",
    *(f"/{name}" for name in LITERAL_WORKSPACES),
)
# Edit deny rule suffixes (path part of Edit(...)).
_REQUIRED_EDIT_DENY_SUFFIXES = (
    *(f"/{name}" for name in STORE_FILENAMES),
    "/hypotheses/**",
    "/review-bundles/**",
    "/native-runtime-record-registration.json",
    "/repos/**",
    "/.config/**",
    "/.local/**",
    "/.ssh/**",
    "/.claude/**",
    "/openclaw.json",
    "/agents/**",
    "/state/**",
    "/claude-code/**",
    "/g2-agent-profile.json",
    "/mempalace-readonly-server.py",
    "/copilot_headers_preload.py",
    "/openclaw.json.last-good",
    "/openclaw.json.pre-allow-probe",
    "/openclaw.json.bak*",
    "/plugins/**",
    "/npm/**",
    "/skills/**",
    "/plugin-skills/**",
    *(f"/{name}/**" for name in LITERAL_WORKSPACES),
    "/.bashrc",
    "/.zshrc",
    "/.gitconfig",
    "**/.env",
)
_SANDBOX_TRUE = ("enabled", "failIfUnavailable", "autoAllowBashIfSandboxed")
_SANDBOX_FORBIDDEN_TRUE = (
    "enableWeakerNestedSandbox",
    "enableWeakerNetworkIsolation",
    "allowAppleEvents",
)
_INEFFECTIVE_PATH_RULE = re.compile(r"^(Write|MultiEdit|NotebookEdit)\(")


class ClaudeRuntimeError(ValueError):
    """Raised when settings cannot be rendered or fail validation."""


VarValue = str | list[str]


def _substitute_string(text: str, values: Mapping[str, VarValue]) -> list[str]:
    """Expand one template string; a list-valued placeholder yields one string per value."""
    results = [text]
    for match in dict.fromkeys(_PLACEHOLDER.findall(text)):
        value = values.get(match[1:-1])
        if value is None:
            continue
        replacements = [value] if isinstance(value, str) else value
        results = [item.replace(match, rep) for item in results for rep in replacements]
    return results


def _substitute(node: Any, values: Mapping[str, VarValue]) -> Any:
    if isinstance(node, str):
        expanded = _substitute_string(node, values)
        return expanded[0] if expanded else ""
    if isinstance(node, list):
        out: list[Any] = []
        for item in node:
            if isinstance(item, str):
                out.extend(_substitute_string(item, values))
            else:
                out.append(_substitute(item, values))
        return out
    if isinstance(node, dict):
        return {key: _substitute(value, values) for key, value in node.items()}
    return node


def _check_path_var(key: str, value: str) -> str:
    if not value.startswith("/") or value == "/" or "\x00" in value or "\n" in value:
        raise ClaudeRuntimeError(f"{key} must be an absolute path other than /: {value!r}")
    if "@" in value or _FORBIDDEN_PATH_CHARS & set(value):
        raise ClaudeRuntimeError(f"{key} must not contain any of @ * ? [ ] ( ): {value!r}")
    if any(part in {"..", "."} for part in value.split("/")):
        raise ClaudeRuntimeError(f"{key} must be normalized: {value!r}")
    return value.rstrip("/")


def _split_list(value: str) -> list[str]:
    return [item for item in value.split(",") if item]


def resolve_vars(state_dir: Path, overrides: Mapping[str, str]) -> dict[str, VarValue]:
    unknown = sorted(set(overrides) - set(KNOWN_VARS))
    if unknown:
        raise ClaudeRuntimeError(f"unknown --var key(s) {unknown}; known: {list(KNOWN_VARS)}")
    missing = [key for key in REQUIRED_VARS if not overrides.get(key)]
    if missing:
        raise ClaudeRuntimeError(f"missing required --var key(s) {missing}")
    state = str(state_dir.expanduser())
    root = _check_path_var("RESEARCH_V2_ROOT", overrides["RESEARCH_V2_ROOT"])
    scalars = {
        "HOME": overrides.get("HOME", os.path.expanduser("~")),
        "OPENCLAW_STATE_DIR": state,
        "RESEARCH_V2_ROOT": root,
        "HYPOTHESIS_WORKTREES_ROOT": overrides.get(
            "HYPOTHESIS_WORKTREES_ROOT", f"{root}/hypothesis-worktrees"
        ),
        "REPO_ROOT": overrides["REPO_ROOT"],
        "OPERATOR_ENV_FILE": overrides["OPERATOR_ENV_FILE"],
    }
    values: dict[str, VarValue] = {k: _check_path_var(k, v) for k, v in scalars.items()}
    state_path = Path(str(values["OPENCLAW_STATE_DIR"]))
    if "OTHER_RESEARCH_ROOTS" in overrides:
        roots = _split_list(overrides["OTHER_RESEARCH_ROOTS"])
    else:
        roots = sorted(
            str(p)
            for p in state_path.glob("research-v[0-9]*")
            if p.is_dir() and os.path.realpath(p) != os.path.realpath(root)
        )
    if "OTHER_WORKSPACES" in overrides:
        spaces = _split_list(overrides["OTHER_WORKSPACES"])
    else:
        spaces = sorted(
            str(p)
            for p in [state_path / "workspace", *state_path.glob("workspace-*")]
            if p.name != OWNER_WORKSPACE and p.name not in LITERAL_WORKSPACES and p.is_dir()
        )
    values["OTHER_RESEARCH_ROOTS"] = [_check_path_var("OTHER_RESEARCH_ROOTS", p) for p in roots]
    values["OTHER_WORKSPACES"] = [_check_path_var("OTHER_WORKSPACES", p) for p in spaces]
    return values


def render_data(
    values: Mapping[str, VarValue], template_path: Path = TEMPLATE_PATH
) -> dict[str, Any]:
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClaudeRuntimeError(f"cannot read template {template_path}: {exc}") from exc
    rendered = _substitute(template, values)
    leftover = sorted(set(_PLACEHOLDER.findall(json.dumps(rendered))))
    if leftover:
        raise ClaudeRuntimeError(f"unrendered placeholders left in settings: {leftover}")
    assert isinstance(rendered, dict)
    return rendered


def render_settings(values: Mapping[str, VarValue], template_path: Path = TEMPLATE_PATH) -> str:
    return json.dumps(render_data(values, template_path), indent=2) + "\n"


def _str_list(value: object) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def validate_settings_data(data: object, expected: Mapping[str, Any] | None = None) -> list[str]:
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["settings must be a JSON object"]
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        problems.append("permissions must be an object")
        permissions = {}
    deny = _str_list(permissions.get("deny"))
    if deny is None:
        problems.append("permissions.deny must be a list of strings")
        deny = []
    if permissions.get("defaultMode") == "bypassPermissions":
        problems.append("permissions.defaultMode must not be bypassPermissions")
    if permissions.get("additionalDirectories"):
        problems.append("permissions.additionalDirectories must not be set")
    edit_paths = [rule[5:-1] for rule in deny if rule.startswith("Edit(") and rule.endswith(")")]
    for suffix in _REQUIRED_EDIT_DENY_SUFFIXES:
        if not any(path.endswith(suffix) for path in edit_paths):
            problems.append(f"permissions.deny lacks an Edit(...) rule ending in {suffix}")
    for rule in deny:
        if _INEFFECTIVE_PATH_RULE.match(rule):
            problems.append(f"deny rule {rule!r} is never consulted; use Edit(...)")
    for path in edit_paths:
        if not path.startswith(("//", "**/")):
            problems.append(f"Edit deny path {path!r} must start with // (absolute) or **/")
    sandbox = data.get("sandbox")
    if not isinstance(sandbox, dict):
        problems.append("sandbox must be an object")
        sandbox = {}
    for key in _SANDBOX_TRUE:
        if sandbox.get(key) is not True:
            problems.append(f"sandbox.{key} must be true")
    if sandbox.get("allowUnsandboxedCommands") is not False:
        problems.append("sandbox.allowUnsandboxedCommands must be false")
    for key in _SANDBOX_FORBIDDEN_TRUE:
        if sandbox.get(key) not in (None, False):
            problems.append(f"sandbox.{key} must not be enabled")
    network = sandbox.get("network")
    if isinstance(network, dict):
        if network.get("allowAllUnixSockets") not in (None, False):
            problems.append("sandbox.network.allowAllUnixSockets must not be true")
    elif network is not None:
        problems.append("sandbox.network must be an object")
    excluded = _str_list(sandbox.get("excludedCommands"))
    if (
        excluded is None
        or len(excluded) != 1
        or not excluded[0].startswith("/")
        or not excluded[0].endswith(EXCLUDED_CLI_SUFFIX)
        or _GLOB_CHARS & set(excluded[0][:-2])
    ):
        problems.append(
            "sandbox.excludedCommands must be exactly one absolute "
            f"'<repo>{EXCLUDED_CLI_SUFFIX}' entry"
        )
    filesystem = sandbox.get("filesystem")
    if not isinstance(filesystem, dict):
        problems.append("sandbox.filesystem must be an object")
        filesystem = {}
    if filesystem.get("disabled") not in (None, False):
        problems.append("sandbox.filesystem.disabled must not be true")
    home = os.path.expanduser("~").rstrip("/")
    lists: dict[str, list[str]] = {}
    for key in ("allowWrite", "denyWrite"):
        entries = _str_list(filesystem.get(key))
        if entries is None:
            problems.append(f"sandbox.filesystem.{key} must be a list of strings")
            continue
        lists[key] = entries
        if not entries:
            problems.append(f"sandbox.filesystem.{key} must not be empty")
        for item in entries:
            if not item.startswith("/") or ".." in item.split("/") or "\x00" in item:
                problems.append(
                    f"sandbox.filesystem.{key} entry {item!r} must be a normalized absolute path"
                )
            if _GLOB_CHARS & set(item):
                problems.append(
                    f"sandbox.filesystem.{key} entry {item!r} has a wildcard (ignored on Linux)"
                )
    for item in lists.get("allowWrite", []):
        stripped = item.rstrip("/")
        if stripped in ("", "/home", home) or stripped.count("/") < 2:
            problems.append(f"sandbox.filesystem.allowWrite entry {item!r} is too broad")
    deny_write = lists.get("denyWrite", [])
    for suffix in _REQUIRED_DENY_WRITE_SUFFIXES:
        if not any(item.endswith(suffix) for item in deny_write):
            problems.append(f"sandbox.filesystem.denyWrite lacks an entry ending in {suffix}")
    for key, expected_value in (
        ("disableAllHooks", True),
        ("autoMemoryEnabled", False),
        ("includeCoAuthoredBy", False),
    ):
        if data.get(key) is not expected_value:
            problems.append(f"{key} must be {str(expected_value).lower()}")
    if data.get("enableAllProjectMcpServers") not in (None, False):
        problems.append("enableAllProjectMcpServers must not be true")
    plugins = data.get("enabledPlugins", {})
    if not isinstance(plugins, dict):
        problems.append("enabledPlugins must be an object")
    elif any(plugins.values()):
        problems.append("enabledPlugins must not enable any plugin")
    if data.get("mcpServers"):
        problems.append("mcpServers must not be configured")
    leftover = sorted(set(_PLACEHOLDER.findall(json.dumps(data))))
    if leftover:
        problems.append(f"unrendered placeholders: {leftover}")
    if expected is not None and data != expected:
        problems.append("settings differ from the template rendered with the expected variables")
    return problems


def validate_settings_file(
    path: Path, *, expected_vars: Mapping[str, VarValue] | None = None
) -> list[str]:
    """Return a list of problems with a rendered settings file; empty means valid.

    With ``expected_vars`` (as returned by :func:`resolve_vars`) the file must also equal
    the template rendered with exactly those values.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"cannot read {path}: {exc}"]
    except ValueError as exc:
        return [f"{path} is not valid JSON: {exc}"]
    problems = _drift_problems(path, data)
    expected = None
    if expected_vars is not None:
        try:
            expected = render_data(expected_vars)
        except ClaudeRuntimeError as exc:
            return [str(exc)]
    return validate_settings_data(data, expected) + problems


def _real(path: str) -> str:
    return os.path.realpath(path)


def _drift_problems(path: Path, data: object) -> list[str]:
    """Fail if workspaces or research roots that exist now are not denied in the settings.

    The state dir is inferred from ``<state>/claude-code/settings.json``; the active research
    root is the directory of the denied ``state.sqlite3`` entry. Roots compare by realpath.
    """
    if path.parent.name != agent_profiles.CLAUDE_CONFIG_DIRNAME or not isinstance(data, dict):
        return []
    sandbox = data.get("sandbox")
    filesystem = sandbox.get("filesystem") if isinstance(sandbox, dict) else None
    deny_write = _str_list(filesystem.get("denyWrite")) if isinstance(filesystem, dict) else None
    permissions = data.get("permissions")
    deny_rules = _str_list(permissions.get("deny")) if isinstance(permissions, dict) else None
    if deny_write is None or deny_rules is None:
        return []  # reported by validate_settings_data
    denied_sandbox = {_real(item) for item in deny_write}
    denied_edit = {
        _real(rule[6:-1].removesuffix("/**"))
        for rule in deny_rules
        if rule.startswith("Edit(//") and rule.endswith(")")
    }
    active = {
        _real(os.path.dirname(item)) for item in deny_write if item.endswith(f"/{STORE_FILENAME}")
    }
    state_dir = path.parent.parent
    candidates = [
        entry
        for entry in sorted(state_dir.glob("workspace*"))
        if entry.name != OWNER_WORKSPACE and entry.is_dir()
    ]
    candidates += [
        entry
        for entry in sorted(state_dir.glob("research-v[0-9]*"))
        if entry.is_dir() and _real(str(entry)) not in active
    ]
    problems: list[str] = []
    for entry in candidates:
        real = _real(str(entry))
        if real not in denied_sandbox:
            problems.append(f"{entry} exists but is not in sandbox.filesystem.denyWrite")
        if real not in denied_edit:
            problems.append(f"{entry} exists but has no Edit deny rule")
    return problems


def check_dropin(
    state_dir: Path, home: Path | None = None, dropin: Path = DROPIN_PATH
) -> list[str]:
    """The systemd drop-in must point CLAUDE_CONFIG_DIR at ``claude_config_dir(state_dir)``."""
    try:
        text = dropin.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read {dropin}: {exc}"]
    match = re.search(r"^Environment=CLAUDE_CONFIG_DIR=(\S+)$", text, re.MULTILINE)
    if not match:
        return [f"{dropin} does not set CLAUDE_CONFIG_DIR"]
    configured = match.group(1).replace("%h", str(home or Path.home()))
    want = str(agent_profiles.claude_config_dir(state_dir))
    if configured != want:
        return [f"drop-in CLAUDE_CONFIG_DIR {configured} != {want}"]
    return []


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(target: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
        _fsync_dir(target.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _make_private_dirs(config_dir: Path) -> None:
    missing: list[Path] = []
    current = config_dir
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    if not config_dir.is_dir():
        raise ClaudeRuntimeError(f"{config_dir} exists and is not a directory")
    os.chmod(config_dir, 0o700)


def install(state_dir: Path, profile_name: str, overrides: Mapping[str, str]) -> Path | None:
    """Render settings.json; returns None (no-op) for non-claude profiles."""
    try:
        profile = agent_profiles.load_profile(profile_name)
    except agent_profiles.AgentProfileError as exc:
        raise ClaudeRuntimeError(str(exc)) from exc
    if profile.runtime != "claude-cli":
        return None
    values = resolve_vars(state_dir, overrides)
    text = render_settings(values)
    problems = validate_settings_data(json.loads(text), json.loads(text))
    if problems:
        raise ClaudeRuntimeError("rendered settings invalid: " + "; ".join(problems))
    config_dir = agent_profiles.claude_config_dir(state_dir)
    if config_dir.is_symlink():
        raise ClaudeRuntimeError(f"{config_dir} must not be a symlink")
    _make_private_dirs(config_dir)
    target = config_dir / "settings.json"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ClaudeRuntimeError(f"{target} must be a regular file")
    _atomic_write(target, text)
    return target


def _parse_vars(items: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ClaudeRuntimeError(f"--var expects KEY=VALUE, got {item!r}")
        result[key] = value
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude_runtime", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install", help="render settings.json into the dedicated config dir")
    inst.add_argument("--state-dir", type=Path, required=True)
    inst.add_argument("--profile", required=True)
    inst.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")
    val = sub.add_parser("validate", help="validate a rendered settings file")
    val.add_argument("--settings", type=Path, required=True)
    val.add_argument(
        "--state-dir", type=Path, help="with --var: also require exact expected values"
    )
    val.add_argument("--var", action="append", default=[], metavar="KEY=VALUE")
    drop = sub.add_parser("check-dropin", help="check the systemd drop-in against the state dir")
    drop.add_argument("--state-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "install":
            target = install(args.state_dir, args.profile, _parse_vars(args.var))
            if target is None:
                print(f"profile {args.profile!r} does not use claude-cli; nothing to install")
            else:
                print(target)
            return 0
        if args.command == "check-dropin":
            problems = check_dropin(args.state_dir)
        else:
            expected = (
                resolve_vars(args.state_dir, _parse_vars(args.var)) if args.state_dir else None
            )
            problems = validate_settings_file(args.settings, expected_vars=expected)
    except ClaudeRuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
