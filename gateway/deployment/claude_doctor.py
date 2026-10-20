"""Read-only readiness probes for the Claude Code runtime used by OpenClaw's claude-cli backend."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

from gateway.agent_profiles import AgentProfileError, claude_config_dir
from gateway.deployment.versions import resolve_openclaw_bin

MIN_CLAUDE_VERSION: Final = (2, 1, 98)
SUBPROCESS_TIMEOUT_SECONDS: Final = 30.0
GATEWAY_TIMEOUT_MS: Final = 10000
SETTINGS_FILENAME: Final = "settings.json"
REQUIRED_PLUGIN: Final = "anthropic"
_MAX_DETAIL = 400
_TOKEN = re.compile(r"sk-ant-[A-Za-z0-9_\-]*")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")

PASS: Final = "pass"
FAIL: Final = "fail"
SKIP: Final = "skip"

Runner = Callable[[Sequence[str], Mapping[str, str]], "ProcessResult"]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def redact(text: str) -> str:
    """Remove any Anthropic token from text and bound its length."""
    cleaned = _TOKEN.sub("sk-ant-[REDACTED]", text).strip()
    if len(cleaned) > _MAX_DETAIL:
        cleaned = cleaned[:_MAX_DETAIL] + "..."
    return cleaned


def run_process(argv: Sequence[str], env: Mapping[str, str]) -> ProcessResult:
    """Run argv (never through a shell) with a bounded timeout and the given env."""
    try:
        completed = subprocess.run(
            list(argv),
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return ProcessResult(124, "", f"timed out after {SUBPROCESS_TIMEOUT_SECONDS:.0f}s")
    except OSError as exc:
        return ProcessResult(127, "", f"cannot run {argv[0]}: {exc.strerror or exc}")
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def _minimal_path() -> str:
    home = os.environ.get("HOME", "")
    entries = [e for e in os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin").split(":") if e]
    local_bin = f"{home}/.local/bin" if home else ""
    if local_bin and local_bin not in entries:
        entries.insert(0, local_bin)
    return ":".join(entries)


def _minimal_env(config_dir: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": _minimal_path(),
        "HOME": os.environ.get("HOME", ""),
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    # The push script's OPENCLAW_BIN override; without it the stale pnpm shim can win.
    if os.environ.get("OPENCLAW_BIN"):
        env["OPENCLAW_BIN"] = os.environ["OPENCLAW_BIN"]
    if extra:
        env.update(extra)
    return env


def resolve_claude_bin(claude_bin: str | None, env: Mapping[str, str]) -> str:
    """Explicit --claude-bin wins; otherwise search the minimal PATH (includes ~/.local/bin)."""
    if claude_bin:
        return os.path.expanduser(claude_bin)
    return shutil.which("claude", path=env["PATH"]) or "claude"


def check_version(runner: Runner, env: Mapping[str, str], claude: str = "claude") -> CheckResult:
    name = "claude_version"
    result = runner([claude, "--version"], env)
    if result.returncode != 0:
        return CheckResult(name, FAIL, redact(result.stderr or result.stdout or "claude failed"))
    match = _VERSION.search(result.stdout)
    if match is None:
        return CheckResult(name, FAIL, f"unparseable version output: {redact(result.stdout)}")
    version = (int(match[1]), int(match[2]), int(match[3]))
    wanted = ".".join(str(part) for part in MIN_CLAUDE_VERSION)
    found = ".".join(str(part) for part in version)
    if version < MIN_CLAUDE_VERSION:
        return CheckResult(name, FAIL, f"claude {found} is older than required {wanted}")
    return CheckResult(name, PASS, f"claude {found} (>= {wanted})")


def check_auth(
    runner: Runner, env: Mapping[str, str], config_dir: Path, claude: str = "claude"
) -> CheckResult:
    name = "claude_auth"
    result = runner([claude, "auth", "status", "--json"], env)
    try:
        parsed: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        detail = redact(result.stderr or result.stdout or f"exit {result.returncode}")
        return CheckResult(name, FAIL, f"claude auth status gave no JSON status: {detail}")
    logged_in = parsed.get("loggedIn")
    if logged_in is not True:
        return CheckResult(name, FAIL, f"not logged in for CLAUDE_CONFIG_DIR={config_dir}")
    method = parsed.get("authMethod")
    sub = parsed.get("subscriptionType")
    parts = [f"authMethod={method}" if isinstance(method, str) else None]
    parts.append(f"subscription={sub}" if isinstance(sub, str) else None)
    detail = ", ".join(part for part in parts if part)
    return CheckResult(name, PASS, redact(f"logged in ({detail})" if detail else "logged in"))


def check_settings(config_dir: Path) -> CheckResult:
    name = "claude_settings"
    path = config_dir / SETTINGS_FILENAME
    if not path.is_file():
        return CheckResult(name, FAIL, f"{path} does not exist")
    try:
        runtime = importlib.import_module("gateway.deployment.claude_runtime")
    except ModuleNotFoundError as exc:
        if exc.name != "gateway.deployment.claude_runtime":
            return CheckResult(name, FAIL, redact(f"cannot import claude_runtime: {exc}"))
        return CheckResult(
            name, SKIP, f"warning: claude_runtime validator not available; {path} exists"
        )
    validate = getattr(runtime, "validate_settings_file", None)
    if not callable(validate):
        return CheckResult(name, FAIL, "claude_runtime has no validate_settings_file")
    try:
        problems = validate(path)
    except Exception as exc:  # validator error types are owned by claude_runtime
        return CheckResult(name, FAIL, redact(f"{path} invalid: {exc}"))
    if not isinstance(problems, list):
        return CheckResult(name, FAIL, "validate_settings_file did not return a list of problems")
    if problems:
        return CheckResult(name, FAIL, redact(f"{path} invalid: " + "; ".join(map(str, problems))))
    return CheckResult(name, PASS, f"{path} valid")


def check_gateway(runner: Runner, env: Mapping[str, str]) -> CheckResult:
    name = "gateway_plugins"
    try:
        openclaw = resolve_openclaw_bin(env.get("OPENCLAW_BIN"), home=env["HOME"], path=env["PATH"])
    except RuntimeError as exc:
        return CheckResult(name, FAIL, redact(str(exc)))
    argv = [
        openclaw,
        "gateway",
        "call",
        "health",
        "--json",
        "--timeout",
        str(GATEWAY_TIMEOUT_MS),
    ]
    result = runner(argv, env)
    try:
        parsed: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        detail = redact(result.stderr or result.stdout or f"exit {result.returncode}")
        return CheckResult(name, FAIL, f"gateway health gave no JSON: {detail}")
    plugins = parsed.get("plugins") if isinstance(parsed, dict) else None
    loaded = plugins.get("loaded") if isinstance(plugins, dict) else None
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        error = parsed.get("error") if isinstance(parsed, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        reason = f": {redact(message)}" if isinstance(message, str) else ""
        return CheckResult(name, FAIL, f"gateway health has no plugins.loaded list{reason}")
    if REQUIRED_PLUGIN not in loaded:
        return CheckResult(
            name, FAIL, redact(f"plugins.loaded lacks {REQUIRED_PLUGIN!r}: {sorted(loaded)}")
        )
    return CheckResult(name, PASS, redact(f"plugins.loaded includes {REQUIRED_PLUGIN!r}"))


def run_checks(
    state_dir: Path,
    *,
    gateway: bool = False,
    claude_bin: str | None = None,
    runner: Runner = run_process,
) -> list[CheckResult]:
    config_dir = claude_config_dir(state_dir)
    env = _minimal_env(config_dir)
    claude = resolve_claude_bin(claude_bin, env)
    results = [
        check_version(runner, env, claude),
        check_auth(runner, env, config_dir, claude),
        check_settings(config_dir),
    ]
    if gateway:
        resolved_state = config_dir.parent
        gateway_env = _minimal_env(
            config_dir,
            {
                "OPENCLAW_STATE_DIR": str(resolved_state),
                "OPENCLAW_CONFIG_PATH": str(resolved_state / "openclaw.json"),
            },
        )
        results.append(check_gateway(runner, gateway_env))
    return results


def check_claude_auth_readiness(
    state_dir: Path, *, claude_bin: str | None = None, runner: Runner = run_process
) -> list[str]:
    """Return failure messages for the Claude auth/version prerequisites (empty when ready)."""
    config_dir = claude_config_dir(state_dir)
    env = _minimal_env(config_dir)
    claude = resolve_claude_bin(claude_bin, env)
    results = [check_version(runner, env, claude), check_auth(runner, env, config_dir, claude)]
    return [f"{item.name}: {item.detail}" for item in results if item.status == FAIL]


def exit_code(results: Sequence[CheckResult]) -> int:
    return 1 if any(item.status == FAIL for item in results) else 0


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="verify the Claude Code runtime prerequisites")
    check.add_argument("--state-dir", required=True, type=Path)
    check.add_argument("--gateway", action="store_true", help="also check gateway health plugins")
    check.add_argument("--claude-bin", default=None, help="claude executable (default: PATH)")
    check.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        results = run_checks(args.state_dir, gateway=args.gateway, claude_bin=args.claude_bin)
    except AgentProfileError as exc:
        print(f"ERROR: {redact(str(exc))}", file=sys.stderr)
        return 2
    code = exit_code(results)
    if args.as_json:
        print(
            json.dumps(
                {"ok": code == 0, "checks": [item.to_dict() for item in results]},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for item in results:
            print(f"[{item.status.upper()}] {item.name}: {item.detail}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
