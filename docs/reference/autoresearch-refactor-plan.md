# Autoresearch Refactor & Improvement Plan

**Date:** 2026-08-01 · **Status:** proposed · **Scope:** the autoresearch loop control plane in this repo (`gateway/autoresearch_*`, `scripts/push-openclaw-config.sh`, loop skills/config). Quantipy itself is out of scope.

## 1. The generic autoresearch loop (external refresh)

Current literature and practice on autonomous research loops (AI-scientist systems, LLM agent loops, multi-agent trading research) converge on a common shape:

1. **Orient** — query system status, retrieve prior findings and failures from memory before proposing anything.
2. **Hypothesize** — produce *falsifiable* hypotheses with explicit testability criteria; single agents confirm their own ideas, so hypothesis generation uses complementary epistemic roles (innovator / pragmatist / contrarian) plus a synthesizer.
3. **Design** — isolate variables, pre-register the evaluation metrics and rejection criteria before running.
4. **Implement & Execute** — delegate to runner subagents; execution must be a reproducible bundle (code + data + environment pinned together).
5. **Evaluate & Review** — mechanical metrics plus adversarial peer-style review, distinct from the implementer.
6. **Decide & Memorize** — keep/discard against pre-registered gates; record outcomes *including negative results* so the loop does not re-try known failures.
7. **Loop governance** — explicit stopping conditions: iteration caps, cost/compute budgets, **no-progress detection**, and goal-achievement checks; failure modes documented systematically; the harness itself evolves from observed trial-and-error, not just the experiments.
8. **Human-on-the-loop** — auditable checkpoints and steering, without the human in the critical path of every iteration.

## 2. Where this repo already exceeds the state of the art

Scoring the repo against those principles honestly: most are not just met but exceeded.

- **Epistemic diversity** — five debate roles (theory, data, microstructure, skeptic, implementation) + arbiter with a mechanical 3-of-5 gate is richer than the 3-role debate in published systems, and the roles map cleanly onto the innovator/pragmatist/contrarian pattern with domain specificity.
- **Disconfirmation pressure** — a dedicated skeptic debater, an adversarial single reviewer separate from the implementer, and DISCARD-biased decision gates.
- **Pre-registration** — debaters must emit universe profile, walk-forward split, cost model, `compute_fit`, and rejection criteria *before* implementation; the runner enforces it structurally.
- **Reproducible execution bundles** — pinned runtime tuple, committed experiment manifests, detached sealed runs (0400 `run.json`), Git-blob source attestation, receipts for everything. This is stronger than anything in the surveyed literature.
- **Deterministic control flow** — phases, envelopes, and consensus counting live in Python, not prompts. Models exercise judgment only inside a mechanically enforced protocol.
- **Memory discipline** — no model writes memory; a state-derived finalizer persists qualifying outcomes; decision receipts are an immutable audit trail.
- **Human-on-the-loop** — G2 status/steer/stop via three scoped tools, with the PM barred from announcing into the human session.

## 3. Principle gaps — improvement plan

The gaps are all in **loop governance and learning**, not in rigor.

### G1. No-progress detection & novelty gating (HIGH)

Novelty exists today only as a model-reported `novelty_score` in the consensus artifact (validated for presence/type, not against history) plus the context curator's reading of receipts. Nothing mechanically stops re-proposing a near-duplicate of a prior failure, and NO_CONSENSUS/CRASH iterations never reach MemPalace at all.

- Add a **hypothesis registry** to loop state: theory-family fingerprint (normalized family + universe profile + target horizon hash) per iteration with its final decision.
- Consensus stage gains a mechanical **novelty gate**: an arbiter brief whose fingerprint matches a prior DISCARD/NO_CONSENSUS entry requires an explicit "what changed" delta field, else the runner rejects the artifact before implementation is dispatched.
- Feed a compact **negative-results ledger** (fingerprint, decision, one-line reason — derived from receipts, not model-written) into the context packet every iteration.

### G2. Campaign-level stopping conditions & budgets (HIGH)

Per-iteration budgets exist (600k symbol-sessions, prompt/artifact byte caps, 2 fix attempts, 1 debate retry); campaign-level governance does not.

- Add campaign counters to state: consecutive non-KEEP iterations, cumulative detached-run compute time, iterations since last KEEP.
- Define a **stall condition** (e.g. N=8 consecutive DISCARD/NO_CONSENSUS, or M=3 consecutive NO_CONSENSUS) that transitions the loop into a `campaign_review` state: loop pauses, supervisor emits a status summary for G2, human steers or acknowledges before resumption. This is the loop-engineering "no-progress exit" applied at the right altitude — the loop already never stops on its own, which the literature flags as an anti-pattern.

### G3. Multiple-testing control (MEDIUM — quant-specific)

Decision gates use raw Sharpe/MDD thresholds per experiment. Across a long campaign this is a multiple-hypothesis problem: enough trials will produce a Sharpe > 1.0 by chance.

- Track **trials per theory family** in the hypothesis registry (G1 provides the substrate).
- Report a deflated Sharpe (or trial-count-adjusted threshold) in the verification evidence, and give the reviewer the family trial count so "this is the 14th momentum variant" is visible at review time.
- Keep the decision gates as-is initially; tighten only after observing base rates.

### G4. Failure-mode post-mortems feeding the harness (MEDIUM)

`INFRA_BLOCKED`/`INFRA_REPAIRED` outcomes are handled fail-closed but their lessons live in commit messages and operator memory. The "self-evolving harness" principle wants each infrastructure failure to leave a durable artifact.

- On every infra suspension/repair, require a short structured post-mortem receipt (trigger, root cause, guard added) alongside the decision receipt.
- Fold the recurring ones into skills (`gateway/agent_config/skills/`) per the existing openclaw-improvement playbook — this formalizes what the human-proxy persona already does ad hoc.

### G5. Periodic human-readable research report (LOW)

Receipts are machine-authoritative but nobody reads JSON on glasses. Add a non-model report generator (state+receipts → markdown) the supervisor refreshes every N iterations or on campaign_review, surfaced through the existing G2 status path. The "manuscript" stage of AI-scientist systems, right-sized to this loop.

### Explicit non-adoptions (considered, rejected)

- **Adaptive debate rounds** (SPRT-style early consensus stopping) — saves tokens but injects nondeterminism into the loop's most audited stage; fixed 5+1-retry stays.
- **External literature retrieval for debaters** — deliberate closed-world design; the data contract and methodology docs are the debaters' authority. Revisit only as an operator-curated ingest, never live web access for stage agents.

## 4. Monolith refactor plan

Three monoliths carry the control plane: `gateway/autoresearch_runner.py` (13,719 lines), `scripts/push-openclaw-config.sh` (4,299 lines of transactional bash), `gateway/autoresearch_supervisor.py` (2,392 lines). All refactors are **mechanical moves, no behavior change**, with `uv run mypy` (already strict), `make test-gateway`, and `ruff check` green at every phase.

### 4.1 Runner → `gateway/autoresearch/` package

The runner decomposes cleanly along its measured concern boundaries into a layered package:

```
gateway/autoresearch/
    constants, errors, enums, fields          # leaves: limits, enums, scalar validators, digests, strict-JSON helpers
    secure_io, gitops                         # hardened file IO (L7676–8027), git helpers (L7188–7411)
    manifest, compute, policy                 # instruction manifests, compute-fit, policy/receipt catalog
    artifacts, receipts, recovery_receipts    # stage artifact dataclasses (~4,000 lines), incl. ARTIFACT_CONTRACTS
    state                                     # AutoresearchState (L5028–5288) + validation context
    evidence, attestation, workspace,         # quantipy run-evidence validation (L8141–9593), runtime attestation,
    transitions                               #   state-parameterized validators (phase gating, retry eligibility)
    prompts → engine → memory, lifecycle      # prompt construction, next_action/advance_state, memory derivation
    persistence → operator_recovery           # locking/atomic saves (L12966–13719), v3/v4/v5 recovery paths
```

Dependency direction is strictly downward. The one structural rule that prevents the state↔validator cycle: **dataclasses live below `state.py`; anything taking a `state:` parameter goes to `transitions.py` or above** — do not preserve current file adjacency. Preserve the existing `TYPE_CHECKING` seam with `autoresearch_runs.py`.

Migration order R0–R8 (S/M/L effort): R0 package scaffold + public-API snapshot test (S) → R1 leaves (M) → R2 io/git/manifest/compute (S) → R3 dataclasses + state, the big ~4,000-line cut (L) → R4 evidence/attestation/workspace/transitions (L, the cycle-risk phase — add an import-graph test) → R5 prompts/engine/memory/lifecycle (M; prompt-byte-budget tests pin exact output, pure moves only) → R6 persistence + operator recovery, runner becomes a <300-line re-export shim (M) → R7 split the 13,365-line test file into `tests/gateway/autoresearch/` mirroring the package, fixtures-first (L) → R8 migrate importers (`supervisor`, `cli`, `control`, `runs`, `decision_receipts`) to deep imports and delete the shim (S).

Extra gate on every phase: a golden-state fixture whose `to_dict()` JSON must be byte-identical before/after — serialization is security-relevant here.

### 4.2 Push script → `gateway/deployment/` package (Python port)

The P0–P4 port now places all deployment logic in `gateway/deployment/`; `scripts/push-openclaw-config.sh` retains the established orchestration flow, prerequisites, recovery directories, and trap/signal wiring. The guard-test suite (5,593 lines) remains **black-box** — fake `$HOME`, mock binaries, subprocess invocation, sqlite fault injection via `sitecustomize.py` — so its behavior contracts survive the implementation cutover.

**P0–P4 are complete.** The achieved end state is that `gateway/deployment/` owns the extracted deployment logic (`guarded_fs`, `identity`, `transactions`, `systemd_env`, `versions`, `config_merge`, `codex_agents`, `codex_db_repair`, `doctor`, and `auth_sync`), while `scripts/push-openclaw-config.sh` remains the Bash orchestrator for the established flow, prerequisites, recovery `mktemp` directories, and trap/signal wiring. Its switched-function wrappers now invoke Python by default; `OPENCLAW_PUSH_IMPL=bash` fails loudly as a removed implementation guard, and the full guard suite has proven the single remaining path.

Future work: replace the remaining Bash orchestrator with a script-to-exec wrapper and move signal handling into Python; this is deferred because the current cutover preserves the proven Bash trap and commit-boundary behavior while the signal-ownership change receives its own risk-controlled phase.

Frozen contracts during the port: exact stderr message strings (tests assert them), jq right-wins merge semantics byte-for-byte (published config hash is compared), rollback-on-any-failure with signals deferred during the final commit boundary.

### 4.3 Supervisor quick extractions (after R8)

Extract `OpenClawRPC`/`NativeGatewayRPC`/`WakeDeliveryProof` (~340 lines) into `autoresearch_rpc.py` (clean seam — `autoresearch_control.py` already imports across it); task reconciliation pure functions; checkpoint/recovery records. The `AutoresearchSupervisor` class (~1,190 lines) then shrinks to a loop orchestrator importing `gateway.autoresearch.engine`.

## 5. Code smells & quick wins (all small, independent)

1. Delete empty dirs: `gateway/agent_config/skills/mempalace/`, `copilot_bridge/`.
2. `logs/` at repo root collects runtime logs — confirm gitignore coverage and stop tooling writing into the repo root.
3. Centralize duplicated validators: `_require_string` re-implemented in `autoresearch_decision_receipts.py` and `autoresearch_platform_validation.py`, `_optional_float/_optional_int` duplicated between supervisor and runner → all import `gateway/autoresearch/fields` after R1.
4. `tests/gateway/test_autoresearch_refactor.py` is misnamed (it's artifact-contract coverage) — absorb into the split as `test_artifacts.py`. `test_autoresearch_supervisor.py` (2,841) and `test_cli.py` (2,618) are next-worst and split on the same pattern later.
5. `pyproject.toml` still names the project `azure-infra-cli` with an Azure Bicep description — rename standalone (touches lockfile/entry points).
6. `gateway/cli.py` carries 10 in-function lazy imports of the runner — consolidate after R8.
7. `gateway/openclaw_config/.env` holds a live OpenRouter API key in plaintext (gitignored but on disk) — rotate or move to a secret store.
8. Generalize the fossilizing recovery-version pattern: v2→v5 each minted a bespoke env-gated operator command for one exact failure topology. After the runner split, design a single parameterized operator-attested retry mechanism (same receipt rigor, one code path) so the next novel failure doesn't require a new command. (Design-level; not part of the mechanical refactor.)

## 6. Sequencing

1. **Now:** §5 quick wins + push-script P0 (heredoc extraction) — a week-scale batch, zero behavior risk, immediately shrinks the bash monolith by a third.
2. **Runner package:** R0–R3, then R4–R6, then R7–R8. Push-script P1–P2 can run in parallel (different files, different suites).
3. **Push-script P3–P4** — complete; the remaining script-to-exec wrapper and Python-owned signal handling are deferred follow-up after the proven cutover.
4. **Supervisor extractions** after R8.
5. **Principle work (§3)** rides on the refactored package: G1 hypothesis registry and G2 campaign counters are `state.py`/`transitions.py`/`engine.py` changes plus SKILL.md updates — far safer to land after the split than inside the monolith. G4/G5 are supervisor-side and can land any time after the supervisor extractions.
6. Throughout: this refactor must not race the pending live deployment (`.archive/OPENCLAW_DEPLOYMENT_STATUS.md`) — complete the guarded deployment and the two-iteration observation window on the *current* code first, so the refactor lands against a known-good operational baseline.

## Effort summary

| Track | Phases | Effort |
|-------|--------|--------|
| Quick wins + P0 | §5, P0 | ~1 week |
| Runner package | R0–R8 | 3–4 weeks, mechanical, test-green per phase |
| Push-script port | P0–P4 | Complete; final script-to-exec wrapper and Python-owned signal handling deferred |
| Supervisor extractions | 3 seams | ~2 days |
| Principle work | G1–G5 | 1–2 weeks after refactor |

