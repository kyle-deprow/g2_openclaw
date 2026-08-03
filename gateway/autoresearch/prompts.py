"""Prompt construction helpers for the autoresearch control plane."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from gateway.autoresearch.artifacts import (
    PhaseTarget as PhaseTarget,
)
from gateway.autoresearch.enums import (
    ArtifactType as ArtifactType,
)
from gateway.autoresearch.enums import (
    ConsensusStatus as ConsensusStatus,
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
from gateway.autoresearch.memory import (
    _is_explicit_no_memory_transition as _is_explicit_no_memory_transition,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.transitions import (
    _compact_json_block as _compact_json_block,
)
from gateway.autoresearch.transitions import (
    _is_operator_precondition_consensus as _is_operator_precondition_consensus,
)

if TYPE_CHECKING:
    from gateway.autoresearch.manifest import (
        InstructionSourceManifest as InstructionSourceManifest,
    )
    from gateway.autoresearch.policy import (
        AutoresearchPolicy as AutoresearchPolicy,
    )


def _json_block(payload: Mapping[str, object], *, compact: bool = False) -> str:
    if compact:
        return _compact_json_block(payload)
    return json.dumps(payload, indent=2, sort_keys=True)


def _render_instruction_source_manifest(manifest: InstructionSourceManifest) -> str:
    return _compact_json_block(manifest.to_dict())


def _select_phase_target(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
) -> PhaseTarget:
    if state.phase is Phase.SETUP_CONTEXT:
        if state.setup is None:
            return PhaseTarget((policy.pm.agent_id,), ArtifactType.SETUP)
        return PhaseTarget((policy.context_curator.agent_id,), ArtifactType.CONTEXT_PACKET)
    if state.phase is Phase.DEBATE:
        return PhaseTarget(policy.debate_agent_ids, ArtifactType.DEBATE_RESULT)
    if state.phase is Phase.CONSENSUS:
        return PhaseTarget((policy.consensus.agent_id,), ArtifactType.CONSENSUS_RESULT)
    if state.phase is Phase.IMPLEMENTATION:
        if (
            state.latest_consensus is None
            or state.latest_consensus.status is not ConsensusStatus.MAJORITY
        ):
            raise AutoresearchValidationError(
                "implementation next action requires a majority consensus"
            )
        return PhaseTarget((policy.implementer.agent_id,), ArtifactType.IMPLEMENTATION_RESULT)
    if state.phase is Phase.VERIFICATION:
        return PhaseTarget((policy.pm.agent_id,), ArtifactType.VERIFICATION_RESULT)
    if state.phase is Phase.REVIEW:
        return PhaseTarget((policy.reviewer.agent_id,), ArtifactType.REVIEW_RESULT)
    if state.phase is Phase.FIX_TEST:
        return PhaseTarget((policy.fixer.agent_id,), ArtifactType.FIX_RESULT)
    if state.phase is Phase.DECISION_LOG:
        return PhaseTarget((policy.pm.agent_id,), ArtifactType.FINAL_DECISION)
    if state.final_decision is not None and state.final_decision.memory_write_required:
        if state.memory_written:
            return PhaseTarget((), ArtifactType.NEXT_ITERATION)
        return PhaseTarget((), ArtifactType.MEMORY_WRITE)
    if _is_explicit_no_memory_transition(state):
        return PhaseTarget((), ArtifactType.NEXT_ITERATION)
    raise AutoresearchValidationError("repeat phase has no valid memory transition")


def _compute_fit_contract(
    state: AutoresearchState,
    phase: Phase,
    expected_artifact_type: ArtifactType,
) -> str:
    if expected_artifact_type not in {
        ArtifactType.DEBATE_RESULT,
        ArtifactType.IMPLEMENTATION_RESULT,
    }:
        if phase is not Phase.VERIFICATION:
            return ""
        if (
            state.implementation_result is not None
            and state.implementation_result.compute_fit is None
        ):
            return (
                "Compute execution contract:\n"
                "- This is a legacy implementation_result without compute_fit. Do not infer "
                "or silently assign a CPU/GPU target. Verify the exact recorded commands, "
                "report compute-fit evidence as unavailable, and surface any mismatch or "
                "migration blocker explicitly.\n\n"
            )
        return (
            "Compute execution contract:\n"
            "- Treat implementation_result.compute_fit as the declared execution target. "
            "Verify the actual run against it and report any mismatch as a concrete failure; "
            "never silently switch CPU/GPU execution.\n\n"
        )
    return (
        "Compute-fit contract:\n"
        "- Choose exactly one compute_fit.target: none, cpu, gpu, or mixed. The control plane "
        "does not prefer GPU or CPU; choose based on the hypothesis, data scale, reproducibility, "
        "and measured or planned cost.\n"
        "- Return compute_fit with target, non-empty rationale, required_dependencies, and "
        "benchmark_plan. Use importable package names for dependencies and `cuda_runtime` for "
        "the CUDA runtime.\n"
        "- A gpu or mixed choice is valid only when the supplied capability snapshot proves a "
        "usable GPU/CUDA runtime and every declared dependency is installed. If not, choose "
        "cpu/none or surface the exact infrastructure blocker; never install dependencies, "
        "silently fall back, or pretend the GPU path ran.\n\n"
    )


def _operator_precondition_decision_instruction(
    state: AutoresearchState,
    phase: Phase,
    expected_artifact_type: ArtifactType,
) -> str:
    if (
        phase is Phase.DECISION_LOG
        and expected_artifact_type is ArtifactType.FINAL_DECISION
        and _is_operator_precondition_consensus(state.latest_consensus)
        and state.implementation_result is None
        and state.latest_verification is None
    ):
        return (
            "Operator-precondition final decision contract:\n"
            "- The latest consensus is a no-code operator precondition, not an "
            "implemented experiment.\n"
            "- Emit final_decision=INFRA_BLOCKED, reviewer_verdict=NOT_RUN, "
            "recommended_metric_value=null, and a concrete infra_rationale.\n"
            "- Set memory_write_required=false. Do not write MemPalace facts for "
            "this no-code transition because no verification_result exists.\n\n"
        )
    return ""


def _mode_contract(state: AutoresearchState) -> str:
    if state.mode is None:
        return (
            "Mode contract:\n"
            "- The context packet must choose exactly alpha_research or data_infra_g0 and give "
            "a nonempty rationale plus burned theory families.\n\n"
        )
    if state.mode is ResearchMode.DATA_INFRA_G0:
        return (
            "Mode contract: DATA_INFRA_G0\n"
            "- Repair data/provenance/folds only. Verification must emit an explicit "
            "infrastructure gate outcome; do not claim alpha performance validation.\n\n"
        )
    return (
        "mode=ALPHA_RESEARCH; strategy experiment. Burned theory families require materially "
        "new evidence. Consensus freezes a strict universe_plan. Persist compact universe, "
        "hydration, and coverage identities/counts/digests only; never full membership arrays. "
        "No fixed-sleeve or per-symbol coverage alternative.\n"
    )


def _mempalace_kg_fact_instruction(
    state: AutoresearchState,
    expected_artifact_type: ArtifactType,
) -> str:
    if expected_artifact_type is not ArtifactType.MEMORY_WRITE:
        return ""
    if state.final_decision is None:
        raise AutoresearchValidationError("MemPalace memory write requires final_decision")
    return (
        "Required standardized MemPalace KG facts:\n"
        "- From the verified authoritative state, derive the exact standardized predicate/object "
        "pairs and final decision subject with the installed runner. Do not re-normalize, "
        "shorten, or regenerate their objects.\n\n"
    )
