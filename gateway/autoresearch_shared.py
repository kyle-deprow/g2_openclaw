"""Shared supervisor-layer values and exception types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from gateway.autoresearch.enums import Phase

AUTORESEARCH_OWNER_AGENT_ID = "autoresearch-pm"
AUTORESEARCH_OWNER_SESSION_KEY = "agent:autoresearch-pm:autoresearch:quantipy"
DEFAULT_GATEWAY_RPC_POLL_INTERVAL_SECONDS = 0.05
READ_ONLY_TASK_LIST_ATTEMPTS = 3
READ_ONLY_TASK_LIST_RETRY_SECONDS = 0.5
REQUIRED_OPENCLAW_VERSION_TEXT = "2026.7.1-2"
DEFAULT_TASK_RPC_TIMEOUT_SECONDS = 30.0
PROVIDER_BLOCKED_ALERT_REASON = "control_plane_provider_blocked"
RELAY_DECAY_ALERT_REASON = "control_plane_native_hook_relay_decayed"


@dataclass(frozen=True, slots=True)
class RecoveryErrorPattern:
    pattern: str
    alert_reason: str | None = None


RECOVERY_ERROR_PATTERNS = (
    RecoveryErrorPattern('no api key found for provider "openai"', PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("no api key found for provider", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("selected model is at capacity", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("model is at capacity", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("selected model is overloaded", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("authentication failed", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("auth rejected", PROVIDER_BLOCKED_ALERT_REASON),
    RecoveryErrorPattern("cli transcript compaction failed"),
    RecoveryErrorPattern("context overflow"),
    RecoveryErrorPattern("prompt too long"),
    RecoveryErrorPattern("maximum context length"),
    RecoveryErrorPattern("maximum context size"),
    RecoveryErrorPattern("too many tokens"),
)
EXPECTED_STAGE_AGENT_IDS: dict[Phase, tuple[str, ...]] = {
    Phase.DEBATE: (
        "debater_microstructure",
        "debater_data",
        "debater_skeptic",
        "debater_theory",
        "debater_implementation",
    ),
    Phase.CONSENSUS: ("consensus_arbiter",),
    Phase.IMPLEMENTATION: ("implementer",),
    Phase.VERIFICATION: (AUTORESEARCH_OWNER_AGENT_ID,),
    Phase.REVIEW: ("reviewer",),
    Phase.FIX_TEST: ("fixer",),
    Phase.DECISION_LOG: (AUTORESEARCH_OWNER_AGENT_ID,),
    Phase.REPEAT: (),
}
RELEVANT_AGENT_IDS = frozenset(
    {
        AUTORESEARCH_OWNER_AGENT_ID,
        "context_curator",
        *(agent_id for agent_ids in EXPECTED_STAGE_AGENT_IDS.values() for agent_id in agent_ids),
    }
)


class SupervisorError(RuntimeError):
    """Base failure for strict autoresearch supervision."""


class SupervisorCheckpointError(SupervisorError):
    """A checkpoint could not be trusted; autonomous writes and wakes must stop."""


class OpenClawUnavailableError(SupervisorError):
    """A transient OpenClaw control-plane operation exhausted bounded retries."""


class ShutdownInterrupted(Exception):
    """A command failure observed after shutdown was already requested."""


class RecoveryStatus(StrEnum):
    READY = "ready"
    IN_FLIGHT = "in_flight"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    EXHAUSTED = "exhausted"
