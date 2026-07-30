# Autonomous Research Loop - Operating Plan

**Created:** 2026-03-15
**Last Updated:** 2026-07-15

## Goal

Run a continuous Quantipy research pipeline in the dedicated
`agent:autoresearch-pm:autoresearch:quantipy` session. The PM curates context,
runs a five-agent debate, delegates experiment implementation and review,
advances structured artifacts through the deterministic runner, decides and
logs the outcome, and begins the next iteration. G2 `main` handles only explicit
human start/status/stop requests.

## Ownership

The PM orchestrates but does not edit Quantipy. Configured Codex stage agents
own experiment modules, notebooks, experiment-specific tests, and accepted
methodology work in disposable persisted worktrees. Shared Quantipy platform,
data loaders, harnesses, dependencies, runtime controls, readiness evidence,
and G2/OpenClaw infrastructure remain human/Codex operator owned. The PM never
promotes or repairs shared infrastructure.

Only `autoresearch-pm` has write-capable MemPalace access. All stage agents are
read-only. This boundary applies to implementation, recovery, and final logging.

## Architecture

```text
Human via G2
  -> OpenClaw main: explicit control handoff only
  -> agent:autoresearch-pm:autoresearch:quantipy
     -> deterministic runner and persisted state
     -> context_curator
     -> five parallel debaters
     -> consensus_arbiter (3-of-5 majority)
     -> implementer
     -> structured verification
     -> one high-reasoning reviewer
     -> fixer in the same worktree when directed
     -> PM decision, experiment log, required MemPalace write
     -> next iteration until explicit stop
```

Every research stage loads `mempalace-readonly`, `quantipy-methodology`, and
`quantipy-data-contract`. Methodology routing reads current Quantipy sources;
the compact data skill defines the runtime contract.

Phase 5 prompt compaction keeps instruction identity deterministic without
placing full instruction files in every prompt. The runner emits compact
`required_receipts`, a v2 `instruction_source_manifest` of `receipt_id`,
canonical absolute path, and SHA-256, plus `source_manifest_sha256`. The digest
is versioned, domain-separated, sorted by receipt ID, duplicate-rejecting, and
bound to the current phase, expected artifact type, ordered target agent IDs,
and canonical target repo root. Stage agents read every listed live source,
verify the hashes, and fail before work on missing, unreadable, or mismatched
files. No redundant manifest data is stored in campaign state.

## Platform Readiness

Before dispatch, the runner validates the operator-owned schema-v3
platform-readiness manifest at
`~/.openclaw/autoresearch/platform-readiness.json`. The separate live campaign
state remains schema-v4. Any live schema-v2 state must be archived and a fresh
schema-v4 state initialized before the supervisor restarts; in-place migration,
repair, or overwrite is forbidden. Readiness pins canonical manifest and snapshot
identities and verifies SHA-256 receipts for the Quantipy data contract and
authoritative XNYS calendar evidence. A `READY` manifest exposes the canonical
capability object injected into every stage prompt.

Control-plane validation reopens the pinned XNYS evidence, verifies its exact
readiness SHA-256, parses its declared range and closed dates into actual XNYS
sessions, and carries that digest/session context through artifact advance.
Every universe receipt must report that exact digest and set
`earliest_execution_date` to the first actual XNYS session after selection;
same-day, weekend, holiday, arbitrary later, and out-of-range dates fail closed.

Stages consume this receipt and do not probe providers, database contents,
cached symbols, or environment configuration to rediscover capabilities.
Existing state is initialized with `gateway-cli autoresearch-pin-readiness`.
After an operator changes a blocked or stale snapshot, rebuild readiness with
`gateway-cli autoresearch-build-readiness --campaign-xnys-start 2022-01-03
--campaign-xnys-end 2025-12-31` and then use
`gateway-cli autoresearch-resume` to atomically replace the suspended live
schema-v4 state with a resumed copy pinned to the new READY receipt. The
campaign starts after Reddit's `2021-12-31` availability boundary because the
configured rolling aggregate entitlement rejects January/July 2021 and supports
2022 onward. Building readiness strictly prewarms the campaign-start universe
screen and daily regular-hours AAPL prices through Quantipy's public client;
failure produces no READY receipt.

An explicit operator-owned readiness-precondition `INFRA_BLOCKED` decision
suspends without incrementing the iteration. The supervisor does not repeatedly
wake it. This branch sets `memory_write_required=false` and writes nothing to
MemPalace. No missing-schema or legacy G0 suspension shape is accepted.

## Quantipy Data Contract

- Historical universe selection uses only
  `qp.security_universe_screen()` and `qp.security_universe_history()`.
- Prewarm each explicit selection/rebalance date once with the screen call,
  then request those dates through cache-only, all-or-nothing history. One
  history request is limited to 32 dates, 1,000 members per date, and 10,000
  total date-member slots.
- For longer full-range schedules, sort and deduplicate dates and partition
  them into deterministic contiguous batches that each satisfy all three
  limits, budgeting slots from the profile member limit. Make exactly one
  `qp.security_universe_history()` call per batch, preserve batch order, and
  fail the full schedule if any batch fails.
- Consensus carries only canonical plan inputs: profile ID, profile digest,
  sorted schedule, maximum members per date, and `execution_policy`. Consensus
  stores no batch boundaries; the runner mechanically derives deterministic
  contiguous batch boundaries from those inputs. Per-batch contract digests
  plus per-date snapshot/grouped-daily and member-union materialization
  identities and digests are recorded only at verification. Stage prompts and
  memory use these compact artifacts rather than full ticker arrays.
- Universe liquidity summaries are explicitly unadjusted. Membership known
  from date D may execute only in the next market session or later.
- Source receipts match Quantipy exactly. Snapshot proof is
  `as_of_date/source/result_count/identity_digest/content_digest/completed_at`
  with no adjustment field. Grouped-summary proof uses `summary_date`, the same
  evidence fields, and literal `adjusted=false`; neither uses generic `date`.
- Canonical member-union bytes are uppercase normalized symbols, sorted and
  deduplicated, UTF-8 encoded, with each symbol followed by LF (`\n`), including
  an explicit trailing LF after the final symbol. SHA-256 of those bytes is the
  member-union digest. State stores no ticker array: it stores count/digest and
  an absolute external union-manifest path plus SHA-256 receipt. Reviewer
  execution reopens that manifest, verifies its file SHA, requires exact
  canonical bytes, and recomputes the count and digest.
- Historical `security_types` filtering is point-in-time certified; use
  `security_types=("CS",)` when common-stock membership matters.
- Market cap and shares are not point-in-time certified and market cap is not a
  historical screen criterion.
- Research OHLCV uses only `qp.prices()`, which hydrates selected-symbol ranges
  into the platform cache before reading them. Reuse cache coverage across
  folds and iterations; hydrate only the needed selected symbols and range.
- Price bars from the platform adjusted-price path must not receive a second
  split/dividend adjustment. Event facts use `qp.corporate_actions()`.
- Historical trades, quotes, and fundamentals are unsupported. Provider-direct
  access, direct database reads, cache-derived universes, and manually fixed
  symbol sleeves are outside the contract.

## Research Methodology

`ALPHA_RESEARCH` evaluates intraday equity strategies. Each proposal defines a
receipt-backed historical universe, signal rationale, prediction and holding
horizon, position sizing, transaction costs, time-aware train/CV/OOS split,
null tests, and rejection criteria. It uses real platform OHLCV, simple
defensible features, optional sentiment, broad common-calendar coverage, and an
untouched OOS holdout.

`DATA_INFRA_G0` repairs data/provenance/fold construction and produces an
explicit `GATE_PASSED` or `REMEDIATION_REQUIRED` result. It cannot claim alpha
performance. Burned alpha theory families require materially new evidence.

Every new debate submission and implementation result includes a structured
`compute_fit`:

- `target`: `none`, `cpu`, `gpu`, or `mixed`.
- `rationale`: fit to hypothesis and data scale.
- `required_dependencies`: JSON list, empty for `none`.
- `benchmark_plan`: planned wall-time, memory, or acceleration measurement.

GPU or mixed execution requires runner-proven GPU/CUDA access and every
declared dependency. Agents do not install dependencies, fabricate evidence,
or change the declared device path. Missing required capability is an exact
operator-owned infrastructure blocker.

## Stage Roster

| Agent | Responsibility |
|-------|----------------|
| `main` | G2 control interface only |
| `autoresearch-pm` | State orchestration, final decisions, experiment logging, PM-only MemPalace writes |
| `context_curator` | Readiness/universe receipts, baseline, recent results, research log, read-only MemPalace context |
| `debater_microstructure` | Market mechanics theory |
| `debater_data` | Supported data, universe receipts, coverage, target construction |
| `debater_skeptic` | Leakage, overfit, unsupported assumptions, cherry-picking pressure |
| `debater_theory` | Statistical and finance rationale |
| `debater_implementation` | Buildability, compute fit, verification cost |
| `consensus_arbiter` | Majority decision and one implementation brief |
| `implementer` | Experiment code, tests, notebook, clean commit |
| `reviewer` | Single adversarial methodology review |
| `fixer` | Bounded accepted experiment fixes in the persisted worktree |

## Structured Verification

Every verification attempt ends in a complete JSON `verification_result`, even
when commands fail or expose a bug signal:

1. Run exact focused commands in the persisted implementation workspace.
2. Run `env PYTHONDONTWRITEBYTECODE=1 uv run quantipy experiment preflight MANIFEST`,
   then launch the exact `env PYTHONDONTWRITEBYTECODE=1 uv run quantipy experiment
   run MANIFEST --output-root ROOT --run-id
   autoresearch-i<iteration>-<commit12>` command through
   `scripts/run-long-task.sh`. Its immutable manifest must set
   `expected_artifact_path` to `ROOT/run-id/run.json`. Direct foreground
   execution cannot satisfy this contract. Under the non-malicious same-host
   agent model, PASS requires the worker-produced sealed attestation; a
   verifier claim cannot replace it. The detached worker must publish terminal
   success with complete EOF drain, truthful truncation metadata for each
   bounded 64 KiB retained log tail, and a secure expected-artifact
   size/SHA-256 attestation. Evidence binds the
   detached run directory/manifest digest and current `run.json` bytes to that
   worker attestation; the artifact's own hash cannot prove itself.
   `ROOT/run-id/run.json` is the known authoritative runtime receipt, and
   `ROOT` is the fixed private
   `/home/dev/.openclaw/autoresearch/quantipy-experiment-runs` root provisioned
   by state initialization as an owner-controlled mode-0700 non-symlink
   directory. Initialization creates or normalizes only the fixed user-owned
   `.openclaw` and `.openclaw/autoresearch` control-plane ancestors to mode
   0700 with no-follow directory descriptors; it never chmods `/home` or
   system ancestors and fails closed on foreign owners, symlinks, and an
   invalid existing `ROOT` mode.
   Verification dispatch validates the same fixed root before a run; there is
   no alternate root. Smoke and feasibility are the mandatory admission gate
   before model import/execution.
   Quantipy exits 0 iff `run.success=true` and 1 iff `run.success=false`.
   PASS requires detached success/exit 0. A typed rejected/failed envelope may
   advance TEST_FAILURE/BUG_SIGNAL only with detached failure/exit 1, no
   signal, ordinary `process_error`, and complete sealed artifact attestation;
   timeout, stop, resource, artifact/capture, signal, and other exit outcomes
   fail closed.
   Sealed mode 0400 artifact/status files and a mode 0500 detached run
   directory prevent ordinary verifier mutation; they do not protect against
   a malicious same-UID process or root/sudo-capable operator deliberately
   rebuilding the local record. That compromise is outside the local
   control-plane threat model. Intentional cleanup requires the operator to
   `chmod 0700 <exact-detached-run-dir>` and remove only that exact directory,
   never the runs root.
3. Capture decisive command, test, metric, compute-fit, and receipt evidence.
4. Capture per-batch contract, snapshot, grouped-daily, and member-union
   materialization identities and digests and bind them to the consensus
   plan/profile identity.
5. Set unavailable fields to `null`; never invent zero-valued metrics or
   coverage.
6. Persist the artifact with `gateway-cli autoresearch-advance` before prose,
   status, review, or handoff. The artifact file must be exactly
   `{"instruction_manifest_sha256": "<source_manifest_sha256 from autoresearch-next>",
   "state_reference_sha256": "<state_reference_sha256 from autoresearch-next>",
   "artifact": {...}}`;
   legacy unwrapped artifacts, missing digests, digest mismatches, and extra
   envelope keys fail before state advance. The local complete envelope is
   capped at 64 KiB so expanded universe receipts remain complete; the next
   action prompt is separately capped at 32 KiB. Compact artifacts rather than
   truncating them.
7. Route accepted fixes only to `fixer` in the same workspace, then repeat the
   structured verification/review sequence directed by the runner.

`PASS` requires passing tests, no bug signals, complete required metrics, and
consistent readiness and universe receipts. `ALPHA_RESEARCH` coverage is only
the compact `DynamicUniverseCoverageReceipt`; legacy per-symbol
`CoverageReceipt` and aggregate `AggregateCoverageReceipt` are explicitly
`DATA_INFRA_G0`-only. Nonzero tests are `TEST_FAILURE`; impossible, leaky, or
internally inconsistent metrics are `BUG_SIGNAL`.

Implementation records the absolute canonical committed
`quantipy-experiment-v2` manifest path and SHA-256. `PASS` additionally needs
`quantipy_experiment_evidence` matching that manifest/commit, deterministic
run ID, `run.json` digest, all four completed ordered stages, and panel
identity/digests when requested. A typed failed/rejected run is retained for
`TEST_FAILURE` or `BUG_SIGNAL`. When execution never started, runtime evidence
is `null` only alongside a strict `quantipy_execution_not_started` receipt
binding the manifest, deterministic expected run ID/path, exact failed
command/evidence, and reason `focused_tests_failed` or `preflight_failed`; the
expected run directory must be absent. Validation atomically reserves that
directory with a private identity-bound tombstone, preventing a later run from
reusing the ID. Retry after a new implementation/fix commit provides a new
deterministic commit-bound run ID. Requested panels may omit evidence only for
typed pre-stage preflight, panel, or filesystem failures; otherwise their
nested typed receipt and bound panel/receipt files are mandatory.
Raw notebook execution (`nbconvert`, `papermill`, or Jupyter) may smoke-test or
render a report but never substitutes for runtime verification.

Manifest-relative provenance follows Quantipy CLI exactly: package and
notebook paths resolve from the manifest parent, and stage paths resolve below
that package. Every local Python source file under the full package root must
be tracked at the implementation commit and exact-byte identical; ignored,
untracked, symlinked, mutable, or group/world-writable source content is
rejected. Generated `__pycache__` directories, `*.pyc`, and non-source runtime
artifacts are ignored by source provenance. Quantipy preflight reads the full
package Python tree once and execution imports only those approved immutable
in-memory bytes; no `run/source` tree is authoritative or retained. The run
envelope binds the uniquely path-ordered file list, each exact byte size and
SHA-256, total bytes, and the `quantipy-experiment-source-v1` domain-separated
aggregate digest. G2 reconstructs that complete inventory from Git blobs at
the implementation commit and requires exact equality, so a dirty execution
followed by workspace restoration cannot pass. G2 mirrors Quantipy's strict
8 MiB canonical run-envelope cap. Secure committed snapshots allow 1 MiB for
each source file and 8 MiB for the notebook. Within the run envelope, source
evidence is limited to 256 ordered Python files, 1 MiB per file and 8 MiB
total; stage summaries are
limited to 4096 characters, failure messages to 2048, identity paths to 4096,
and the nested or standalone panel receipt to 4 MiB.

The shared G2/Quantipy panel wire contract is receipt
`research-price-panel-receipt-v2` over the unchanged request
`research-price-panel-v1`. Receipt coverage is the exact seven-key
`price-coverage-compact-v1` object with
`canonical-json-zlib-base64-v1`: strict canonical base64, bounded zlib decode
without an unbounded flush, exact size/ratio/digest checks, EOF/no trailing data,
and strict canonical JSON before the normal full coverage validation. G2 keeps
only compact coverage in normalized run evidence. Raw receipt bytes are bounded
to 4 MiB. Raw/canonical run envelopes must be strictly below 8 MiB; expanded
coverage is capped at 32 MiB, compressed coverage at 4 MiB, and compression
ratio at 200.

External verification retry is an explicit human/Codex operator infrastructure
operation, never PM authority. With `G2_OPENCLAW_OPERATOR_RETRY=1`, an operator
may retry only a strictly revalidated, attested local research-panel HTTP 413
pre-stage panel failure (`success=false`, `panel_requested=true`, `panel=null`,
no stage receipts). The prior artifact, implementation commit, and manifest must
all still match. The receipt is replaced, verification history is retained, and
the deterministic run ID advances from `-v2` to `-v3` and onward only through
`-v9`. Missing, running, successful, or methodology failures are rejected; no
artifact is deleted or reused. The compatible state schema remains v4 and is not
silently migrated.

The sole accepted legacy bootstrap is retry-receipt schema 1 at deterministic
attempt `-v2`, with exactly the initial `-v1` artifact and its canonical digest.
It exists only to materialize the already-attested live v2 HTTP 413 failure.
Every newly issued receipt uses retry-receipt schema 2 and, from `-v3` onward,
contains the complete ordered canonical digest list for every prior verification
artifact. Every validation recomputes and compares that list exactly: changing
any artifact field (including `null_test_summary`), or adding, removing, or
reordering history, fails closed. The initial `-v1` artifact must contain the
canonical byte-exact local HTTP 404 message (including ordered query and MDN
line); every sealed `-v2` through the immediately prior attempt must contain the
corresponding byte-exact manifest-bound HTTP 413. Each run ID is deterministic.

Before every operator retry, the local readiness probe invokes the same complete
panel-receipt semantic validator used for Quantipy run evidence. It accepts only
one `AAPL` ticker with exactly the `2022-01-03` session and `sessions=1`, the
bounded request and observed coverage ranges, correct digest bindings, and
hydration no later than export. Zero or multiple tickers/sessions, another
session, invalid ranges, or export before hydration fails closed.

For every new `DATA_INFRA_G0` envelope, include
`platform_coverage_validation` emitted by Quantipy's shared
`qp.validate_dynamic_price_coverage` validator; a self-authored JSON receipt
cannot prove infrastructure. The canonical receipt digest proves only
self-consistency and never grants suspension authority. The receipt includes
source request identity and provider plus `member_union_digest`,
`requested_sessions_digest`, `pit_active_roster_digest`, and
`source_price_coverage_response_digest`. The price hydration receipt must carry
the required `source_price_coverage_response_digest` from the actual Quantipy
`PriceCoverageResponse`; it is not the hydration `coverage_receipt_digest`
metadata digest. The receipt contract is
`dynamic-price-coverage-v1` over source contract `price-coverage-v1` and native
regular-hours `1min` data. G2 recomputes Quantipy's compact JSON-array
`member_union_digest` from the verified universe member-union manifest, while
separately requiring the established universe and hydration newline-manifest
digests to match each other. Full-union proof uses `full_union_hydration`, where
hydrated symbol-sessions equal `member_union_count * requested_session_count`
and inactive-union sessions are hydrated minus active sessions for both scopes.
Scope selects asserted upstream count semantics, while every receipt reports
both geometries. G2 keeps `pit_active_roster_digest` as an intrinsic Quantipy
receipt field but does not claim independent exact PIT roster identity, because
the compact universe receipt does not contain Quantipy's per-session ticker
arrays. A `pit_active_roster` receipt is not equivalent proof for the G0
full-union gate. Provider-empty inactive union sessions are valid and are not
violations. `unexpected_session_count` counts distinct unexpected dates.
The finite violation codes are
`unexpected_ticker`, `unexpected_symbol_session`, `missing_hydrated_symbol_session`,
`provider_empty_active_symbol_session`, and `missing_active_symbol_session`.
`GATE_PASSED` requires a `COMPLETE` receipt cross-checked against runner-owned
preflight identity and counts before non-suspending `INFRA_REPAIRED`.
`REMEDIATION_REQUIRED` requires matching nonempty violation codes, is stage
evidence only, and ends in non-suspending `DISCARD`. It cannot authorize
`INFRA_BLOCKED` or suspend the loop; suspension is explicit operator-owned
readiness suspension only. A missing paired universe, hydration, or platform receipt,
or any scope, contract,
provenance, or digest mismatch is the canonical `BUG_SIGNAL`
`platform_coverage_contract_mismatch`, with null infrastructure outcome,
rationale, and receipt, and routes to `fixer`.

The single reviewer checks implementation fidelity, receipt-bounded range and
universe use, leakage, overlapping holds, costs, tuning, null tests, OOS
independence, compute-fit consistency, and reproducibility.

## Decisions And Memory

The runner enforces this order for alpha work:

1. Exhausted test retries: `CRASH`.
2. Remaining critical review issue or max drawdown at least 30%: `DISCARD`.
3. Decision Sharpe at most -0.5: `DISCARD`.
4. Decision Sharpe above 1.0 with reviewer `PASS`: `STRONG KEEP`.
5. Decision Sharpe above 0.5: `SIGNIFICANT KEEP` or `STRONG KEEP`.
6. At or below 0.5, a numeric baseline is mandatory: improvement is
   KEEP-family; no improvement is `DISCARD`. Plain `KEEP` requires that numeric
   baseline.

In both `ALPHA_RESEARCH` and `DATA_INFRA_G0`, second-round `NO_CONSENSUS`
remains `NO_CONSENSUS`; it does not suspend, does not write MemPalace, and
`autoresearch-start-next` begins the next iteration with fresh context.
`INFRA_BLOCKED` and suspension are reserved only for explicit operator-owned
readiness suspension. Completed
`DATA_INFRA_G0` `REMEDIATION_REQUIRED` proceeds to review and non-suspending
`DISCARD`; `GATE_PASSED` with a runner-bound `COMPLETE` receipt maps to
`INFRA_REPAIRED`. The gate mapping never overrides consensus handling. Both
no-memory outcomes set
`memory_write_required=false`. For every other memory-required final decision,
the PM writes compact experiment, feature, model, metric, reviewer, decision,
failure, and receipt facts after the decision artifact is accepted. Full ticker
arrays are never stored in prompts, historical logs, or MemPalace. Canonical
decision receipts under the autoresearch state directory replace
the read-only historical `RESEARCH_LOG.md` as platform decision authority.

## Success Criteria

1. Every implemented alpha experiment completes structured verification and
   one adversarial review before its final decision.
2. Every stage uses pinned readiness and universe receipts instead of capability
   discovery.
3. Every accepted strategy is reproducible from committed experiment artifacts
   and compact data receipts.
4. The loop continues autonomously until an explicit human/Codex stop command.
5. The portfolio accumulates independently validated, orthogonal strategies
   rather than optimizing one result indefinitely.
