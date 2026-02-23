# G2 OpenClaw Agent Configuration

This directory contains configuration files for the OpenClaw agent identity
used by the G2 Gateway.

## Files

- `SOUL.md` — System prompt that instructs the LLM to produce concise,
  plain-text responses suitable for the G2's constrained display.

> **Note:** `SOUL.md` is **not** loaded or injected by the Gateway at runtime.
> It must be configured directly on the OpenClaw server side (e.g. via the
> OpenClaw admin UI or agent config files). The Gateway only forwards messages;
> it does not set the agent's system prompt.

## Session Key

The Gateway uses `agent:claw:g2` as the session key for all interactions.
This provides conversation continuity within a single session.

## Tool Restrictions

The following OpenClaw tools are **not useful** on G2 glasses and should be
disabled or ignored in the agent configuration:

- `browser` — no web browser on glasses
- `canvas` — no canvas rendering capability
- `screen_record` — glasses have no screen recording
- `camera` — not accessible via EvenHub SDK

Useful tools:
- `memory` — persistent notes across sessions
- `web_search` — fetch information for voice queries
- `calculator` — quick math
