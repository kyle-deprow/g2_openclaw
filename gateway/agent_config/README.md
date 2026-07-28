# G2 OpenClaw Agent Configuration

This directory contains repo-managed bootstrap files for OpenClaw agent
identity. Deploy them with `bash scripts/push-openclaw-config.sh`.
After changing these runtime docs or skills, run that push command and restart
the OpenClaw gateway service so every configured workspace receives the update.

The push script copies `AGENTS.md`, `SOUL.md`, `TOOLS.md`, and `BOOTSTRAP.md`
to every configured agent workspace derived from
`gateway/openclaw_config/openclaw.json`. The `main` G2 interface agent lands in
the default OpenClaw workspace at `~/.openclaw/workspace`; `autoresearch-pm`
and stage agents default to `~/.openclaw/workspace-{id}` unless an explicit
`.workspace` is configured. The script does not copy or overwrite local
workspace files such as `USER.md`, `IDENTITY.md`, or personal notes.

## Files

- `SOUL.md` — Role-aware identity: G2 interface, autonomous PM, and stage boundaries.
- `AGENTS.md` — Operational rules: G2 handoff, PM loop, stage isolation.
- `TOOLS.md` — Tool usage guide for OpenClaw Codex runtime and built-in tools.
- `BOOTSTRAP.md` — Project context: tech stack, repo layout, conventions.
- `skills/quantipy-methodology/` — Stage-specific routing to current Quantipy
  instructions.
- `skills/quantipy-data-contract/` — Compact runtime data-access,
  point-in-time, receipt, and prompt-hygiene contract.

## Load Order

OpenClaw reads bootstrap files in this order: AGENTS → SOUL → TOOLS → BOOTSTRAP.
Later files can reference concepts from earlier ones.

## Session Key

The Gateway uses `agent:main:g2` as the base session key for human G2
interactions. Autonomous research runs only in
`agent:autoresearch-pm:autoresearch:quantipy`.

## State Preparation

Stop the supervisor before preparing state. The campaign uses schema-v3 state. The separate
schema-v3 platform-readiness manifest writes to
`~/.openclaw/autoresearch/platform-readiness.json`. A live schema-v2 state, or
state missing `schema_version`, is unsupported. Archive it and initialize a
fresh schema-v3 state before restarting the supervisor; never migrate or
overwrite schema-v2 in place. The state procedure writes and validates a
temporary replacement before archiving the old state.

```bash
(
  set -e
  state=/home/dev/.openclaw/autoresearch/quantipy-state.json
  tmp="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.XXXXXX)"
  trap 'rm -f "$tmp"' EXIT
  cd /home/dev/repos/g2_openclaw
  uv run gateway-cli autoresearch-init-state \
    --readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --output "$tmp"
  if [ -e "$state" ]; then
    archive="${state}.schema-v2.$(date -u +%Y%m%dT%H%M%SZ).archive"
    mv -- "$state" "$archive"
  fi
  mv -- "$tmp" "$state"
  trap - EXIT
)
```

The control command and supervisor already use that authoritative path. Run:

```bash
cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next \
  /home/dev/.openclaw/autoresearch/quantipy-state.json
```

## Suspended Campaign Resume

If a live campaign is suspended on `INFRA_BLOCKED`, rebuild the schema-v3
platform-readiness manifest first. The frozen Quantipy campaign requires the
explicit XNYS interval `2022-01-03` through `2025-12-31`: Reddit begins
`2021-12-31`, while the configured rolling aggregate entitlement supports 2022
onward but rejects January/July 2021. The readiness build strictly probes the
campaign start through Quantipy's public `security_universe_screen` and daily
regular-hours `prices` APIs for `AAPL`. This may hydrate/cache data as the
intentional operator prewarm; a failed probe produces no READY receipt.

In both `ALPHA_RESEARCH` and `DATA_INFRA_G0`, second-round `NO_CONSENSUS`
remains `NO_CONSENSUS`; it does not suspend, does not write MemPalace, and
`autoresearch-start-next` persists the immutable decision receipt, then begins
the next iteration with fresh context.
An LLM-authored receipt never authorizes `INFRA_BLOCKED` or suspension.
Suspension remains explicit operator-owned readiness suspension only.
After G0 implementation and verification, `GATE_PASSED` requires a full-union
`COMPLETE` receipt cross-checked against runner-owned preflight identity and
counts and maps to non-suspending `INFRA_REPAIRED`. `REMEDIATION_REQUIRED` is
stage evidence only and maps to non-suspending `DISCARD`.

Then atomically resume the same schema-v3 state file:

```bash
(
  set -e
  state=/home/dev/.openclaw/autoresearch/quantipy-state.json
  resumed="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.XXXXXX)"
  trap 'rm -f "$resumed"' EXIT
  cd /home/dev/repos/g2_openclaw
  uv run gateway-cli autoresearch-build-readiness \
    /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --quantipy-root /home/dev/repos/quantipy \
    --expected-quantipy-commit <full-quantipy-git-hash> \
    --xnys-calendar /home/dev/.openclaw/autoresearch/evidence/xnys-trading-calendar.json \
    --campaign-xnys-start 2022-01-03 \
    --campaign-xnys-end 2025-12-31
  uv run gateway-cli autoresearch-resume "$state" \
    --readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --output "$resumed"
  mv -- "$resumed" "$state"
  trap - EXIT
)
```

The next-action output includes compact `required_receipts`, a v2
`instruction_source_manifest`, and `source_manifest_sha256` instead of full
instruction contents. The digest is versioned, domain-separated, sorted by
receipt ID, duplicate-rejecting, and bound to the phase, expected artifact
type, ordered target agent IDs, and canonical target repo root. Before writing
a stage artifact, use the dispatch `source_manifest_sha256` and
`state_reference_sha256` from `autoresearch-next`; read live source files when
their current methodology rules are needed, not as a mutable freshness gate.
Production artifact files passed to `autoresearch-advance` must use the exact
strict production envelope and stay at or below 64 KiB. This local artifact
budget accommodates complete expanded universe receipts; the separate
`autoresearch-next` prompt remains capped at 32 KiB:

```json
{
  "instruction_manifest_sha256": "<source_manifest_sha256>",
  "state_reference_sha256": "<state_reference_sha256>",
  "artifact": {}
}
```

Example advance command:

```bash
cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-advance \
  /home/dev/.openclaw/autoresearch/quantipy-state.json artifact.json \
  --readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json \
  --output /home/dev/.openclaw/autoresearch/quantipy-state.json
```

The in-place output is serialized by the runner's state lock and published with
an atomic replace. Do not move a shell-created empty temp file over the
authoritative state after `autoresearch-advance` fails.

`autoresearch-advance` rejects mismatched, missing, extra-key, stale-state, and
unwrapped files before state advance. The complete envelope file must be at most
64 KiB; compact the artifact rather than truncating it. `autoresearch-next` also
has a hard 32 KiB prompt budget and fails closed with an actionable error if
accepted state artifacts would exceed it.

Long verification, notebook, hydrate, and backtest commands must use
`scripts/run-long-task.sh` with an immutable run manifest and a one-time private
command input file. Create the command file through the repo-owned helper, which
reads the schema-v1 stdin protocol and atomically creates a non-symlink 0600
JSON file with `O_EXCL`/`O_NOFOLLOW`:

```json
{"schema_version":1,"command":["bash","-lc","<non-secret command>"]}
```

Invoke the launcher from this repo with:

```bash
command_file=/home/dev/.openclaw/autoresearch/command-inputs/<unique-command>.json
uv run gateway-cli autoresearch-create-command-file --output "$command_file"
/home/dev/repos/g2_openclaw/scripts/run-long-task.sh \
  --run-dir <absolute-run-dir> \
  --manifest <absolute-manifest.json> \
  --command-file "$command_file"
```

The manifest stores `command_sha256`, `instruction_manifest_sha256`, and
`state_reference_sha256`; it never stores command arguments. The command input
is consumed exactly once, is not passed to `systemd-run`, and cannot be supplied
positionally. Do not pass API keys, tokens, passwords, client secrets, or
private keys as command arguments; use credential files, environment
references, or inherited authentication.

The detached worker uses a `MemoryHigh=20G` soft limit and a `MemoryMax=24G`
hard limit. These limits apply only to the long-running research command and
are separate from the OpenClaw gateway's own native-crash containment limits.

## Quantipy Typed Runtime Verification

Implementation must commit one canonical `quantipy-experiment-v2` manifest in
its disposable workspace and record its absolute path and SHA-256 in
`implementation_result`. Verification runs focused tests, then
`env PYTHONDONTWRITEBYTECODE=1 quantipy experiment preflight MANIFEST`, then
launches this exact command through `scripts/run-long-task.sh`:

```bash
env PYTHONDONTWRITEBYTECODE=1 quantipy experiment run "$manifest" --output-root "$root" \
  --run-id "autoresearch-i<iteration>-<commit12>"
```

The immutable detached manifest must set `expected_artifact_path` to the known
`$root/$run_id/run.json`. Direct foreground execution cannot satisfy this
contract. Under the non-malicious same-host agent trust model, PASS requires
the worker-produced sealed attestation; a verifier claim cannot replace it.
Before publishing terminal success, the detached
worker securely snapshots that expected artifact and records its path, size,
SHA-256, and file identity in schema-v5 `status.json`. It seals the artifact
and terminal status mode 0400 and the detached run directory mode 0500. G2
requires the detached run directory and manifest digest, successful terminal
status, complete EOF drain with truthful truncation metadata for each bounded
64 KiB retained log tail, and an exact match between the current `run.json`
bytes and the worker attestation. The hash supplied in
`quantipy_experiment_evidence` is not sufficient by itself.

Quantipy exits 0 exactly when `run.success=true` and 1 exactly when it is
false. PASS requires detached `succeeded`/exit 0. A typed rejected or failed
run for TEST_FAILURE/BUG_SIGNAL requires detached `failed`/exit 1, no signal,
and ordinary `process_error` classification. Timeout, operator stop, resource
exhaustion, artifact/capture failure, signals, exit 2+, and other outcomes are
not accepted as Quantipy contract exits.

These permissions prevent ordinary verifier mutation; they are not
cryptographic protection against a malicious process with the same UID or a
root/sudo-capable operator rebuilding the local record. That compromise is
outside the local control-plane threat model. For intentional cleanup only,
the operator may run `chmod 0700 <exact-detached-run-dir>` and then remove that
exact run directory; never recursively chmod or delete the runs root.

`gateway-cli autoresearch-init-state` provisions `$root` at the fixed
`/home/dev/.openclaw/autoresearch/quantipy-experiment-runs` path as an
owner-controlled mode-0700 non-symlink directory. It also creates or
normalizes the fixed user-owned `.openclaw` and `.openclaw/autoresearch`
control-plane ancestors to mode 0700 through no-follow directory descriptors;
it never chmods `/home` or system ancestors. Provisioning fails closed on a
foreign owner or symlink and on an invalid existing `$root` mode. Verification
dispatch validates the same fixed root before a run. There is no alternate root.
`$root` is the runner-declared fixed private autoresearch runs root, and
`$root/$run_id/run.json` is the only full runtime proof. Its digest, manifest
binding, complete immutable execution-source file inventory/digests, four
ordered completed receipts, and mandatory requested-panel receipt/files go in
`quantipy_experiment_evidence`. G2 compares the full source inventory and
domain-separated aggregate digest with exact Git blobs at the implementation
commit; Quantipy retains no authoritative `run/source` directory. Smoke and
feasibility must complete before model import/execution. `nbconvert`,
`papermill`, and Jupyter
may smoke-test or render a report only; they never substitute for a PASS. When
focused tests or preflight prevent execution, runtime evidence is `null` and a
strict `quantipy_execution_not_started` receipt must bind the exact failed
command/evidence, manifest, deterministic expected run ID/path, and allowed
reason. The entire expected run directory must be absent; validation atomically
reserves it with a private identity-bound tombstone. A retry must use the new
deterministic run ID produced by a new implementation/fix commit.

Never run both preparation procedures for the same campaign. Archive
incompatible state before initialization.
