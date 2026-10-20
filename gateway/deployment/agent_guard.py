"""Config for the repo-owned ``g2-agent-guard`` OpenClaw plugin.

Under the claude profile OpenClaw's per-agent ``tools.deny`` does not stop Claude
Code's native tools, so per-agent policy comes from a ``before_tool_call`` plugin.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from gateway.agent_profiles import AgentProfile, AgentProfileError, load_profile

PLUGIN_ID = "g2-agent-guard"
_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "openclaw_plugins" / PLUGIN_ID

# Names as seen by OpenClaw's before_tool_call hook (case-insensitive, "_"/"-" ignored).
FILE_WRITE_TOOLS: tuple[str, ...] = ("write", "edit", "multiedit", "notebookedit", "apply_patch")
# Claude Code's Bash reaches the hook as "exec"; the plugin treats "bash" and "exec" alike.
SHELL_TOOLS: tuple[str, ...] = ("bash",)
# Claude Code's built-in delegation tools (hook names observed live: "agent", "workflow";
# "task" was not present in 2.1.304 but is denied as the legacy name). They would let a
# pinned agent hand work to another model invisibly to OpenClaw.
SUBAGENT_TOOLS: tuple[str, ...] = (
    "agent",
    "task",
    "workflow",
    # Version-drift insurance: scheduling, messaging, worktree and plan-mode tools.
    "monitor",
    "enter_worktree",
    "cron_create",
    "cron_list",
    "cron_delete",
    "schedule_wakeup",
    "send_message",
    "enter_plan_mode",
)
# Every Claude Code native tool the hook has been observed to see. "*" is not used for
# main because OpenClaw's own MCP tools (for example sessions_list) pass the same hook
# under their plain names and "*" would block them.
MAIN_DENY_TOOLS: tuple[str, ...] = (
    *SHELL_TOOLS,
    *FILE_WRITE_TOOLS,
    "read",
    "glob",
    "grep",
    "ls",
    "web_fetch",
    "web_search",
    "todo_write",
    *SUBAGENT_TOOLS,
)


# `gateway-cli research` subcommands the owner (Astra) must never run. Audited against
# gateway/research/cli.py: each is operator-only by docstring or takes --operator-reference
# (`run-release-unstarted`, `run-reverify`, `operator-note`, `campaign-policy-set`), is
# operator setup or registration of operator-supplied records (`init`,
# `exposure-ledger-register`, `native-runtime-register`), is the operator's counterpart to
# the owner `pause --owner` (`resume`), or is the owner-wake daemon (`serve`).
OPERATOR_ONLY_SUBCOMMANDS: tuple[str, ...] = (
    "init",
    "campaign-policy-set",
    "exposure-ledger-register",
    "native-runtime-register",
    "run-release-unstarted",
    "run-reverify",
    "operator-note",
    "resume",
    "serve",
)
# Explicit owner allowlist. A test pins OWNER_SUBCOMMANDS + OPERATOR_ONLY_SUBCOMMANDS to
# exactly the commands registered in gateway/research/cli.py, so a new subcommand fails
# that test until it is classified here.
OWNER_SUBCOMMANDS: tuple[str, ...] = (
    "hypothesis-create",
    "hypothesis-freeze",
    "compute-probe",
    "hypothesis-set-compute",
    "attempt-open",
    "implementation-submit",
    "submission-preflight",
    "submission-build",
    "review-bundle",
    "review-reserve",
    "review-reconcile",
    "review-collect",
    "review-cancel",
    "run",
    "power-check",
    "reconcile",
    "cancel",
    "attempt-close",
    "hypothesis-decide",
    "pause",
    "status",
)
RUNNER_SUBCOMMANDS: tuple[str, ...] = ("run",)


def guard_plugin_dir() -> Path:
    return _PLUGIN_DIR


def _require_absolute(name: str, value: Path) -> str:
    if not value.is_absolute():
        raise ValueError(f"{name} must be an absolute path, got {str(value)!r}")
    return str(value)


def guard_config(
    profile: AgentProfile,
    *,
    owner_workspace_root: Path,
    hypothesis_worktrees_root: Path,
    research_root: Path,
    gateway_cli: Path,
) -> dict[str, object]:
    """Plugin config (``plugins.entries["g2-agent-guard"].config``) for ``profile``."""
    owner_ws = _require_absolute("owner_workspace_root", owner_workspace_root)
    worktrees = _require_absolute("hypothesis_worktrees_root", hypothesis_worktrees_root)
    root = _require_absolute("research_root", research_root)
    cli = _require_absolute("gateway_cli", gateway_cli)
    if profile.runtime != "claude-cli":
        return {"agents": {}}
    no_cli = {"cli": cli, "mode": "cli-calls-only", "allow": []}
    return {
        "agents": {
            "main": {"denyTools": list(MAIN_DENY_TOOLS), "cliGate": no_cli},
            "research-orchestrator": {
                "denyTools": list(SUBAGENT_TOOLS),
                "writeRoots": [owner_ws, root],
                "cliGate": {
                    "cli": cli,
                    "mode": "gated",
                    "allow": list(OWNER_SUBCOMMANDS),
                    "research_root": root,
                    # Without --owner the CLI records an operator-attributed pause.
                    "required_flags": {"pause": ["--owner"]},
                },
            },
            "research-implementer": {
                "denyTools": list(SUBAGENT_TOOLS),
                "writeRoots": [worktrees],
                "cliGate": {"cli": cli, "mode": "gated", "allow": []},
            },
            "research-runner": {
                "denyTools": [*FILE_WRITE_TOOLS, *SUBAGENT_TOOLS],
                "cliGate": {
                    "cli": cli,
                    "mode": "cli-calls-only",
                    "allow": list(RUNNER_SUBCOMMANDS),
                    "research_root": root,
                },
            },
            "research-reviewer": {
                "denyTools": [*FILE_WRITE_TOOLS, *SHELL_TOOLS, *SUBAGENT_TOOLS],
                "cliGate": no_cli,
            },
        }
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gateway.deployment.agent_guard")
    sub = parser.add_subparsers(dest="command", required=True)
    cfg = sub.add_parser("config", help="print the plugin config as JSON")
    cfg.add_argument("--profile", required=True)
    cfg.add_argument("--owner-workspace-root", type=Path, required=True)
    cfg.add_argument("--hypothesis-worktrees-root", type=Path, required=True)
    cfg.add_argument("--research-root", type=Path, required=True)
    cfg.add_argument("--gateway-cli", type=Path, required=True)
    sub.add_parser("plugin-dir", help="print the plugin directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "plugin-dir":
        print(guard_plugin_dir())
        return 0
    try:
        profile = load_profile(args.profile)
    except AgentProfileError as exc:
        print(f"agent_guard: {exc}", file=sys.stderr)
        return 2
    try:
        config = guard_config(
            profile,
            owner_workspace_root=args.owner_workspace_root,
            hypothesis_worktrees_root=args.hypothesis_worktrees_root,
            research_root=args.research_root,
            gateway_cli=args.gateway_cli,
        )
    except ValueError as exc:
        print(f"agent_guard: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(config, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
