---
name: quantipy-data-contract
description: Runtime data-access and point-in-time contract for Quantipy autoresearch stages.
version: 1.0.0
---

# Quantipy Data Contract

Load this skill for every Quantipy context, debate, consensus, implementation,
verification, review, and fix stage. The runner-provided platform-readiness
receipt is the capability authority. Do not rediscover capabilities by probing
providers, databases, cached symbols, or environment configuration.

## Universe Selection

- Select a historical universe only with `qp.security_universe_screen()` and
  `qp.security_universe_history()`.
- Before an experiment, prewarm each explicit selection or rebalance date once
  with `qp.security_universe_screen()`. Then call
  `qp.security_universe_history()` for those dates. History is strict,
  all-or-nothing, and cache-only; missing coverage is a blocker, not a reason to
  substitute another universe.
- Each history request allows at most 32 dates, 1,000 members per date, and
  10,000 total date-member slots. The profile member limit must not exceed
  1,000.
- For a full-range schedule that exceeds one request, sort and deduplicate the
  dates, then partition them into deterministic contiguous batches in that
  order. Each batch must satisfy all three limits, using the profile member
  limit to budget date-member slots. Call
  `qp.security_universe_history()` exactly once per batch, not once for the
  entire over-limit schedule and not once per date. Preserve batch order and
  fail the full schedule if any batch fails.
- At consensus, freeze only the universe plan/profile identity: profile ID,
  profile digest, sorted selection schedule, maximum members per date,
  and `execution_policy`. These are the canonical inputs; do not store
  redundant batch boundaries. The runner mechanically derives deterministic
  contiguous boundaries from them. Per-batch contract digests plus snapshot,
  grouped-daily, and member-union materialization identities and digests belong
  only in verification receipts after the history batches run.
- Liquidity screens use explicitly unadjusted grouped-daily data. A selection
  known from date D's completed summary follows the
  `next-session-or-later` policy. The verification receipt's calendar digest
  must exactly equal the XNYS evidence digest pinned by readiness, and
  `earliest_execution_date` must equal the first actual parsed XNYS session
  after D. A weekend, holiday, same-day, or merely later date is invalid.
- Preserve Quantipy's exact source receipt shapes. A security snapshot receipt
  has `as_of_date`, `source`, `result_count`, `identity_digest`,
  `content_digest`, and `completed_at`; it has no `adjusted` field. A grouped
  summary receipt has `summary_date` plus the same five evidence fields and
  literal `adjusted=false`. Never rename either date to generic `date`.
- The member-union digest is SHA-256 over canonical bytes: trim symbols,
  uppercase them, remove duplicates, sort ascending, encode as UTF-8, and join
  with one `\n` after every symbol, including the final symbol. Persist only
  the count and digest in state, plus an absolute external union-manifest path
  and SHA-256 receipt. The manifest itself must contain exactly those
  canonical bytes so reviewer execution can recompute both values without a
  ticker array in state.
- Historical `security_types` filtering is point-in-time certified. Use
  `security_types=("CS",)` when common-stock membership matters.
- Market cap and shares from ticker detail are not point-in-time certified.
  Market cap is not a supported historical screen. Do not impose a historical
  market-cap band or claim cap-verified universe compliance.
- Never infer eligible securities from locally cached price symbols, sentiment
  coverage, current listings, or a manually supplied ticker sleeve.

## Prices And Actions

- During implementation prewarm, load research OHLCV only with `qp.prices()`. It hydrates the requested
  selected-symbol/date range into the platform cache and returns from that
  cache only after strict coverage succeeds. Reuse that cache across folds and
  iterations; do not query repositories or the database directly. The committed
  v2 experiment stages are client-free and consume the runtime-owned verified panel;
  they must not call `qp.prices()` or perform external data loading.
- For `DATA_INFRA_G0`, create `platform_coverage_validation` only through
  Quantipy's shared `qp.validate_dynamic_price_coverage` validator. The native
  receipt is `dynamic-price-coverage-v1` / `price-coverage-v1` at regular-hours
  `1min` and includes source request identity/provider plus
  `member_union_digest`, `requested_sessions_digest`, `pit_active_roster_digest`,
  and `source_price_coverage_response_digest`. The price hydration receipt must
  carry that required source digest from the actual Quantipy
  `PriceCoverageResponse`; it is not the hydration `coverage_receipt_digest`
  metadata digest. A canonical receipt digest is not independent provenance and
  never authorizes suspension. The G2 runner recomputes Quantipy's compact
  JSON-array member-union digest from the verified universe manifest, separately
  requires universe/hydration newline-manifest digests to match, and treats
  `pit_active_roster_digest` as intrinsic Quantipy receipt data rather than
  independently reproducible PIT identity. The runner cross-checks a `COMPLETE`
  receipt against runner-owned preflight identity and counts before
  `INFRA_REPAIRED`.
  `full_union_hydration` proves every union
  member/session was checked: hydrated sessions equal union count times
  requested sessions, and inactive union sessions equal hydrated minus active
  sessions for both scopes. Scope selects asserted upstream count semantics while
  both geometries remain in the receipt. Provider-empty inactive union sessions
  are valid, not violation codes; `unexpected_session_count` counts distinct
  unexpected dates. Never substitute `pit_active_roster` as full-union proof or
  self-author a receipt. `REMEDIATION_REQUIRED` is stage evidence only: it
  proceeds to review and then non-suspending `DISCARD`, never `INFRA_BLOCKED`.
  Actual suspension is explicit operator-owned readiness suspension only. A
  missing paired universe, hydration, or platform receipt, or any scope,
  contract, provenance, or digest mismatch is the exact
  `platform_coverage_contract_mismatch` `BUG_SIGNAL` for `fixer`, not
  infrastructure evidence.
- Before any ALPHA verification command that can call `qp.prices()` for a
  hydrate/backtest, compute the planned `member_union_count * XNYS
  session_count` scope. The G2 OpenClaw control-plane hard budget is 600,000
  symbol-sessions. Over-budget experiments must return a structured
  `BUG_SIGNAL` named `price_hydration_scope_exceeds_budget` before hydration,
  not silently launch a multi-day Massive/cache fill.
- Hydrate only symbols selected by the universe contract and only the required
  experiment range. Do not manually prefetch every date range or a broad symbol
  catalog.
- Price bars returned by `qp.prices()` use the platform's adjusted price path.
  Do not apply splits or dividends to those bars a second time.
- Use `qp.corporate_actions()` when split or dividend event facts are required.
  Preserve its exact completed-range coverage semantics.

## Unsupported Data

Historical trades, quotes, and fundamentals are unavailable. Provider-direct
access, direct SQL, repository reads, and cache-derived universes are outside
the research contract. A hypothesis requiring unsupported data must be rejected
or reported as an exact operator-owned infrastructure blocker; it must not be
approximated with another field.

## Prompt Hygiene

Do not place full ticker arrays in prompts, transcripts, historical logs, or
MemPalace. Persist or reference compact universe receipts, dates, screen
criteria, counts, digests, and artifact paths. A small illustrative symbol
sample is acceptable only when needed to explain a result and is not the
authoritative universe.

Do not ask the runner to inject full instruction files. Treat the compact
`instruction_source_manifest` as the deterministic dispatch identity and return
the exact bound `source_manifest_sha256` in the `instruction_manifest_sha256`
envelope field around the structured artifact. Read the listed canonical files
when their current methodology content is needed, but do not fail a stage solely
because a mutable live state, readiness manifest, or methodology file changed
after dispatch. The runner, not the stage agent, performs the authoritative
persisted-state and envelope validation before accepting artifacts. Missing
required content, unwrapped envelopes, extra envelope keys, or oversized
artifact envelopes remain invalid.
