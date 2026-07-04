# G2 OpenClaw Agent Configuration

This directory contains bootstrap files for the OpenClaw agent identity.
Deploy them with `bash scripts/push-openclaw-config.sh`.

## Files

- `SOUL.md` — Core personality: orchestrator identity, boundaries, tone.
- `AGENTS.md` — Operational rules: orchestration loop, HIL gates, session management.
- `TOOLS.md` — Tool usage guide for OpenClaw Codex runtime and built-in tools.
- `BOOTSTRAP.md` — Project context: tech stack, repo layout, conventions.

## Load Order

OpenClaw reads bootstrap files in this order: AGENTS → SOUL → TOOLS → BOOTSTRAP.
Later files can reference concepts from earlier ones.

## Session Key

The Gateway uses `agent:claw:g2` as the session key for all interactions.
