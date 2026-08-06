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

## In-sandbox user-systemd bus failure

Signature:

```text
Failed to connect to user scope bus via local transport: Operation not permitted
No data available
```

This is expected when a stage agent runs inside its bubblewrap sandbox. The
launcher prepares the run, queues a request under
`stage-inbox/launch-requests/`, and prints `LAUNCH_QUEUED: <run-dir>`; that line
is success. Do not retry or classify it as a blocker. The unsandboxed
supervisor launches the prepared run on its next cycle, normally within about
60 seconds, and ordinary supervision wakes the owner session.

## Detached long-run root migration

The detached-run root changed from
`/home/dev/.openclaw/autoresearch/runs` to
`/home/dev/.openclaw/autoresearch/model-workspaces/long-runs`. Before the next
campaign inspection, stop the autoresearch supervisor and move the old root's
contents into the new root (or create an operator-managed symlink), preserving
ownership and private modes. This is an operator migration only; the gateway
does not modify either root automatically.

## Launch-request inbox quarantine

`stage-inbox/launch-requests/accepted/` accumulates requests whose prepared
runs were successfully handed to systemd. `rejected/` accumulates malformed,
unsafe, or launcher-failed requests. Inspect only the immediate entries and
their metadata; do not recursively follow symlinks or treat a request filename
as proof that its run started. For inspection, list immediate files under each
child directory and use `stat` on the exact paths before opening any JSON.

To clear a poisoned inbox safely, stop both
`quantipy-autoresearch-supervisor.service` and
`openclaw-gateway.service` first. Inspect and archive or remove only the exact
quarantined entries, then restore the inbox and child directories to owner-only
mode 0700 before restarting the services. Never recursively delete or chmod the
inbox root. A rejected request leaves its prepared-but-unlaunched run directory
behind; remove that exact run directory before re-preparing the same attempt
path, because `prepare_run` refuses an existing directory.

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
