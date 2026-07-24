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

Stop the supervisor before preparing state. Before any `autoresearch-next`, use
exactly one procedure. The campaign uses schema-v2 state. The separate
schema-v3 platform-readiness manifest writes to
`~/.openclaw/autoresearch/platform-readiness.json`. Each state procedure writes
a temporary file, validates the command result, and atomically replaces the
authoritative state path.

```bash
# Losslessly migratable schema-less pristine state only.
(
  set -e
  state=/home/dev/.openclaw/autoresearch/quantipy-state.json
  tmp="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.XXXXXX)"
  trap 'rm -f "$tmp"' EXIT
  cd /home/dev/repos/g2_openclaw
  uv run gateway-cli autoresearch-migrate-state "$state" --output "$tmp"
  mv -- "$tmp" "$state"
  trap - EXIT
)

# New campaign, or after archiving an incompatible historical state.
(
  set -e
  state=/home/dev/.openclaw/autoresearch/quantipy-state.json
  tmp="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.XXXXXX)"
  trap 'rm -f "$tmp"' EXIT
  cd /home/dev/repos/g2_openclaw
  uv run gateway-cli autoresearch-init-state \
    --readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --output "$tmp"
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
`autoresearch-start-next` begins the next iteration with fresh context.
An LLM-authored receipt never authorizes `INFRA_BLOCKED` or suspension.
Suspension remains explicit operator-owned readiness suspension only, plus exact
legacy iteration-40 compatibility.
After G0 implementation and verification, `GATE_PASSED` requires a full-union
`COMPLETE` receipt cross-checked against runner-owned preflight identity and
counts and maps to non-suspending `INFRA_REPAIRED`. `REMEDIATION_REQUIRED` is
stage evidence only and maps to non-suspending `DISCARD`.

Then atomically resume the same schema-v2 state file:

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
strict production envelope and stay at or below 24 KiB:

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

Never run both preparation procedures for the same campaign. Archive
incompatible state before initialization.
