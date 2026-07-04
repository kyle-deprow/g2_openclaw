# G2 OpenClaw Agent Configuration

This directory contains repo-managed bootstrap files for OpenClaw agent
identity. Deploy them with `bash scripts/push-openclaw-config.sh`.

The push script copies `AGENTS.md`, `SOUL.md`, `TOOLS.md`, and `BOOTSTRAP.md`
to every configured agent workspace derived from
`gateway/openclaw_config/openclaw.json`. The `main` agent lands in the default
OpenClaw workspace at `~/.openclaw/workspace`; other agents default to
`~/.openclaw/workspace-{id}` unless an explicit `.workspace` is configured. The
script does not copy or overwrite local workspace files such as `USER.md`,
`IDENTITY.md`, or personal notes.

## Files

- `SOUL.md` — Core personality: Research PM identity, boundaries, tone.
- `AGENTS.md` — Operational rules: orchestration loop, HIL gates, session management.
- `TOOLS.md` — Tool usage guide for OpenClaw Codex runtime and built-in tools.
- `BOOTSTRAP.md` — Project context: tech stack, repo layout, conventions.

## Load Order

OpenClaw reads bootstrap files in this order: AGENTS → SOUL → TOOLS → BOOTSTRAP.
Later files can reference concepts from earlier ones.

## Session Key

The Gateway uses `agent:claw:g2` as the session key for all interactions.
