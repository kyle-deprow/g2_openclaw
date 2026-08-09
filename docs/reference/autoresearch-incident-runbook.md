# Autoresearch incident runbook

Terse recovery notes for the Codex app-server and autoresearch owner session.

## Data API

If prewarm reports an `httpx.ConnectError` while connecting to `127.0.0.1:8000`, restart the independent data-plane unit `quantipy-api.service` with `systemctl --user restart quantipy-api.service`.

## Pause/resume the autoresearch supervisor

The gateway runtime-caps drop-in declares
`Upholds=quantipy-autoresearch-supervisor.service`. While the gateway is up,
manually stopping the supervisor will therefore be undone by systemd. To pause
autoresearch, stop the gateway too, or mask the supervisor before stopping it;
unmask the supervisor before resuming it.

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

## Detached verification timeout or interruption

If a detached ALPHA verification run started, reaches terminal `FAILED`, and
has `failure_classification=timeout` (or another non-operator interruption),
preserve the exact run directory, manifest, status, and complete sealed output
capture. The verification artifact may carry `status=TEST_FAILURE` with
`quantipy_execution_interrupted`, bound to the expected run ID/path, manifest
digests, status digest, exit/signal/classification, timeout and wall time, and
stdout/stderr capture digests. The supervisor can build and auto-advance this
artifact from the terminal run record; do not hand-edit state or fabricate a
not-started receipt.

The resulting `fix_test` round must rescope the experiment specification and
timeout. Do not relaunch the identical manifest. `OPERATOR_STOPPED` remains on
the operator recovery path and is not valid interrupted evidence.

## Runaway-spec signature

Signature: repeated detached runs exit 143 and report
`failure_classification: timeout`, with no `run.json`. Read
`projected_model_seconds` against the manifest `timeout_seconds`, and compare
`encoded_feature_columns` with the training-row count. The fix round rescopes
the experiment; no operator action is needed beyond letting the bounded fix
machinery run.

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
