# G2 OpenClaw Agent Configuration

This directory contains repo-managed bootstrap files for OpenClaw agent
identity. Deploy them with `bash scripts/push-openclaw-config.sh`.
After changing these runtime docs or skills, run that push command and restart
the OpenClaw gateway service so every configured workspace receives the update.

The push script copies `AGENTS.md`, `SOUL.md`, `TOOLS.md`, and `BOOTSTRAP.md`
to every configured agent workspace derived from
`gateway/openclaw_config/openclaw.json`. The `main` G2 interface agent lands in
the default OpenClaw workspace at `~/.openclaw/workspace`; `autoresearch-pm`
and audited stage-agent workspaces default to `~/.openclaw/workspace-{id}`
unless an explicit `.workspace` is configured. Autoresearch launches stages
through native Codex `spawn_agent` using `.codex/agents/*.toml`, not OpenClaw
`sessions_spawn`. The script does not copy or overwrite local
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

Stop the supervisor before preparing state. The campaign uses schema-v6 state. The separate
schema-v3 platform-readiness manifest writes to
`~/.openclaw/autoresearch/platform-readiness.json`. A live schema-v5 state, or
state missing `schema_version`, is incompatible and must be archived before
fresh schema-v6 initialization. Never migrate or overwrite incompatible state
in place. The state procedure writes and validates a temporary replacement
before archiving the old state.

```bash
(
  set -e
  state=/home/dev/.openclaw/autoresearch/quantipy-state.json
  tmp="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.XXXXXX)"
  trap 'rm -f "$tmp"' EXIT
  cd /home/dev/repos/g2_openclaw
  /home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-init-state \
    --readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --output "$tmp"
  if [ -e "$state" ]; then
    archive="${state}.incompatible.$(date -u +%Y%m%dT%H%M%SZ).archive"
    mv -- "$state" "$archive"
  fi
  mv -- "$tmp" "$state"
  trap - EXIT
)
```

The control command and supervisor already use that authoritative path. Run:

```bash
/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-next \
  /home/dev/.openclaw/autoresearch/quantipy-state.json
```

`autoresearch-next` is model-facing and read-only. Verification dispatch
attestation/provisioning and repeat successor persistence are supervisor-owned
during `run_once` before its corresponding wake. G2 start only enables that
supervisor; it neither mutates authoritative state nor sends a direct PM wake.
If the authoritative state reports a campaign stall, the supervisor remains
paused until an operator reviews it and runs
`autoresearch-acknowledge-campaign-review` with a 32-1024 character
acknowledgement. The PM must not clear the pause or touch G2.
There is no state-schema migration in the external-verification command. The
only legacy retry-receipt bootstrap accepted by schema-v6 state is the actual
schema-1 receipt for deterministic attempt `-v2`: it must bind exactly one
canonical initial `-v1` local-panel HTTP 404 artifact. After stopping the
supervisor and repairing the Quantipy API, invoke the operator command from the
human/Codex shell with the explicit capability:

```bash
cd /home/dev/repos/g2_openclaw && G2_OPENCLAW_OPERATOR_RETRY=1 \
  /home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-retry-external-verification \
  /home/dev/.openclaw/autoresearch/quantipy-state.json \
  --reason "Restarted the stale Quantipy API service and verified the panel route."
```

Each retry performs a bounded local AAPL/XNYS-session ZIP-contract probe. The
probe requires exactly one `AAPL` coverage ticker with exactly the
`2022-01-03` session, one requested/observed date range, valid Quantipy
hydration and export ordering, and every receipt/panel digest. The command
binds implementation and readiness identities, preserves every failure
artifact, and authorizes only the historical `-v2` bootstrap and one generic
`-v3` retry. It writes retry-receipt schema 2 and binds the complete ordered
canonical digest list for all prior verification artifacts. It rejects arbitrary
failures, malformed or reordered history, v4-and-later generic retries, and
PM/fixer invocations without the explicit capability. All state-schema changes
require archiving and fresh schema-v6 initialization; no general in-place
migration exists.

An owner-session stop first disables the supervisor, cancels only exact owner
tasks, then repeatedly rescans state-bound detached manifests until a bounded
quiescence interval proves that no pending manifest-only launch or running unit
remains. It stops only exact state-bound transient units and deletes the owner
session only after each stopped record seals
`operator_stopped`/exit 143/signal null and its unit and PID are inactive. For
detached units, `is-active` exit 3 requires exact
`loaded/inactive/dead` `show` evidence, while exit 4 requires exact
`not-found/inactive/dead` evidence. A
stopped pending `-v3` verification is not an external-panel retry. With the
supervisor stopped, a human/Codex operator may use
`G2_OPENCLAW_OPERATOR_INTERRUPTED_VERIFICATION_RECOVERY=1` and
`gateway-cli autoresearch-recover-interrupted-verification` only for the exact
sealed v1/v2 + schema-2-v3 topology; recovery writes an interruption receipt
that embeds and digests the complete immutable v3 retry receipt, plus
deterministic `-v4` authorization, without inventing a verification result.
Only `G2_OPENCLAW_OPERATOR_PLATFORM_RUNTIME_RECOVERY=1` and
`gateway-cli autoresearch-recover-platform-runtime` may subsequently authorize
`-v5`. It binds its own current probe/reason, re-attests the canonical runtime,
and validates the preserved v3/v4 chain independently.

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
the runner records the immutable decision receipt before beginning the next
iteration with fresh context.
An LLM-authored receipt never authorizes `INFRA_BLOCKED` or suspension.
Suspension remains explicit operator-owned readiness suspension only.
After G0 implementation and verification, `GATE_PASSED` requires a full-union
`COMPLETE` receipt cross-checked against runner-owned preflight identity and
counts and maps to non-suspending `INFRA_REPAIRED`. `REMEDIATION_REQUIRED` is
stage evidence only and maps to non-suspending `DISCARD`. Neither G0 outcome is
research memory. MemPalace retains only ALPHA_RESEARCH KEEP-family outcomes and
ALPHA_RESEARCH `DISCARD` outcomes backed by completed verification with
`status=PASS` and `tests_passed=true`.
Crashes, exhausted verification failures, consensus failures, and
infrastructure control-plane outcomes proceed without a memory write.

Then atomically resume the same schema-v6 state file:

```bash
(
  set -e
  state=/home/dev/.openclaw/autoresearch/quantipy-state.json
  resumed="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.XXXXXX)"
  trap 'rm -f "$resumed"' EXIT
  cd /home/dev/repos/g2_openclaw
  /home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-build-readiness \
    /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --quantipy-root /home/dev/repos/quantipy \
    --expected-quantipy-commit <full-quantipy-git-hash> \
    --xnys-calendar /home/dev/.openclaw/autoresearch/evidence/xnys-trading-calendar.json \
    --campaign-xnys-start 2022-01-03 \
    --campaign-xnys-end 2025-12-31
  /home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-resume "$state" \
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
Production artifact files passed to `autoresearch-submit-stage` must use the exact
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

Example submission command:

```bash
/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-submit-stage \
  /home/dev/.openclaw/autoresearch/quantipy-state.json artifact.json \
  --readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json
```

Model sessions never write the authoritative state file directly; the Codex
sandbox only permits writes to the model workspace and the stage inbox.
`autoresearch-advance` is reserved for the unsandboxed supervisor and operator.
The supervisor validates and applies accepted submissions from the inbox with
the runner's locked atomic persistence within one poll cycle. After
`autoresearch-submit-stage`, end the turn; do not poll for acceptance. The
submission acceptance check happens on the next supervisor wake; quote any
rejection verbatim then.

`autoresearch-submit-stage` rejects mismatched, missing, extra-key, stale-state, and
unwrapped files before state advance. The complete envelope file must be at most
64 KiB; compact the artifact rather than truncating it. `autoresearch-next` also
has a hard 32 KiB prompt budget and fails closed with an actionable error if
accepted state artifacts would exceed it.

Context packets distinguish methodology-failure discards from clean negatives:
BUG_SIGNAL or otherwise untrustworthy-evidence decisions go in
`contested_methodology_families` and invite one hardened revisit with explicit
defect/correction novelty evidence, while clean negative results remain in
`burned_theory_families` and stay off-limits.

Long verification, notebook, hydrate, and backtest commands must use the
detached launch mechanism (prepared run + schema_version 1 launch request from
a sandboxed session). NEVER execute `scripts/run-long-task.sh` in your session:
inside a sandboxed session the uid mapping makes root-owned control binaries stat
as nobody:nogroup, so the launcher's ownership pin always fails before it can
prepare or queue anything. Create the command file through the repo-owned helper,
which reads the schema-v1 stdin protocol and atomically creates a non-symlink 0600
JSON file with `O_EXCL`/`O_NOFOLLOW`:

```json
{"schema_version":1,"command":["bash","-lc","<non-secret command>"]}
```

The following launcher block is only the supervisor-side and human-operator path;
the PM/model session must prepare and submit the run as described below:

```bash
command_file=/home/dev/.openclaw/autoresearch/model-workspaces/command-inputs/<unique-command>.json
/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-create-command-file --output "$command_file"
/home/dev/repos/g2_openclaw/scripts/run-long-task.sh \
  --run-dir <absolute-run-dir> \
  --runs-root /home/dev/.openclaw/autoresearch/model-workspaces/long-runs \
  --manifest <absolute-manifest.json> \
  --command-file "$command_file"
```

The manifest stores `command_sha256`, `instruction_manifest_sha256`, and
`state_reference_sha256`; it never stores command arguments. The command input
is consumed exactly once, is not passed to `systemd-run`, and cannot be supplied
positionally. Do not pass API keys, tokens, passwords, client secrets, or
private keys as command arguments; use credential files, environment
references, or inherited authentication.

Prepare the immutable run with
`/home/dev/repos/g2_openclaw/.venv/bin/python -m gateway.autoresearch_runs
prepare-with-command-file --manifest <absolute-manifest.json> --run-dir
<absolute-run-dir> --runs-root
/home/dev/.openclaw/autoresearch/model-workspaces/long-runs --command-file
"$command_file"`. Ensure the inbox directory
`/home/dev/.openclaw/autoresearch/stage-inbox/launch-requests/` exists first with
`mkdir -m 700 -p`; it must be a non-symlink directory owned by the session user.
Then write the schema_version 1 launch request
`{"schema_version":1,"run_dir":"<absolute-run-dir>","runs_root":"/home/dev/.openclaw/autoresearch/model-workspaces/long-runs"}`
under a unique filename ending in `.json`, such as
`<run-name>-$(date -u +%Y%m%dT%H%M%S%N)-$$.json`; write a `.tmp` sibling with
mode 0600 and `mv` it into the inbox. The owner-only supervisor normally
launches it within about 60 seconds. Confirm in the same turn that it lands in
`accepted/` (not `rejected/`) before reporting the run as queued. Under low host
memory, the supervisor defers the launch and the request legitimately stays
pending in the inbox; treat a still-pending request as deferred and re-check it
on the next wake. Report a blocker only if it is rejected or remains pending
after several wakes. If rejected, quote `rejected/<request-name>.reason`
verbatim when present, else quote the supervisor advisory log line.

The detached worker uses a `MemoryHigh=48G` soft limit and a `MemoryMax=64G`
hard limit. These limits apply only to the long-running research command and
are separate from the OpenClaw gateway's own native-crash containment limits.

## Quantipy Typed Runtime Verification

Implementation must commit one canonical `quantipy-experiment-v2` manifest in
its disposable workspace and record its absolute path and SHA-256 in
`implementation_result`. Verification runs focused tests, then
launches this exact command through the detached launch mechanism (prepared run + schema_version 1 launch request from a sandboxed session):

```bash
env PYTHONDONTWRITEBYTECODE=1 uv --directory /home/dev/repos/quantipy run --frozen --no-sync quantipy experiment run "$manifest" --output-root "$root" \
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
false. Process success is not research validity: PASS requires detached
`succeeded`/exit 0 plus complete alpha evidence (metrics, coverage, and paired
receipts) after all four stages. Under this rule, successful execution with
anomalous or missing alpha evidence is BUG_SIGNAL: it may truthfully carry the detached
`succeeded`/exit-0 four-stage evidence when focused tests passed, but it never
counts as PASS and routes to the fixer. TEST_FAILURE remains invalid after a
successful Quantipy run because focused test failure prevents runtime execution
in the required command order. TEST_FAILURE and an ordinary failed BUG_SIGNAL
use typed detached `failed`/exit 1 as applicable, with no signal and ordinary
`process_error` classification. Timeout, operator stop, resource exhaustion,
artifact/capture failure, signals, exit 2+, and other outcomes are not accepted
as Quantipy contract exits.

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
focused tests prevent execution, runtime evidence is `null` and a
strict `quantipy_execution_not_started` receipt must bind the exact failed
command/evidence, manifest, deterministic expected run ID/path, and allowed
reason. The entire expected run directory must be absent; validation atomically
reserves it with a private identity-bound tombstone. A retry must use the new
deterministic run ID produced by a new implementation/fix commit.

Never run both preparation procedures for the same campaign. Archive
incompatible state before initialization.
