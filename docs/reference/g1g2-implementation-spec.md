# G1 + G2 Loop-Governance Implementation Spec

**Date:** 2026-08-03 · **Status:** approved · **Parent:** `autoresearch-refactor-plan.md` §3 (G1, G2) · **Prepared by:** design review against the post-refactor `gateway/autoresearch/` package.

**Baseline facts:** live state is `schema_version=4, phase=fix_test, iteration=1` at `~/.openclaw/autoresearch/quantipy-state.json`; `AUTORESEARCH_STATE_SCHEMA_VERSION = 4` (`gateway/autoresearch/constants.py:31`); `MAX_NEXT_ACTION_PROMPT_BYTES = 32*1024`, `NEXT_ACTION_PROMPT_TARGET_BYTES = 31*1024` (`constants.py:49-52`).

## 0. Design invariants (all conservative / fail-closed)

| # | Invariant | Why |
|---|---|---|
| I1 | The registry is **append-only evidence**, never a completeness proof. `_validate_state` checks internal consistency only, never "every past iteration has an entry". | Many tests hand-construct `phase=REPEAT` states; a completeness invariant would break them and make freshly initialized mid-campaign states unloadable. |
| I2 | The novelty gate is an **acceptance-time gate**, not a state invariant: it runs only in the `Phase.CONSENSUS` branch of `advance_state` and reads only entries with `iteration < state.iteration`. | If it were a load invariant, the entry appended at `DECISION_LOG` would match its own consensus and permanently brick the state. |
| I3 | No new `Phase` member. | A new phase ripples into `PHASE_RECEIPTS`, `_select_phase_target`, `ARTIFACT_CONTRACTS`, decision receipts, and the SKILL.md loop diagram; `campaign_review` needs none of that. |
| I4 | `suspended`/`suspension_reason` semantics untouched; campaign review gets an orthogonal flag pair. | `_validate_state` hard-requires `suspended ⇒ INFRA_BLOCKED`; a stall follows a normal DISCARD/NO_CONSENSUS. |
| I5 | One schema bump (v5) for all of G1+G2, landed in phase A. | The live state file is re-initialized exactly once. |
| I6 | Counters are **stored and cross-checked** against a registry-derived recomputation. | Matches the codebase's redundant-mechanical-check habit. |

## 1. Hypothesis registry

### 1.1 New module `gateway/autoresearch/governance.py`
Below `state.py` in the dependency order; imports `constants`, `enums`, `errors`, `fields`, `artifacts` only. `state.py` imports it; `transitions.py`/`prompts.py` consume it.

### 1.2 Fingerprint
`ConsensusResultArtifact` carries no horizon field; the horizon component is the universe plan's selection-date span. `theory_family_fingerprint(consensus, mode) -> str` (64-hex sha256), domain-separated like `build_authoritative_state_reference`, over canonical JSON of:

| Key | Source | Normalization |
|---|---|---|
| `version` | `HYPOTHESIS_FINGERPRINT_VERSION = "g2-openclaw-autoresearch-hypothesis-fingerprint-v1"` | literal |
| `research_mode` | `state.mode.value` | literal |
| `family` | `consensus.winner_theory_family` | `_normalise_identifier` (same normalizer as `burned_theory_families`) |
| `profile_id` | `universe_plan.profile_id` | validated kebab-case |
| `profile_digest` | `universe_plan.profile_digest` | validated sha256 |
| `selection_span` | `[dates[0], dates[-1], len(dates)]` | dates validated sorted-unique |
| `max_members_per_date` | `universe_plan.max_members_per_date` | int |

Excluded: `execution_policy` (single-constant, zero entropy); the full `selection_dates` list (span+count is coarser — the fail-closed direction). Fingerprint is `None` for NO_CONSENSUS and operator-precondition consensus.

### 1.3 `HypothesisRegistryEntry`
Frozen slots dataclass: `iteration`, `research_mode`, `consensus_status`, `decision`, `family | None`, `contested_families: tuple[str,...]`, `fingerprint | None`, `metric_value | None`, `reason` (runner-derived, ≤160 chars), `novelty_delta_sha256 | None`. Exact-key from_dict (all 10 required); validate() enforces the shape rules incl. `family is None` iff NO_CONSENSUS; `contested_families` ≤ 2, sorted-unique, empty for MAJORITY.

### 1.4 `contested_families`
For NO_CONSENSUS: normalized `vote_family` values from the final debate round receiving ≥2 of 5 votes (first mechanical use of `DebateSubmission.vote_family`). At most 2 members by construction.

### 1.5 `reason`
`" ".join(final_decision.log_summary.split())[:160]` — runner-owned, deterministic, no model writes.

### 1.6 `CampaignCounters`
`consecutive_non_keep`, `consecutive_no_consensus`, `iterations_since_last_keep`. Derived by `derive_campaign_counters(registry, *, acknowledged_through_iteration)`: consecutive counters consider entries after the acknowledgement baseline; `INFRA_BLOCKED`/`INFRA_REPAIRED` are **counter-neutral** (neither increment nor reset); `iterations_since_last_keep` counts all entries and ignores the baseline. `_validate_state` recomputes and compares (`"campaign_counters do not match the hypothesis registry"`).

### 1.7 State fields
Appended to `AutoresearchState`: `hypothesis_registry`, `campaign_counters`, `campaign_review_required: bool`, `campaign_review_reason: str | None`, `campaign_review_history: tuple[CampaignReviewRecord,...]` (cap 32). `CampaignReviewRecord`: `triggered_iteration`, `reason`, `counters`, `acknowledgement | None`, `acknowledged_iteration | None`.

New `_validate_state` checks: registry iterations strictly increasing; `entry.iteration <= state.iteration` (`<` unless REPEAT); `len(registry) <= MAX_HYPOTHESIS_REGISTRY_ENTRIES (512)` else hard stop ("archive the campaign and initialize a fresh state"); counters cross-check; `campaign_review_reason` non-null iff flag; flag ⇒ REPEAT with final_decision.

### 1.8 Serialization
All five keys required in `from_dict`/`to_dict` — the v4 conditional-key-removal pattern is NOT extended; v5 is a clean break. Registry entries become part of the state digest and decision receipts automatically.

### 1.9 Write points
- `advance_state` DECISION_LOG branch: build entry (consensus + debate + decision), append, recompute counters, evaluate stall (phase C), single `replace(...)` validated by the existing `_validate_state` call.
- `lifecycle.suspend_for_infrastructure`: appends a counter-neutral INFRA_BLOCKED entry.
- **Critical:** `lifecycle.start_next_iteration` and `resume_suspended_iteration` construct fresh `AutoresearchState(...)` — they must explicitly carry all five fields or the registry silently resets every iteration. Dedicated carry-forward test required.

### 1.10 Schema version — decision
**Bump to 5. No migration path.** Keep the load-refusal, update its message to name v5 and `autoresearch-init-state`. Operator procedure: stop supervisor → archive state file → init → pin readiness → restart. Rationale: (1) auto-migration breaks persisted-state byte-equality checks and would mutate a 0600 file outside lock discipline; (2) an empty-registry migration is fail-OPEN (falsely claims no prior trials); (3) live file is iteration 1 — nothing is lost; the no-migration contract is documented and test-enforced. Land while the loop is stopped (deployment already incomplete).

## 2. Novelty gate

### 2.1 `ConsensusResultArtifact.novelty_delta: str | None = None`
Required key, nullable value, in exact-keys/to_dict/`ARTIFACT_CONTRACTS` (`"novelty_delta|null"`). validate(): NO_CONSENSUS ⇒ None; when present 32–1024 chars stripped.

### 2.2 Enforcement
`_validate_consensus_novelty_gate(state, artifact)` in `transitions.py`, called in the CONSENSUS branch of `advance_state` after the round check, before consensus history is built — strictly before implementation dispatch. Skipped for NO_CONSENSUS and operator-precondition consensus.

### 2.3 Matching rule
`delta_required` iff Tier 1 (fingerprint equality with a prior DISCARD/CRASH/NO_CONSENSUS entry) or Tier 2 (winner family ∈ a prior NO_CONSENSUS entry's `contested_families`). **Documented non-rule:** same family + different universe plan after DISCARD does NOT require a delta (that is G3's multiple-testing territory; `family_trial_counts` derivation ships as substrate). Outcomes: required+missing → reject; delta hash equals any prior `novelty_delta_sha256` → reject; not-required+present → reject (keeps the field a signal); else accept and store the hash on the DECISION_LOG entry.

### 2.4 Error contract
Lowercase, specific-cause, stable prefixes:
```
consensus winner_theory_family '<family>' repeats iteration <n> <DECISION>; set novelty_delta explaining what changed
consensus winner_theory_family '<family>' repeats the iteration <n> NO_CONSENSUS deadlock; set novelty_delta explaining what changed
consensus novelty_delta duplicates the iteration <n> delta verbatim
consensus novelty_delta is only valid when the theory family repeats a prior non-KEEP outcome
```

### 2.5 Fixture impact
Zero existing fixtures change (all consensus artifacts in tests are dataclass-constructed; no raw-dict sites). New builders only.

## 3. Negative-results ledger

`_negative_results_ledger(state)` in `prompts.py` — pure registry read, no I/O. Entries with decision ∈ {DISCARD, CRASH, NO_CONSENSUS}, newest first, cap `NEGATIVE_RESULTS_LEDGER_MAX_ENTRIES = 12`, byte cap `NEGATIVE_RESULTS_LEDGER_MAX_BYTES = 2048` with whole-line oldest-first truncation and a `... <k> older negative results omitted` tail. Injected in `_phase_instruction` between the mempalace and operator-precondition segments, ONLY for CONTEXT_PACKET / DEBATE_RESULT / CONSENSUS_RESULT (implementation/verification prompts byte-unchanged); empty string when no qualifying entries (iteration-1 prompts byte-identical). Pipe-delimited fixed columns incl. `contested=` rows; inline rule statement mirroring §2.3. Existing prompt-budget checks remain the final bound.

## 4. Campaign counters + stall

### 4.1-4.3
Counters recomputed at exactly one place (DECISION_LOG transition; mirror in suspend_for_infrastructure). Thresholds as `CampaignGovernancePolicy` (`stall_consecutive_non_keep=8`, `stall_consecutive_no_consensus=3`) on `AutoresearchPolicy`; optional strictly-validated `agents.defaults.autoresearchCampaignGovernance` config block (absent → defaults; no deployment coupling). Stall predicate at DECISION_LOG; on trip: set flag, deterministic reason string, append `CampaignReviewRecord`.
```
campaign stalled: <n> consecutive non-KEEP iterations (threshold <N>)
campaign stalled: <n> consecutive NO_CONSENSUS iterations (threshold <M>)
```

### 4.4 Mechanism — orthogonal flag (decision)
Rejected: reusing `suspended` (would relax three fail-closed invariants incl. the changed-readiness resume requirement); new Phase (I3). Enforcement points: `engine.next_action` raises (PM-side pause naming the recovery command); `lifecycle.start_next_iteration` raises; `AutoresearchSupervisor.run_once` short-circuits `NO_ACTION/"campaign_review_pending"` before any wake/finalization/dispatch.

### 4.5 Surfacing — status field, not a wake
No supervisor→G2 push path exists by design. Extend `ControlStatus` (+`suspended`, `suspension_reason`, `campaign_review_required`, `campaign_review_reason`, three counters) populated from already-loaded state; `g2_autoresearch_status` serializes it for free.

### 4.6 Operator resume — new CLI command (decision)
`gateway-cli autoresearch-acknowledge-campaign-review --acknowledgement "<32-1024 chars>"`, modeled on `autoresearch-resume`, calling `lifecycle.acknowledge_campaign_review`: requires flag+REPEAT; clears flag/reason; updates the trailing review record; recomputes counters with `acknowledged_through_iteration = state.iteration` (zeroes the consecutive pair, keeps `iterations_since_last_keep`). Deliberately NOT env-gated: touches no evidence, mints no recovery claim; the env-gated pattern is not extended (plan §5.8).

## 5. Protocol doc deltas (`gateway/agent_config/skills/autoresearch/SKILL.md`)
v8.1.0 → 8.2.0 at end of phase B; → 8.3.0 at end of phase C; phase A is doc-string-only (schema-v4 → schema-v5 mentions). Sections: Platform Readiness Preflight (v5 strings ×3); Research Modes (ledger consistency with `burned_theory_families`); Loop (+registry/stall line); Context Curator (ledger authoritative); Debate (`contested=` rows equally burned); Consensus (full `novelty_delta` arbiter contract); Decide And Log (registry append incl. NO_CONSENSUS/CRASH); Recovery And Status (stall semantics; PM must not clear it, must not touch G2). Companion files with test-enforced "schema-v4 state" strings: agent_config AGENTS.md/README.md, quantipy plan doc, cli.py docstring+output. Do NOT touch "schema-v5 `status.json`" (detached-run status — unrelated, test-pinned).

## 6. Test plan
New: `test_governance.py` (fingerprint determinism/normalization/None-cases, contested_families, counter derivation incl. infra neutrality both halves, entry round-trip/rejections, family_trial_counts), `test_novelty_gate.py` (11 cases incl. the documented non-rule and not-a-load-invariant), `test_campaign_stall.py` (thresholds, infra neutrality, blocked next_action/start_next_iteration, ack semantics, non-default policy, re-trip prevention). Changed: builders (+3 helpers), transitions v5-message + prompt-byte states, public-API map additions, artifacts contract coverage for CONSENSUS_RESULT, agent-config-docs and cli version strings, supervisor NO_ACTION case, control status case. Compatibility: v4-refusal test (match archive/re-init message), exact-key strictness for missing v5 keys, golden round-trip with populated registry + digest stability. Prompt-budget: 512-entry registry within target minus margin; implementation/verification prompts byte-identical with and without registry.

## 7. Phasing — three dispatches, smallest-risk-first
- **A — schema v5 + registry substrate** (model-invisible; prompt bytes proven identical to main; carry-forward test is the gate; land while the loop is stopped, fold re-init into the pending guarded deployment).
- **B — novelty gate + ledger** (first artifact-rejecting change; inert while registry is empty; SKILL 8.2.0; two clean iterations observed before C).
- **C — stall + campaign_review + acknowledgement** (can halt the loop; lands last; every enforcement point fails closed naming the recovery command; SKILL 8.3.0).
