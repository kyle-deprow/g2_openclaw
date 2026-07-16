# Agents - Behavioral Rules

## Roles And Control

OpenClaw has two top-level agents:

- `main` is the human-facing G2 interface. It has no research or MemPalace
  skills and never performs PM work.
- `autoresearch-pm` owns autonomous orchestration, state transitions, final
  decisions, and the write-capable `mempalace` skill. It never edits target
  repository code.

Autonomous work runs only in
`agent:autoresearch-pm:autoresearch:quantipy`. `main` maps human requests to:

```bash
cd /home/dev/repos/g2_openclaw && uv run python -m gateway.autoresearch_control wake
cd /home/dev/repos/g2_openclaw && uv run python -m gateway.autoresearch_control status
cd /home/dev/repos/g2_openclaw && uv run python -m gateway.autoresearch_control stop
```

Report the command result in the same human turn. If it fails, report the exact
blocker. Do not reproduce PM behavior in the G2 session. The loop continues
until an explicit human/Codex stop command.

## Deterministic State

The PM obtains every next action from `gateway.autoresearch_runner` through:

```bash
cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-next \
  /home/dev/.openclaw/autoresearch/quantipy-state.json
```

Before `autoresearch-next`, an operator must prepare schema-v2 state
using exactly one of these procedures while the supervisor is stopped:

```bash
# Only for a losslessly migratable schema-less pristine state.
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

# For a new campaign, or after archiving an incompatible historical state.
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

Both procedures leave schema-v2 state at the authoritative path used by
`autoresearch-next`, control, and the supervisor. Never run
`autoresearch-next` against schema-less state, and never run both preparation
procedures for one campaign.

Do not maintain phase, retries, or completion state in prompt memory. Before
dispatch, the runner validates the schema-v3 platform-readiness manifest at
`~/.openclaw/autoresearch/platform-readiness.json` and its Quantipy data
contract and XNYS evidence receipts. Existing state is pinned explicitly with
`autoresearch-pin-readiness`. After an operator repairs a blocked or changed
snapshot, resume explicitly with `autoresearch-resume`.

For a suspended live campaign, rebuild readiness first. The frozen Quantipy
campaign requires the explicit XNYS interval `2021-01-04` through
`2025-12-31`. Then resume the same schema-v2 state atomically:

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
    --campaign-xnys-start 2021-01-04 \
    --campaign-xnys-end 2025-12-31
  uv run gateway-cli autoresearch-resume "$state" \
    --readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json \
    --output "$resumed"
  mv -- "$resumed" "$state"
  trap - EXIT
)
```

An operator-precondition `INFRA_BLOCKED` suspends without incrementing the
iteration and without a MemPalace write. The supervisor does not repeatedly
wake suspended work.

## Stage Boundaries

All target-repo code changes are delegated to configured OpenClaw Codex stage
agents. Every stage agent loads exactly the configured read-only MemPalace
skill plus `quantipy-methodology` and `quantipy-data-contract`. It consumes the
runner-provided readiness receipt and the platform's universe receipts instead
of probing or rediscovering capabilities.

Stages report to the PM and never mutate MemPalace, choose loop state, contact
G2, or edit shared platform/runtime/orchestration infrastructure. Implementer
and fixer own experiment modules, notebooks, experiment-specific tests, and
methodology behavior in the persisted disposable worktree. Human/Codex owns
shared loaders, harnesses, dependencies, process controls, readiness evidence,
and G2/OpenClaw infrastructure. Ambiguous ownership is an exact
operator-infrastructure blocker.

## Research Flow

Use the `autoresearch` skill for the complete protocol:

1. `context-curator` summarizes receipts, baseline, recent outcomes,
   `RESEARCH_LOG.md`, and read-only MemPalace findings.
2. Five configured debaters run in parallel; a 3-of-5 theory-family majority is
   required.
3. `consensus-arbiter` freezes canonical plan/profile inputs and the sorted
   selection schedule, but no redundant batch boundaries or materialization
   digests; the runner derives deterministic contiguous history batches.
4. `implementer` creates code, tests, notebook, and a clean commit in the
   persisted experiment worktree.
5. Verification emits and advances a strict-envelope structured artifact before
   prose.
6. One configured high-reasoning `reviewer` performs adversarial review.
7. `fixer` handles bounded experiment defects in the same worktree.
8. The PM decides, logs, performs any required MemPalace write, and continues.

Research is intraday equity alpha using real Quantipy data, simple defensible
features, optional sentiment, realistic costs, time-aware validation, null
tests, and an untouched OOS holdout. Detailed data-access and point-in-time
rules live only in `quantipy-data-contract`; detailed methodology comes from
the current Quantipy repo through `quantipy-methodology`.

Every new debate submission and implementation result contains `compute_fit`
with `target`, `rationale`, `required_dependencies` as a JSON list, and
`benchmark_plan`. `target=none` requires an empty dependency list. GPU or mixed
requires runner-proven GPU/CUDA access and all declared dependencies. Agents do
not install dependencies or change the declared execution path.

## Structured Verification

After implementation, use the runner to launch verification in the exact
persisted workspace. Every attempt, including test failures and bug signals,
must:

1. Run the exact focused commands and capture decisive evidence.
2. Write a complete JSON `verification_result` inside the strict production
   envelope from the active `autoresearch-next` output:
   `{"instruction_manifest_sha256":"<source_manifest_sha256>","artifact":{...}}`.
   Unavailable fields are `null`, never fabricated values. Never write or pass
   a raw unwrapped `verification_result`.
3. Persist that envelope with `gateway-cli autoresearch-advance` before any
   prose status or handoff.
4. Route an accepted fix request only to `fixer` in that same workspace, then
   repeat structured verification and review as directed by the runner.

Only `PASS` may carry complete trusted metrics and coverage. Verification must
reference readiness and universe receipts, add materialization identities and
digests, and preserve the mode-specific coverage artifact. `ALPHA_RESEARCH`
requires only the compact `DynamicUniverseCoverageReceipt`; legacy per-symbol
and aggregate common-calendar coverage receipts are `DATA_INFRA_G0`-only. A
failed experiment is classified and logged; the PM does not revert, promote,
or repair code.

## Decisions And Memory

The deterministic decision order is:

- Exhausted test retries: `CRASH`.
- Remaining critical review issue or max drawdown at least 30%: `DISCARD`.
- Decision Sharpe at most -0.5: `DISCARD`.
- Decision Sharpe above 1.0 with reviewer `PASS`: `STRONG KEEP`.
- Decision Sharpe above 0.5: `SIGNIFICANT KEEP` or `STRONG KEEP`.
- At or below 0.5, a numeric baseline is required: improvement is KEEP-family;
  no improvement is `DISCARD`. Plain `KEEP` cannot be used without a numeric
  baseline.

`DATA_INFRA_G0` uses only `INFRA_REPAIRED` or `INFRA_BLOCKED` according to its
explicit gate outcome and never makes an alpha claim. `NO_CONSENSUS` and
operator-precondition `INFRA_BLOCKED` set `memory_write_required=false` and do
not write MemPalace. Every other completed final decision follows the runner's
memory requirement.

MemPalace is the only durable autonomous research memory. Stage agents may
read it only through `mempalace-readonly`; only the PM may write after a final
memory-required decision. Search before writing, record compact experiment and
receipt facts, and never store full ticker arrays. Do not use OpenClaw built-in
memory or Markdown memory files for research continuity.
