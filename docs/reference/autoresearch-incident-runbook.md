# Autoresearch incident runbook

Terse recovery notes for the Codex app-server and autoresearch owner session.

## Unified exec: missing arg0 shim

Signature:

```text
CreateProcess { Rejected("Failed to create unified exec process: No such file or directory") }
```

Confirm:

```bash
gateway-cli autoresearch-doctor
pgrep -af 'codex app-server'
```

If doctor reports a stale `tmp/arg0/codex-arg0*` directory, restart the gateway:

```bash
systemctl --user restart openclaw-gateway.service
```

## Missing Codex writable root

Confirm with `gateway-cli autoresearch-doctor`; it reports each missing declared root.
Redeploy the managed config, or create the reported roots privately:

```bash
mkdir -m 700 -p /home/dev/.openclaw/autoresearch/model-workspaces
mkdir -m 700 -p /home/dev/.openclaw/autoresearch/stage-inbox
```

## Stale owner MCP connection

Signature: MCP calls time out after `20,000 ms` while the resumed owner thread is
otherwise active. Confirm with `gateway-cli autoresearch-doctor`, then reset the
owner mapping so the next wake creates a fresh thread:

```bash
gateway-cli autoresearch-reset-owner-session --confirm
```

Immediately after a gateway restart, a first MCP cold start can also take about
20 seconds; allow that one startup timeout before treating it as stale state.

Stop both services before resetting, and verify they are inactive first:

```bash
systemctl --user stop quantipy-autoresearch-supervisor.service openclaw-gateway.service
systemctl --user is-active quantipy-autoresearch-supervisor.service openclaw-gateway.service
gateway-cli autoresearch-reset-owner-session --confirm
```

Restart the services and verify recovery:

```bash
systemctl --user restart openclaw-gateway.service quantipy-autoresearch-supervisor.service
gateway-cli autoresearch-doctor
```

If the sessions store is malformed or its owner entry lacks `sessionId`, the reset
command refuses; repair it manually from the timestamped backup.

## Auth recovery

`refresh_token_invalidated` means the auth profile needs recovery. Profile cooldown
self-heals when available; otherwise re-login to the OpenAI/Codex profile.
