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
     -> context-curator
     -> five parallel debaters
     -> consensus-arbiter (3-of-5 majority)
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
state remains schema-v2. Readiness pins canonical manifest and snapshot
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
`gateway-cli autoresearch-build-readiness --campaign-xnys-start 2021-01-04
--campaign-xnys-end 2025-12-31` and then use
`gateway-cli autoresearch-resume` to atomically replace the suspended live
schema-v2 state with a resumed copy pinned to the new READY receipt.

An operator-precondition `INFRA_BLOCKED` decision suspends without incrementing
the iteration. The supervisor does not repeatedly wake it. This branch sets
`memory_write_required=false` and writes nothing to MemPalace.

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
| `context-curator` | Readiness/universe receipts, baseline, recent results, research log, read-only MemPalace context |
| `debater-microstructure` | Market mechanics theory |
| `debater-data` | Supported data, universe receipts, coverage, target construction |
| `debater-skeptic` | Leakage, overfit, unsupported assumptions, cherry-picking pressure |
| `debater-theory` | Statistical and finance rationale |
| `debater-implementation` | Buildability, compute fit, verification cost |
| `consensus-arbiter` | Majority decision and one implementation brief |
| `implementer` | Experiment code, tests, notebook, clean commit |
| `reviewer` | Single adversarial methodology review |
| `fixer` | Bounded accepted experiment fixes in the persisted worktree |

## Structured Verification

Every verification attempt ends in a complete JSON `verification_result`, even
when commands fail or expose a bug signal:

1. Run exact focused commands in the persisted implementation workspace.
2. Capture decisive command, test, metric, compute-fit, and receipt evidence.
3. Capture per-batch contract, snapshot, grouped-daily, and member-union
   materialization identities and digests and bind them to the consensus
   plan/profile identity.
4. Set unavailable fields to `null`; never invent zero-valued metrics or
   coverage.
5. Persist the artifact with `gateway-cli autoresearch-advance` before prose,
   status, review, or handoff. The artifact file must be exactly
   `{"instruction_manifest_sha256": "<source_manifest_sha256>", "artifact": {...}}`;
   legacy unwrapped artifacts, missing digests, digest mismatches, and extra
   envelope keys fail before state advance. The complete envelope is capped at
   24 KiB, below the 32 KiB hard prompt budget; compact artifacts rather than
   truncating.
6. Route accepted fixes only to `fixer` in the same workspace, then repeat the
   structured verification/review sequence directed by the runner.

`PASS` requires passing tests, no bug signals, complete required metrics, and
consistent readiness and universe receipts. `ALPHA_RESEARCH` coverage is only
the compact `DynamicUniverseCoverageReceipt`; legacy per-symbol
`CoverageReceipt` and aggregate `AggregateCoverageReceipt` are explicitly
`DATA_INFRA_G0`-only. Nonzero tests are `TEST_FAILURE`; impossible, leaky, or
internally inconsistent metrics are `BUG_SIGNAL`.

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

G0 decides only `INFRA_REPAIRED` or `INFRA_BLOCKED` from its gate outcome.
`NO_CONSENSUS` and `INFRA_BLOCKED` set `memory_write_required=false` and do not
write MemPalace. For every other memory-required final decision, the PM writes
compact experiment, feature, model, metric, reviewer, decision, failure, and
receipt facts after the decision artifact is accepted. Full ticker arrays are
never stored in prompts, `RESEARCH_LOG.md`, or MemPalace.

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
