"""Autoresearch enum types."""

from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    SETUP_CONTEXT = "setup_context"
    DEBATE = "debate"
    CONSENSUS = "consensus"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    REVIEW = "review"
    FIX_TEST = "fix_test"
    DECISION_LOG = "decision_log"
    REPEAT = "repeat"


class ArtifactType(StrEnum):
    SETUP = "setup_context"
    CONTEXT_PACKET = "context_packet"
    DEBATE_RESULT = "debate_result"
    CONSENSUS_RESULT = "consensus_result"
    IMPLEMENTATION_RESULT = "implementation_result"
    VERIFICATION_RESULT = "verification_result"
    REVIEW_RESULT = "review_result"
    FIX_RESULT = "fix_result"
    FINAL_DECISION = "final_decision"
    MEMORY_WRITE = "memory_write"
    NEXT_ITERATION = "next_iteration"


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class ResearchMode(StrEnum):
    """The only two mutually exclusive purposes for an iteration."""

    ALPHA_RESEARCH = "alpha_research"
    DATA_INFRA_G0 = "data_infra_g0"


class ComputeTarget(StrEnum):
    """Execution target selected by an experiment without prescribing it."""

    NONE = "none"
    CPU = "cpu"
    GPU = "gpu"
    MIXED = "mixed"


class ConsensusStatus(StrEnum):
    MAJORITY = "MAJORITY"
    NO_CONSENSUS = "NO_CONSENSUS"
    NONE = "NONE"


class VerificationStatus(StrEnum):
    PASS = "PASS"
    BUG_SIGNAL = "BUG_SIGNAL"
    TEST_FAILURE = "TEST_FAILURE"


class InfraGateOutcome(StrEnum):
    GATE_PASSED = "GATE_PASSED"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"


class ReviewVerdict(StrEnum):
    PASS = "PASS"
    CONDITIONAL_PASS = "CONDITIONAL PASS"
    FAIL = "FAIL"


class FinalReviewerVerdict(StrEnum):
    """The reviewer outcome persisted with a final decision."""

    PASS = "PASS"
    CONDITIONAL_PASS = "CONDITIONAL PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class FinalDecision(StrEnum):
    KEEP = "KEEP"
    SIGNIFICANT_KEEP = "SIGNIFICANT KEEP"
    STRONG_KEEP = "STRONG KEEP"
    DISCARD = "DISCARD"
    CRASH = "CRASH"
    NO_CONSENSUS = "NO_CONSENSUS"
    INFRA_REPAIRED = "INFRA_REPAIRED"
    INFRA_BLOCKED = "INFRA_BLOCKED"


class FixTriggerPhase(StrEnum):
    VERIFICATION = "verification"
    REVIEW = "review"
