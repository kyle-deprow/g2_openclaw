"""Prompt construction for the autoresearch engine."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from gateway.autoresearch import compute, constants
from gateway.autoresearch import manifest_runtime as manifest_runtime_module
from gateway.autoresearch import transitions as transitions_module
from gateway.autoresearch.artifacts import (
    ARTIFACT_CONTRACTS as ARTIFACT_CONTRACTS,
)
from gateway.autoresearch.artifacts import (
    NextAction as NextAction,
)
from gateway.autoresearch.artifacts import (
    PriceHydrationScopePreflight as PriceHydrationScopePreflight,
)
from gateway.autoresearch.constants import (
    MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS as MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS,
)
from gateway.autoresearch.constants import (
    MAX_ARTIFACT_FILE_BYTES as MAX_ARTIFACT_FILE_BYTES,
)
from gateway.autoresearch.enums import (
    ArtifactType as ArtifactType,
)
from gateway.autoresearch.enums import (
    Phase as Phase,
)
from gateway.autoresearch.enums import (
    ResearchMode as ResearchMode,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.evidence import (
    _target_repo_root_for_state as _target_repo_root_for_state,
)
from gateway.autoresearch.evidence import (
    build_quantipy_execution_contract as build_quantipy_execution_contract,
)
from gateway.autoresearch.gitops import (
    _render_literal as _render_literal,
)
from gateway.autoresearch.manifest import (
    InstructionSourceManifest as InstructionSourceManifest,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy as AutoresearchPolicy,
)
from gateway.autoresearch.policy import (
    ReceiptCatalog as ReceiptCatalog,
)
from gateway.autoresearch.prompts import (
    _compute_fit_contract as _compute_fit_contract,
)
from gateway.autoresearch.prompts import (
    _json_block as _json_block,
)
from gateway.autoresearch.prompts import (
    _mempalace_kg_fact_instruction as _mempalace_kg_fact_instruction,
)
from gateway.autoresearch.prompts import (
    _mode_contract as _mode_contract,
)
from gateway.autoresearch.prompts import (
    _negative_results_ledger as _negative_results_ledger,
)
from gateway.autoresearch.prompts import (
    _operator_precondition_decision_instruction as _operator_precondition_decision_instruction,
)
from gateway.autoresearch.prompts import (
    _render_instruction_source_manifest as _render_instruction_source_manifest,
)
from gateway.autoresearch.prompts import (
    _select_phase_target as _select_phase_target,
)
from gateway.autoresearch.recovery_receipts import (
    _expected_quantipy_verification_run_id as _expected_quantipy_verification_run_id,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.state import (
    AutoresearchValidationContext as AutoresearchValidationContext,
)
from gateway.autoresearch.transitions import (
    _latest_verification_is_price_scope_bug_signal as _latest_verification_is_price_scope_bug_signal,  # noqa: E501
)
from gateway.autoresearch_readiness import (
    PlatformReadinessManifest as PlatformReadinessManifest,
)


def _alpha_campaign_directive(state: AutoresearchState) -> str:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return ""
    return (
        "ALPHA_RESEARCH campaign directive: strategies may use any holding period from minutes "
        "up to a full trading session. Every position must be flat by the session close; "
        "overnight carry is forbidden. The receipt-bound panel is regular-hours 1-minute data, "
        "so an overnight leg cannot be execution-modelled and must not be proposed. Proposals "
        "must target at least one trade per day on average aggregated across the pre-registered "
        "panel over the OOS window, and a verification result below 1.0 trades/day on average "
        "misses the activity requirement regardless of Sharpe. The verification artifact must "
        "also report trades per day per instrument so a large universe cannot satisfy the "
        "aggregate floor while most instruments sit idle. Proposals should leave headroom above "
        "this floor rather than targeting it exactly. The relaxation follows campaign evidence "
        "that trading friction, not signal absence, has been the binding constraint: one iteration "
        "found a null-beating edge consumed by costs, and another pre-screened for cost-surviving "
        "edges at a five-minute horizon and found none. The permitted-horizon range now spans "
        "both shorter and longer holds, each with a different edge-to-cost ratio. This is context, "
        "not an instruction to prefer long holds: the panel chooses the horizon on its merits, and "
        "a short-horizon proposal that clears costs remains "
        "entirely valid. A permanently burned family may be re-proposed at a materially different "
        "holding horizon only with materially_new_evidence naming the horizon change as the "
        "substantive difference and explaining why the prior negative does not carry over. A "
        "trivially rescaled rerun of a burned family is not novel; contested-family revisit rules "
        "are unchanged. Each iteration's consensus must declare a universe_plan carrying a "
        "mechanical, data-independent selection rule with its screen criteria, ranking "
        "criterion, as-of date, and member count, fixed before any return, label, or "
        "performance data is examined; selection may never use out-of-sample returns, realized "
        "Sharpe, or any performance metric, because that is instrument cherry-picking and "
        "invalidates the iteration. Selected members bind to member_union_digest and the runner "
        "validates them against the plan, so post-hoc member substitution is forbidden. Keep the "
        "receipt-bound data contract, decision gates, review, and negative-results ledger.\n"
    )


def _phase_instruction(
    state: AutoresearchState,
    phase: Phase,
    expected_artifact_type: ArtifactType,
    agent_ids: Sequence[str],
    *,
    state_path: Path,
) -> str:
    instructions: dict[Phase, str] = {
        Phase.SETUP_CONTEXT: (
            "Own the setup/context phase deterministically. "
            "If setup is missing, create the setup artifact. "
            "If setup exists, produce the context packet only after "
            "loading the required instructions. Classify prior families from the decision "
            "rationale in canonical receipts: families discarded due to BUG_SIGNAL or other "
            "untrustworthy-evidence decisions belong in contested_methodology_families, not "
            "burned_theory_families; families with clean negative results stay burned."
            " Any context packet baseline_metric MUST be expressed as an "
            "out-of-sample, cost-net Sharpe; do not use an in-sample or gross baseline."
        ),
        Phase.DEBATE: (
            "Run the fixed five-agent debate. "
            "Each configured debate agent returns one theory, one vote, "
            "and explicit objections. Do not substitute agents or models. In ALPHA_RESEARCH, "
            "a family listed in contested_methodology_families is retryable at most once: "
            "a revisit MUST set "
            "materially_new_evidence, name the suspected methodology defect from the prior "
            "attempt, and describe the hardening that addresses it (for example stricter "
            "purge/embargo, a feature-timing audit, or pre-registered parameters). "
            "contested_methodology_families is distinct from the registry/ledger "
            "contested_families, which stay treated as burned. Once a revisit has occurred, "
            "the CURATOR must move the family to burned_theory_families and must not re-list "
            "it in contested_methodology_families."
        ),
        Phase.CONSENSUS: (
            "Decide whether the latest debate has a 3-of-5 majority; return MAJORITY or "
            "NO_CONSENSUS only. The first NO_CONSENSUS gets exactly one retry. For every "
            "non-operator-precondition MAJORITY in both ALPHA_RESEARCH and DATA_INFRA_G0, "
            "freeze one compact universe_plan with profile "
            "identity/digests, sorted unique explicit selection dates, and "
            "next-session-or-later execution policy. In ALPHA_RESEARCH, for a proposal "
            "revisiting a family listed in contested_methodology_families, require "
            "materially_new_evidence naming the prior defect and the hardening correction; "
            "include novelty_delta only when the mechanical novelty rule requires it "
            "(fingerprint or Tier-2 contested match); otherwise carry the suspected defect "
            "and its correction in materially_new_evidence and the implementation brief. "
            "contested_methodology_families is distinct from the registry/ledger "
            "contested_families, which stay treated as burned. Once a revisit has occurred, "
            "the CURATOR must move the family to burned_theory_families and must not re-list "
            "it in contested_methodology_families. Before testing the 3-of-5 majority in both "
            "rounds, group closely related proposals into theory-family clusters; cluster votes "
            "count together when proposals share the same economic mechanism. After the single "
            "retry, if no cluster reaches three votes, MUST NOT return NO_CONSENSUS: use the "
            "pre-registered deterministic tie-break: most votes; least-explored family by "
            "fewest hypothesis-registry entries across its families; lexicographically smallest "
            "normalized family name. Record tie-break application, rule inputs, and losing "
            "clusters in dissent_summary. The brief still faces full verification and unchanged "
            "gates; majority is integrity, tie-break is the guaranteed exit."
        ),
        Phase.IMPLEMENTATION: (
            "Implementation is allowed only after a majority consensus. "
            "Use the final implementation brief exactly "
            "as approved. No implementation without consensus majority. For "
            "ALPHA_RESEARCH, derive and prewarm the platform data plan before creating "
            "or running the committed quantipy-experiment-v2 package. A succeeded prewarm "
            "run bound to the current state reference MUST be reused. A replacement prewarm "
            "may be launched only after stating in one line which existing receipt is unusable "
            "and why; this is a reporting duty, not a mechanical gate. The implementation "
            "stages must not import quantipy or use network, provider, SQL, filesystem, "
            "or hydration access. "
            "worker must use only the public Quantipy client path: prewarm every frozen "
            "explicit selection date once with "
            "qp.security_universe_screen(), derive deterministic contiguous batches from "
            "the frozen canonical plan inputs, perform one "
            "qp.security_universe_history() operation per batch, form only an in-memory "
            "sorted member union, and call "
            "qp.prices() exactly once for that union and the full experiment "
            "range/timeframe/market-hours before constructing any fold. Resolve that "
            "union into the fixed, sorted manifest panel request. The Quantipy runtime "
            "owns authoritative panel creation, hydration, receipt validation, and "
            "receipt persistence; receipts remain runtime-owned. Do not put that logic "
            "in prepare.py or any other experiment stage. The v2 runtime "
            "intentionally gives stages only the immutable verified panel. Before any "
            "hydrate-capable command, compute price_hydration_scope_preflight with "
            "member_union_count, experiment range, timeframe, market_hours, XNYS "
            "session_count, planned_symbol_sessions, and within_budget; include it "
            "in implementation_result. Accepted feasibility summaries report "
            "encoded_feature_columns after one-hot/categorical expansion with declared "
            "feature count and training row count; plainly flag a near-row-unique "
            "categorical as a compute and memorization hazard when encoded columns "
            "approach or exceed rows. Report calibration_fit_seconds from one fit of the "
            "largest (final) walk-forward fold with one candidate at real encoded "
            "dimensionality, "
            "and projected_model_seconds = calibration_fit_seconds * fold_count * "
            "candidate_count, identifying fold_count and candidate_count. This cost "
            "telemetry is reporting-only, not an admission gate; preserve within_budget's "
            "existing data-budget semantics and never refuse work on the basis of projected cost. "
            "If "
            "within_budget is false, do not run any "
            "qp.prices(), hydrate, full backtest, or notebook command that would load "
            "the price panel; commit the scaffold, focused tests, notebook shell, and "
            "over-budget preflight so verification can emit the structured feasibility "
            "BUG_SIGNAL without spending the hydrate cost. Commit one canonical "
            "quantipy-experiment-v2 manifest under the workspace with exactly prepare, smoke, "
            "feasibility, and model stage_files; record its canonical absolute path and "
            "SHA-256 in implementation_result. A notebook alone is not implementation evidence. "
            "Stage summaries have a 4096-character cap; assert serialized length <= 3800 locally "
            "before accepting, compacting with top-K by |magnitude| with K named, ~6 significant "
            "figures, prefixes stripped, comparators as scalars, and bulk detail in the run "
            "artifact. Experiment packages are built ONLY in the persisted experiment workspace, "
            "never in the "
            "authoritative quantipy worktree."
        ),
        Phase.VERIFICATION: (
            "Verify the produced experiment deterministically. "
            "Use implementation_result.workspace_path and "
            "implementation_result.commit_sha as the source under test. "
            "Run focused tests, then the one canonical detached uv --directory runtime command "
            "under the fixed private runs root with deterministic "
            "run_id before evaluating metrics. Set the detached run's timeout_seconds from "
            "the projected_model_seconds reported in implementation_result.summary, or "
            "from the prior attempt's feasibility output carried into a fix round, using "
            "timeout_seconds = min(max(3 * projected_model_seconds + pre_model_seconds, 1800), "
            "43200) for implementation_result.compute_fit.target gpu or mixed, or "
            "timeout_seconds = min(max(3 * projected_model_seconds + pre_model_seconds, 1800), "
            "21600) for cpu or none, where pre_model_seconds is the prior attempt's measured "
            "wall time before the model stage started, or 1200 when no attempt has run. The "
            "projection covers only the model fit; hydration, prepare, smoke, and feasibility "
            "must be budgeted separately or a large panel is killed before modelling begins. "
            "A first attempt has no projection, so say so in the artifact and use the "
            "default 28800s for gpu or mixed or the default 14400s for cpu or none. Measured "
            "values still drive the timeout, and the ceiling is only a cap, not permission to "
            "skip projection. A timeout kill yields recoverable "
            "execution-interrupted evidence routed to a bounded fix round, so an "
            "underestimate is recoverable; deriving it makes failure fast rather than "
            "preventing it. Reject impossible metrics, failing tests, or incomplete required "
            "metrics. A run that completes all stages and produces zero trades because a "
            "declared, pre-registered cost or edge gate excluded every candidate is a valid "
            "negative result, not BUG_SIGNAL. Emit status=PASS with tests_passed=true, "
            "bug_signals=[], trade_count=0, and trades_per_day=0.0; report undefined ratio "
            "metrics is_walk_forward_sharpe_net, oos_sharpe_net, max_drawdown_pct, and win_rate "
            "as 0.0 with the zero denominator stated in the summary, never null. The artifact "
            "must cite the gate's exact parameter names and values from the approved consensus "
            "implementation_brief and the excluded_candidate_count; do not rely on a self-report "
            "that merely says pre-registered. Make it eligible for a normal DISCARD only after "
            "the reviewer confirms that exact pre-registration and count. "
            "Zero trades caused by a defect such as an empty panel, broken feature build, or "
            "exception remain BUG_SIGNAL. Stage summaries have a 4096-character cap; assert "
            "serialized length <= 3800 locally before accepting, compacting with top-K by "
            "|magnitude| with K named, ~6 significant figures, prefixes stripped, comparators "
            "as scalars, and bulk detail in the run artifact."
        ),
        Phase.REVIEW: (
            "Run exactly one configured reviewer. "
            "The reviewer must return PASS, CONDITIONAL PASS, or FAIL "
            "with concrete fix requests. Before accepting a zero-trade gate-excluded result "
            "for DISCARD, confirm that the verification artifact's exact gate parameter names "
            "and values match the approved consensus implementation_brief and that "
            "excluded_candidate_count is present; otherwise do not accept the DISCARD. "
            "For ALPHA_RESEARCH, the reviewer's recommended_metric_name MUST name an "
            "out-of-sample, cost-net metric from the accepted verification artifact. "
            "An in-sample metric is not a valid recommendation."
        ),
        Phase.FIX_TEST: (
            "Apply a narrow fix against the latest verification or review failure. "
            "After a fix, the next step is always verification. Any notebook, "
            "hydrate, backtest, or similarly long test command MUST be launched "
            "through /home/dev/repos/g2_openclaw/scripts/run-long-task.sh with a "
            "unique absolute --run-dir and bounded polling; direct foreground "
            "execution is invalid. Output starting with LAUNCH_QUEUED: is success "
            "— end the turn, do not retry, and do not classify it as a blocker; "
            "the supervisor launches within one poll cycle. If the launcher "
            "cannot be used outside that queue path, fail closed and report the "
            "infrastructure blocker without emitting a fix_result."
        ),
        Phase.DECISION_LOG: (
            "Decide and log the completed iteration. "
            "For ALPHA_RESEARCH, the decision Sharpe is the out-of-sample cost-net Sharpe; "
            "if the reviewer recommended an in-sample metric, substitute the out-of-sample "
            "net Sharpe and state the substitution in the rationale. Average activity below "
            "1.0 trades/day over the OOS window is DISCARD regardless of Sharpe. "
            "Memory writes are forbidden before this final decision artifact exists. "
            "Set memory_write_required=true only for ALPHA_RESEARCH KEEP, SIGNIFICANT KEEP, "
            "STRONG KEEP, or DISCARD with a latest completed verification status=PASS and "
            "tests_passed=true. Set it false for every other outcome; the runner enforces "
            "this retention rule mechanically."
        ),
        Phase.REPEAT: (
            "The iteration is complete. Do not start the next loop from prompt memory. "
            "Do not write MemPalace from a model turn. If final memory is required, "
            "the platform supervisor finalizes it from authoritative state before "
            "the next iteration is adopted."
        ),
    }
    contract = _json_block(ARTIFACT_CONTRACTS[expected_artifact_type], compact=True)
    agent_text = ", ".join(agent_ids) if agent_ids else "(controller/no agent spawn)"
    workspace_contract = _workspace_isolation_contract(state, phase)
    mode_contract = _mode_contract(state)
    compute_fit_contract = _compute_fit_contract(
        state,
        phase,
        expected_artifact_type,
    )
    verification_handoff_contract = _verification_handoff_contract(
        phase,
        expected_artifact_type,
        state_path=state_path,
        state=state,
        price_scope_preflight=(
            state.implementation_result.price_hydration_scope_preflight
            if phase is Phase.VERIFICATION and state.implementation_result is not None
            else None
        ),
    )
    mempalace_fact_instruction = _mempalace_kg_fact_instruction(state, expected_artifact_type)
    operator_precondition_instruction = _operator_precondition_decision_instruction(
        state,
        phase,
        expected_artifact_type,
    )
    negative_results_ledger = ""
    if expected_artifact_type in {
        ArtifactType.CONTEXT_PACKET,
        ArtifactType.DEBATE_RESULT,
        ArtifactType.CONSENSUS_RESULT,
    }:
        rendered_ledger = _negative_results_ledger(state)
        if rendered_ledger:
            negative_results_ledger = f"{rendered_ledger}\n\n"
    context_source_instruction = ""
    if expected_artifact_type is ArtifactType.CONTEXT_PACKET:
        context_source_instruction = (
            "Context source contract:\n"
            "- standalone iteration context files are non-authoritative residue. Do not read "
            "or reuse iteration-<n>-context.json. Rebuild the context packet only from STATE_REF, "
            "the instruction manifest sources, canonical decision receipts, and read-only "
            "MemPalace retrieval. "
            "If the live state no longer matches STATE_REF, do not emit an artifact; report the "
            "stale dispatch so the PM can rerun autoresearch-next.\n\n"
        )
    return (
        f"phase={phase.value}\n"
        f"agents={agent_text}\n"
        f"artifact_type={expected_artifact_type.value}\n"
        f"instruction={instructions[phase]}\n"
        f"{mode_contract}"
        f"{_alpha_campaign_directive(state)}"
        f"{compute_fit_contract}"
        f"{workspace_contract}"
        f"{verification_handoff_contract}"
        f"{mempalace_fact_instruction}"
        f"{negative_results_ledger}"
        f"{operator_precondition_instruction}"
        f"{context_source_instruction}"
        f"ARTIFACT_CONTRACT={contract}\n"
    )


def _verification_handoff_contract(
    phase: Phase,
    expected_artifact_type: ArtifactType,
    *,
    state_path: Path,
    state: AutoresearchState,
    price_scope_preflight: PriceHydrationScopePreflight | None = None,
) -> str:
    if (
        phase is not Phase.VERIFICATION
        or expected_artifact_type is not ArtifactType.VERIFICATION_RESULT
    ):
        return ""
    expected_run_id = (
        _expected_quantipy_verification_run_id(
            state,
            state.implementation_result.commit_sha,
        )
        if state.implementation_result is not None
        else "autoresearch-i<iteration>-<commit12>"
    )
    execution_contract = (
        build_quantipy_execution_contract(
            runtime_root=_target_repo_root_for_state(state),
            manifest_path=Path(state.implementation_result.experiment_manifest_path),
            output_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            run_id=expected_run_id,
        )
        if state.implementation_result is not None
        else None
    )
    execution_command = (
        " ".join(execution_contract.command)
        if execution_contract is not None
        else (
            "env PYTHONDONTWRITEBYTECODE=1 uv --directory <canonical-root> run "
            "--frozen --no-sync quantipy experiment run <absolute-worktree-manifest> "
            "--output-root <output-root> --run-id <run-id>"
        )
    )
    scope_gate = ""
    if price_scope_preflight is not None:
        scope_gate = (
            "- Runner-bound price hydration scope preflight: "
            f"{_json_block(price_scope_preflight.to_dict(), compact=True)}. "
        )
        if price_scope_preflight.within_budget:
            scope_gate += (
                "This is within budget; verification may run the hydrate/backtest command "
                "after tests and notebook smoke checks pass.\n"
            )
        else:
            scope_gate += (
                "This exceeds budget. Do not run any command that can call qp.prices() "
                "for the hydrate/backtest. Emit status=BUG_SIGNAL with bug_signals "
                "containing price_hydration_scope_exceeds_budget and the exact "
                "preflight values, set hydrate-dependent metrics/coverage/receipts to "
                "null, and advance the artifact.\n"
            )
    alpha_interruption_instruction = (
        "- For ALPHA_RESEARCH runtime timeout or interruption, submit a TEST_FAILURE "
        "verification_result carrying quantipy_execution_interrupted evidence, or let "
        "the supervisor auto-advance the terminal run. The resulting Fix/Test round "
        "must rescope the experiment specification and timeout; do not relaunch the "
        "identical manifest.\n"
        if state.mode is ResearchMode.ALPHA_RESEARCH
        else ""
    )
    return (
        "Verification handoff contract:\n"
        "- Every verification attempt must terminate by writing a structured JSON "
        "verification_result artifact to an absolute path under the PM workspace, "
        "validating that same absolute path with jq/wc, and advancing it with "
        "`/home/dev/repos/g2_openclaw/.venv/bin/gateway-cli autoresearch-advance "
        f"{_render_literal(str(state_path))} "
        "/home/dev/.openclaw/workspace-autoresearch-pm/<artifact.json> "
        "--instruction-manifest-sha256 <source_manifest_sha256> "
        "--state-reference-sha256 <state_reference_sha256>` "
        "before any prose completion or status report. A prose-only verification "
        "completion is invalid.\n"
        "- This applies to failing tests and partial runs: do not stop after "
        "describing a failure. Persist and advance the JSON artifact with exact "
        "commands in commands_run and decisive evidence in the metric fields, "
        "null_test_summary, bug_signals, data_coverage, and infra_rationale when "
        "applicable.\n"
        "- Mandatory typed Quantipy runtime gate: first run focused tests, then launch the "
        "exact direct argv `"
        f"{execution_command}` with detached cwd equal to the canonical runtime root through "
        "`/home/dev/repos/g2_openclaw/scripts/run-long-task.sh`. Its immutable detached "
        "manifest must set expected_artifact_path to the known "
        "`<root>/<run-id>/run.json`. Direct foreground execution cannot satisfy this "
        "contract. Under the non-malicious same-host agent trust model, PASS requires the "
        "detached worker's sealed attestation; a verifier claim cannot replace it. The "
        "detached worker must publish terminal success with complete EOF drain, truthful "
        "bounded-tail truncation metadata, and a secure expected-artifact "
        "size/SHA-256 attestation before quantipy_experiment_evidence can be accepted; "
        "the evidence must bind the detached run directory/manifest digest and the current "
        "run.json bytes must match that worker attestation. An artifact-supplied hash alone "
        "is never proof. The run must also match the committed manifest path/SHA and "
        "implementation commit. Smoke and feasibility must accept before Quantipy imports "
        "or executes model. PASS requires success and completed_stages exactly "
        "[prepare, smoke, feasibility, model], plus panel identity/digests when requested. "
        "Quantipy exits 0 exactly for success=true and 1 exactly for success=false. PASS "
        "requires detached SUCCEEDED/exit 0. Process success is not research validity: a "
        "successful run with anomalous or missing alpha metrics, coverage, or paired receipts "
        "is BUG_SIGNAL when tests_passed=true and bug_signals is nonempty; it routes to FIX_TEST "
        "and never counts as PASS. TEST_FAILURE remains invalid after a successful Quantipy run "
        "because focused test failure prevents runtime execution under this command order. A valid "
        "rejected/failed run used by TEST_FAILURE or BUG_SIGNAL requires detached FAILED/exit 1 "
        "with no signal and ordinary process_error classification; timeout, operator stop, "
        "resource exhaustion, artifact, capture, signal, or any other nonzero outcome is not "
        "a typed contract exit. A run that exists must retain truthful rejected/failed typed "
        "evidence. When focused tests "
        "prevent execution, set runtime evidence "
        "to null and populate quantipy_execution_not_started with its allowed reason, exact "
        "command/evidence, manifest binding, and expected run ID/path. G2 atomically tombstones "
        "the absent run directory; retry only after a new commit yields a new deterministic ID. "
        "Requested panels may omit evidence only for typed pre-stage preflight, panel, or "
        "filesystem failures. Notebook execution, nbconvert, and papermill "
        "may render a smoke/report only and never substitute for this gate or PASS.\n"
        "- Classify failures mechanically: any nonzero or failed test command is "
        "status TEST_FAILURE with tests_passed=false; impossible, leaky, or "
        "anomalous metrics are status BUG_SIGNAL with nonempty bug_signals; use "
        "PASS only when tests passed and bug_signals is empty. Every required JSON key "
        "must be present; for TEST_FAILURE or BUG_SIGNAL, unavailable metrics or coverage "
        "must be null rather than fabricated or zero-valued.\n"
        "- Verification owns all factual receipts; implementation_result must not contain "
        "them. For ALPHA_RESEARCH PASS, require complete alpha metrics, compact dynamic "
        "data_coverage, and paired universe and price hydration receipts. Construct deterministic "
        "contiguous universe history batches from the canonical plan, with one operation per batch "
        "and authoritative contract/source/calendar evidence for every date. Include one canonical "
        "price hydration receipt and compact dynamic data_coverage bound to the same union, range, "
        "timeframe, and market-hours. PASS requires missing_symbol_count=0, "
        "missing_symbol_sessions=0, and covered_symbol_sessions=expected_symbol_sessions. "
        "Before running any command that can call qp.prices(), generate a price hydration "
        "scope preflight from the planned member-union count and XNYS experiment session "
        f"count. The hard budget is {MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS} "
        "symbol-sessions. If the planned member_union_count * session_count exceeds "
        "that budget, do not run the hydrate/backtest command; emit status=BUG_SIGNAL, "
        "tests_passed=true when focused unit/notebook checks already passed, null "
        "metrics/coverage/receipts that require the skipped hydrate, and include a "
        "nonempty bug_signals entry named price_hydration_scope_exceeds_budget with "
        "the computed count, limit, member_union_count, date range, and session_count. "
        "This is an experiment feasibility signal for fixer, not an operator "
        "infrastructure repair.\n"
        "- Dynamic coverage accepted by the control plane must stay within the same "
        f"{MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS} symbol-session budget. "
        "Dynamic coverage must include member_union_count, member_union_digest, "
        "experiment_start, experiment_end, oos_start, oos_end, timeframe, market_hours, "
        "expected_symbol_sessions, covered_symbol_sessions, missing_symbol_count, "
        "missing_symbol_sessions, default_fold_count, and fallback_fold_count. "
        "TEST_FAILURE or BUG_SIGNAL may set all three receipts to null when unavailable, "
        "but must never emit a partial receipt chain or a partial PASS.\n"
        f"{scope_gate}"
        f"{alpha_interruption_instruction}"
        "- For DATA_INFRA_G0, include infra_gate_outcome and infra_rationale "
        "explaining why the infrastructure gate is GATE_PASSED or "
        "REMEDIATION_REQUIRED. status and tests_passed describe verifier command, "
        "test, and typed Quantipy runtime execution plus experiment correctness, never whether "
        "the infrastructure gate passed. REMEDIATION_REQUIRED is a valid completed "
        "verification outcome: emit PASS with tests_passed=true when commands, tests, "
        "and typed Quantipy runtime execution succeeded. A DATA_INFRA_G0 PASS may set "
        "alpha metrics "
        "and data_coverage to null when unavailable, but the platform gate requires "
        "runner-checkable implementation preflight plus paired universe, price hydration, "
        "and platform coverage receipts. If that provenance is unavailable or mismatched, emit "
        "BUG_SIGNAL with the sole bug signal platform_coverage_contract_mismatch and null "
        "infrastructure outcome, rationale, and receipt. A remediation PASS is stage evidence "
        "only: it advances to review and then non-suspending DISCARD. It can never authorize "
        "INFRA_BLOCKED or suspend the loop; only explicit operator-owned readiness suspension "
        "may suspend. Do not send remediation to fixer. Use "
        "TEST_FAILURE only for actual nonzero command or test execution, a malformed "
        "or missing required receipt, an experiment defect, or inability to execute "
        "verification. Do not use Sharpe as the gate rationale. Every new G0 "
        "envelope must include platform_coverage_validation from Quantipy's shared "
        "qp.validate_dynamic_price_coverage validator. Its canonical digest proves only "
        "self-consistency; the runner trusts it only when it matches the exact "
        "implementation preflight, universe verification receipt, price hydration receipt, "
        "verified member-union manifest, requested XNYS sessions, source response digest, "
        "and count identity fields. The accepted contract is "
        "contract_version=dynamic-price-coverage-v1, "
        "source_contract_version=price-coverage-v1, timeframe=1min, market_hours=regular, "
        "scope=full_union_hydration, "
        "source request identity/provider fields and digest fields member_union_digest, "
        "requested_sessions_digest, pit_active_roster_digest, and "
        "source_price_coverage_response_digest. The member_union_digest is Quantipy's "
        "compact JSON-array digest; do not compare it directly to the universe or "
        "hydration newline-manifest digest. The price hydration receipt must carry "
        "the required source_price_coverage_response_digest from the actual Quantipy "
        "PriceCoverageResponse; it is not the hydration coverage_receipt_digest metadata "
        "digest. Treat pit_active_roster_digest as intrinsic Quantipy receipt data, not "
        "as independently reproducible exact PIT identity. "
        "hydrated_symbol_sessions must equal "
        "member_union_count * requested_session_count, while inactive union sessions "
        "are hydrated minus active sessions for both receipt scopes. Scope selects the "
        "upstream assertion semantics; every receipt reports both geometries. "
        "pit_active_roster is not proof of full-union coverage. Provider-empty inactive "
        "union sessions are valid and are not violation codes. GATE_PASSED requires a "
        "COMPLETE receipt cross-checked against runner-owned preflight identity and counts "
        "before non-suspending INFRA_REPAIRED. REMEDIATION_REQUIRED requires matching "
        "nonempty violation codes but remains non-authorizing stage evidence. "
        "unexpected_session_count counts distinct unexpected dates. A "
        "missing paired receipt or Quantipy scope, contract, provenance, or digest "
        "mismatch becomes BUG_SIGNAL "
        "platform_coverage_contract_mismatch with null infrastructure outcome, rationale, "
        "and receipt, then routes to fixer. Never self-author a receipt as infrastructure "
        "proof.\n\n"
    )


def _workspace_isolation_contract(state: AutoresearchState, phase: Phase) -> str:
    if phase is Phase.IMPLEMENTATION:
        worktree_root = _render_literal(str(constants.DEFAULT_AUTORESEARCH_WORKTREE_ROOT))
        return (
            "Workspace isolation contract:\n"
            "- Create and use a disposable isolated clone for this iteration under the "
            f"canonical model-writable root {worktree_root}; "
            "before cloning, run umask 077 and mkdir -p "
            "/home/dev/.openclaw/autoresearch/model-workspaces, chmod 700 on that root, "
            "and verify the current user owns it with mode 0700; after clone creation, "
            "run chmod 700 on the workspace directory and verify it is owned by "
            "the current user with mode 0700. Do not use git worktree; linked worktrees "
            "share canonical Git metadata with the authoritative checkout. "
            "Never use /tmp. /tmp is a 31G tmpfs, and each Quantipy workspace virtualenv "
            "is about 1.5G, so stale iteration workspaces exhaust it. Do not implement "
            "directly in the main target repo checkout.\n"
            "- Do not leave background experiment, notebook, pytest, or data-generation "
            "processes running after the stage exits.\n"
            "- Commit all accepted implementation changes before emitting the artifact; if "
            "the worktree cannot be made clean, fail closed and report that blocker.\n"
            "- Include the disposable worktree path in workspace_path and the accepted "
            "commit SHA in commit_sha.\n"
            "- Set every detached manifest's working_directory and spawned process "
            "cwd to that exact worktree path; never run prewarm or implementation "
            "commands from the "
            "authoritative target checkout.\n"
            "- Preserve unrelated user files such as "
            "docs/quantipy_experiment_mempalace_preload.md.\n\n"
        )
    if phase is not Phase.FIX_TEST:
        return ""
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "fix_test workspace contract requires implementation_result"
        )
    price_scope_fix_contract = ""
    if _latest_verification_is_price_scope_bug_signal(state):
        price_scope_fix_contract = (
            "- The latest verification is a price_hydration_scope_exceeds_budget "
            "BUG_SIGNAL. During Fix/Test, do not run any hydrate-capable command, "
            "including qp.prices(), generate_*results scripts, nbconvert, papermill, "
            "or jupyter execute. Fix only the experiment scope/guard/tests and let "
            "the next verification stage perform any permitted hydrate/backtest. "
            "The control plane rejects fix_result.tests_rerun entries matching those "
            "commands.\n"
        )
    return (
        "Fix/Test workspace continuity contract:\n"
        "- From the verified authoritative state, reuse the exact persisted implementation "
        "worktree and accepted implementation commit. Never create another worktree or edit "
        "the main target checkout.\n"
        "- Before editing, require a clean or recoverable Git state. Do not discard or "
        "overwrite unrelated changes; if reconciliation is ambiguous or would lose "
        "unrelated work, fail closed and report the blocker.\n"
        "- Before editing, verify the persisted workspace is an owned non-symlink "
        "directory with mode 0700; if that precondition fails, stop and report the "
        "infrastructure blocker.\n"
        "- If the authoritative target checkout advanced because human/Codex promoted "
        "shared infrastructure, incorporate that already-authoritative history into this "
        "same experiment worktree while preserving the accepted experiment commit. Never "
        "independently edit shared infrastructure.\n"
        "- Do not leave background experiment, notebook, pytest, or data-generation "
        "processes running after the stage exits.\n"
        "- Any notebook, hydrate, backtest, or similarly long test command must be "
        "launched through /home/dev/repos/g2_openclaw/scripts/run-long-task.sh with a "
        "unique absolute --run-dir and bounded polling. Direct foreground execution "
        "is invalid. Output starting with LAUNCH_QUEUED: is success — end the turn, "
        "do not retry, and do not classify it as a blocker; the supervisor launches "
        "within one poll cycle. If the launcher cannot be used outside that queue "
        "path, fail closed and report the infrastructure blocker without emitting a "
        "fix_result.\n"
        "- Finish with a clean, committed result. The fix_result artifact must use the same "
        "verified workspace_path exactly and report its accepted final commit SHA in commit_sha.\n"
        "- Keep every detached Fix/Test manifest's working_directory and spawned process "
        "cwd set to the same persisted workspace_path; never run from the authoritative "
        "target checkout.\n"
        "- If a verification fix changes the planned ALPHA price-hydration scope, include "
        "the updated price_hydration_scope_preflight in fix_result using the same strict "
        "object shape as implementation_result. If the fix does not change scope, set "
        "price_hydration_scope_preflight to null. Do not omit the key.\n"
        f"{price_scope_fix_contract}"
        "- Preserve unrelated user files such as "
        "docs/quantipy_experiment_mempalace_preload.md.\n\n"
    )


def _build_prompt_text(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    phase: Phase,
    expected_artifact_type: ArtifactType,
    agent_ids: Sequence[str],
    instruction_source_manifest: InstructionSourceManifest,
    source_manifest_sha256: str,
    state_reference_sha256: str,
    readiness: PlatformReadinessManifest,
) -> str:
    target_repo = _target_repo_root_for_state(state)
    compute_snapshot = compute.collect_compute_capability_snapshot(target_repo)
    phase_instruction = _phase_instruction(
        state,
        phase,
        expected_artifact_type,
        agent_ids,
        state_path=Path(instruction_source_manifest.state_reference.path),
    )
    return (
        "Autoresearch prompt.\n"
        f"PLATFORM_READINESS_CAPABILITIES={readiness.prompt_capabilities()}\n"
        f"POLICY={policy.model_policy_summary()}\n"
        f"COMPUTE_SNAPSHOT={_json_block(compute_snapshot.to_dict(), compact=True)}\n"
        f"{phase_instruction}\n"
        f"STATE_REF={instruction_source_manifest.state_reference.canonical_json()}\n"
        f"state_reference_sha256={state_reference_sha256}\n"
        f"source_manifest_sha256={source_manifest_sha256}\n"
        f"INSTRUCTION_MANIFEST={_render_instruction_source_manifest(instruction_source_manifest)}\n"
        "STATE_REF is the immutable runner-bound state reference for this dispatch. Use it to "
        "identify the phase, iteration, and canonical state path, but do not reject the task "
        "because the live state file has advanced after dispatch; the runner performs the "
        "authoritative persisted-state match before accepting artifacts. Do not work from "
        "prompt memory: read the state path when you need phase artifacts, and treat missing or "
        "malformed required artifact data as a blocker.\n"
        "Then read every instruction_source_manifest file at its canonical path when its "
        "current methodology rules are needed. Configured skills remain authoritative for "
        "current methodology, but do not reject the task solely because a mutable live source "
        "file or readiness file no longer hashes to the dispatch receipt. The exact compact "
        "manifest's "
        "versioned, domain-separated digest binds the verified state reference, phase, artifact "
        "type, ordered agent IDs, canonical repo root, and sorted receipts. Artifacts use this "
        'exact JSON envelope: {"instruction_manifest_sha256":"<source_manifest_sha256>",'
        '"state_reference_sha256":"<state_reference_sha256>","artifact":{...}}. '
        "Unwrapped artifacts, missing/mismatched digests, and extra envelope keys are invalid "
        "and cannot advance. Artifact maximum: "
        f"{MAX_ARTIFACT_FILE_BYTES} bytes; compact, never truncate.\n"
    )


def next_action(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    readiness: PlatformReadinessManifest,
    *,
    state_path: Path = constants.DEFAULT_AUTORESEARCH_STATE_PATH,
) -> NextAction:
    try:
        validation_context = AutoresearchValidationContext.from_readiness(readiness)
        validation_context.validate_for_state(state)
        transitions_module._validate_state(state, policy, validation_context)
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    if state.suspended:
        raise AutoresearchValidationError(
            "autoresearch is suspended on an infrastructure blocker; "
            "run autoresearch-resume after platform readiness changes"
        )
    if state.phase is not Phase.REVIEW:
        transitions_module._revalidate_accepted_member_union_manifests(state)
    transitions_module._validate_alpha_verification_price_preflight(state)
    target = _select_phase_target(state, policy)
    required_receipts = receipts.require(manifest_runtime_module.PHASE_RECEIPTS[state.phase])
    instruction_source_manifest = manifest_runtime_module.build_instruction_source_manifest(
        phase=state.phase,
        expected_artifact_type=target.artifact_type,
        target_agent_ids=target.agent_ids,
        target_repo_root=_target_repo_root_for_state(state),
        state=state,
        state_path=state_path,
        receipts=required_receipts,
    )
    source_manifest_sha256 = instruction_source_manifest.sha256()
    state_reference_sha256 = instruction_source_manifest.state_reference.sha256()
    prompt_text = _build_prompt_text(
        state=state,
        policy=policy,
        phase=state.phase,
        expected_artifact_type=target.artifact_type,
        agent_ids=target.agent_ids,
        instruction_source_manifest=instruction_source_manifest,
        source_manifest_sha256=source_manifest_sha256,
        state_reference_sha256=state_reference_sha256,
        readiness=readiness,
    )
    prompt_bytes = len(prompt_text.encode("utf-8"))
    if prompt_bytes > constants.MAX_NEXT_ACTION_PROMPT_BYTES:
        raise AutoresearchValidationError(
            "autoresearch prompt exceeds hard byte budget: "
            f"{prompt_bytes} > {constants.MAX_NEXT_ACTION_PROMPT_BYTES} bytes for phase "
            f"{state.phase.value}; compact accepted artifact/state fields before dispatch"
        )
    if prompt_bytes > constants.NEXT_ACTION_PROMPT_TARGET_BYTES:
        raise AutoresearchValidationError(
            "autoresearch prompt exceeds operational byte target: "
            f"{prompt_bytes} > {constants.NEXT_ACTION_PROMPT_TARGET_BYTES} bytes for phase "
            f"{state.phase.value}; preserve the {constants.MAX_NEXT_ACTION_PROMPT_BYTES}-byte hard "
            "limit reserve before dispatch"
        )
    action = NextAction(
        phase=state.phase,
        next_agent_ids=target.agent_ids,
        expected_artifact_type=target.artifact_type,
        required_receipts=required_receipts,
        instruction_source_manifest=instruction_source_manifest,
        source_manifest_sha256=source_manifest_sha256,
        state_reference_sha256=state_reference_sha256,
        prompt_text=prompt_text,
    )
    if state.phase is Phase.REVIEW:
        # Keep this external-file check adjacent to review dispatch.
        transitions_module._revalidate_accepted_member_union_manifests(state)
    return action
