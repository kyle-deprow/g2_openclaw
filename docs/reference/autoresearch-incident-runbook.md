# Autoresearch incident runbook

Terse recovery notes for the Codex app-server and current research owner.

## Data API

If prewarm reports an `httpx.ConnectError` while connecting to
`127.0.0.1:8000`, restart the independent data-plane unit
`quantipy-api.service` with `systemctl --user restart quantipy-api.service`.

## Current owner service and controls

`research-owner.service` is the single bounded owner service. It runs the
current `research serve` driver and is bound to the gateway lifecycle. The root
operator owns service lifecycle and deployment; this note does not authorize
editing receipts, changing policy, or creating a second worker.

Use only the live `uv run gateway-cli research --help` vocabulary. The verified
read/status and pause/resume forms require the research root; pause and resume
also require an explicit reason:

```text
uv run gateway-cli research status --root <research-root> --json
uv run gateway-cli research pause --root <research-root> --reason "<reason>"
uv run gateway-cli research resume --root <research-root> --reason "<reason>"
```

Status is read-only. Pause when policy, route, review, input, or job evidence is
missing or unresolved; resume only after the root operator has verified the
required evidence and policy. Never infer success from a missing status, an
unknown response, a process heartbeat, or a smoke/API check. Preserve durable
receipts and escalate service or data-plane failures to the root operator.

## Auth recovery

`refresh_token_invalidated` means the auth profile needs recovery. Profile
cooldown self-heals when available; otherwise re-login to the OpenAI/Codex
profile.
