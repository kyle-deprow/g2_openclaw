---
name: research-loop
description: Bounded, route-neutral research-owner contract for the native research loop.
---

# Research loop

This is an OpenClaw runtime skill. It is not a Codex/G2 implementation skill.
It describes the route-neutral contract only; it does not deploy or prove an
installed runtime. The reversal text below is a research proposal and operating
contract, not evidence of alpha or a claim that a current-price ETF feed is
available.

## Canonical roles and loop

- Astra is the research owner.
- Native Luna is the implementer and runner.
- Claude Code Opus via ACP is reviewer-only.
- OpenAI/Codex remains the provider; no fallback or route switching.
- Rule: hypothesis equals iteration; each attempt is code → review → run.
- Use maximum three attempts; Astra explicitly chooses FINISH, ABANDON, or PAUSE.
- Every attempt outcome, refusal, and terminal state returns to Astra.

## First campaign: conditional short-term reversal

- The requested panel window is 2021-10-01 through 2026-07-31 inclusive (the
  first XNYS session is 2021-10-01). This window is an input bound, not a claim
  that 2026 is a fresh final holdout; exposure and chronological split evidence
  must establish any holdout.
- The initial capability is price-panel-only and limited to liquid, unlevered
  equity-sector ETFs. Eligibility does not remove earnings exposure. Admission
  requires dated platform evidence for the universe; cache availability or a
  retrospectively successful ticker list is not universe evidence.
- The first hypothesis tests whether an unusually negative market-adjusted
  open-to-close shock, normalized by earlier volatility, reverses over the next
  five trading sessions when temporary selling pressure rather than new
  information is plausible. Fit betas and scalers only on earlier data.
- Decide at the regular close, enter at the next regular open, hold long or
  cash, and exit by the fifth session. Do not credit the close-to-next-open
  rebound to the post-entry result. A holding horizon above five sessions is
  refused.
- Use a simple baseline and at most two predeclared volume or regime variants.
  Indicators are features, not independent alpha claims; correlated RSI,
  bands, and returns are not independent confirmations.
- Compare unconditional dip-buying, cash, and exposure-matched passive
  baselines. Report net costs and 2x/3x cost sensitivity, chronological fold
  results, drawdown, concentration, sample counts, block-aware uncertainty and
  nulls, and an overlap purge of at least five sessions. Keep an exposure
  ledger: prior exposed history is development evidence, never a fresh final
  holdout; unknown history blocks FINAL_HOLDOUT.
- News, Reddit, and regularized ML are optional incremental hypotheses only
  after baseline evidence and supported point-in-time inputs. Current
  admission remains ETF price-panel-only. Missing news is not evidence of no
  news, and historical final engagement or edited-post fields are leakage.
  Non-earnings news continuation, scheduled macro-event equity/rates ETF or
  futures strategies, and crypto funding/basis convergence are later
  hypotheses, not silently enabled runtime capability.
- A useful result requires nontrivial economic analysis and an explicit
  conclusion. Reject and inconclusive results count; smoke, API, process, or
  zero-trade success is not scientific evidence and must not be tuned into a
  profitability claim.

## Frozen specification

The immutable outer `HypothesisSpec` fields are `hypothesis_id`, `title`,
`spec_json`, `spec_sha256`, `panel_path`, `receipt_path`,
`evaluation_spec_path`, `evaluation_spec_sha256`, `panel_sha256`,
`receipt_sha256`, `max_attempts`, `base_commit`, `created_at`, `dividends_path`,
`dividends_sha256`, and `state`. The frozen hypothesis document fields are
`contract`, `mechanism`, `prediction`, `target`, `baseline`, `universe_rule`,
`features`, `entry_rule`, `exit_rule`, `position_sizing_rule`, `variants`,
`search_budget_evaluations`, `analysis`, `evaluation`, `training`, `purpose`,
`forward_label_sessions`, `purge_rule`, `primary_metric`, `minimum_evidence`,
`null_tests`, `reject_criteria`, `missing_data_rule`, `compute`, and
`deliverables`. Each feature declares `name`, `source`, `as_of_rule`, and
`lookback_sessions`; bounds include dates, minimum evidence, compute limits,
and relative deliverable paths.

## Admission, status, and readiness

- Admission consumes typed caller evidence: immutable spec/panel/receipt/
  evaluator digests, a trusted receipt, trusted ordered panel sessions, the
  exposure ledger, evaluator bounds, the supplied capability, and explicit
  operator policy. Missing, malformed, conflicting, or unsupported input is a
  typed refusal, never an inferred value.
- The capability is price-panel-only capability within ETF scope.
- Stock refusal without trusted point-in-time earnings coverage is mandatory; unknown or unavailable earnings fail closed.
- A holding horizon above five sessions is refused.
- Preserve typed admission refusals and trusted panel sessions as caller
  evidence. Use immutable receipts, ledger, and evaluator bounds for scientific
  evidence.
- Exposure counting uses an immutable, digest-matched ledger with non-overlap,
  known ranges, `history_unknown`, and `trial_count`. Classify exposure as
  NONE, KNOWN_EXPOSED, or UNKNOWN_HISTORY. Unknown history blocks FINAL_HOLDOUT;
  valid development work may record it. Never treat absent history as clean.
- Read existing research status/control surfaces only. Preserve typed admission
  refusals, policy-unset pause, exhausted-attempt completion, exact cancel,
  owner wake, and read-only main controls. Pause on a missing policy ceiling,
  unproven route, unresolved review, invalid input, or lost job. Do not clear a
  readiness pause autonomously. Invalid inputs remain refusals.
- Receipt artifact digests bind the exact bytes but do not attest a provider;
  native readiness reads model, effort, and role from the official rollout,
  while requested `fast` and observed `unknown` service tiers stay distinct.

## Review and runner contract

- Spawn only the approved native implementer and experiment runner.
- Reserve review evidence before the one ACP Opus review; the evidence and
  bundle are immutable.
- The ACK must bind the exact child task, run, mode, attempt, commit, and spec
  digest. Collect only a strict verdict bound to that bundle.
- Never retype a verdict or respawn after an unknown acknowledgement; never
  launch without operator policy and a proven route.
- A respawn after unknown acknowledgement is forbidden.
- An exact cancel rereads the current task and is sent at most once. Preserve a
  pending/unknown response and pause until correlation is resolved.
- Run only an admitted, committed worktree after review PASS. The runner does
  no-repair/no-retry, input substitution, evaluator substitution, or invented
  result. A failed or lost job remains terminal evidence for Astra. Completion
  of the final allocated attempt remains allowed even when new admission is
  exhausted.

For review collection, use the deployed canonical paths:
`--core-database /home/dev/.openclaw/state/openclaw.sqlite`,
`--acpx-sessions /home/dev/.openclaw/workspace/state/sessions`, and
`--claude-projects /home/dev/.claude/projects`. These are verified local paths,
not portable upstream defaults; model shells may not inherit them, so do not
infer or substitute paths, search arbitrary records, or repair a verdict. If
correlation remains unavailable, preserve the refusal.

The analysis stage uses `--scenarios /scenarios --inputs /inputs --out /stage/analysis`;
cost specs are at
`/inputs/evaluation-specs/{spec_id}.json`, not `/evaluation-specs`. The
canonical EvaluationSpecSet and RunPlan are host-attested control evidence,
not automatically mounted analysis inputs: do not invent mounts or arguments
or leak host paths. Frozen future specs, RunPlans, synthetic fixtures, and
reviewed analysis must agree with this container contract.

For a fresh hypothesis, author and freeze one immutable `EvaluationSpecSet`
manifest before review. Its regular, digest-bound evaluator files must share
the primary panel, universe, dates, horizon, and operator-policy bounds; only
explicit cost settings may differ. Submit a reviewed `RunPlan` that names the
set digest, one primary scenario, and each true Quantipy evaluator scenario.
Never manufacture cost variants by post-processing one retained trade file.
The runner executes scenarios sequentially and performs declared analysis only
after every scenario has produced its bound result, trades, and daily
artifacts. Historical H1 rows remain readable, but a fresh launch has no
argv-only or single-spec fallback.
Each scenario's target command must write to its own canonical
`run/scenarios/sNNN/targets-stage/targets.json`; do not use a shared run-root
target path or another scenario's stage.

## Scientific proof boundary

Trusted receipts, ledgers, panel sessions, and evaluator bounds are immutable
inputs. Synthetic tests prove only contract behavior. Smoke or synthetic data
does not prove installed readiness. Make no alpha claim from smoke or synthetic
data. Earnings freshness, live routing, child behavior, and campaign launch
readiness remain unproven here.
