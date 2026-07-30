"""Deterministic control-plane for the Quantipy autoresearch loop.

This module owns the fixed phase graph, stage-agent policy validation,
skill/source receipts, artifact validation, and next-action selection.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import tomllib
from bisect import bisect_right
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from ctypes.util import find_library
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast
from urllib.parse import unquote, urlencode, urlparse

from gateway.autoresearch_systemd import SystemdUnitStateError, systemd_unit_is_active
from gateway.mempalace_finalizer import (
    FINAL_MEMORY_SOURCE_FILE,
    FinalMemoryWriter,
    FinalMemoryWriteRequest,
    MempalaceFinalizationError,
    SubprocessFinalMemoryWriter,
    finalization_journal_path,
)

if TYPE_CHECKING:
    from gateway.autoresearch_runs import RunRecord

from gateway.autoresearch_panel_receipts import (
    PANEL_RECEIPT_MAX_BYTES,
    RUN_ENVELOPE_MAX_BYTES,
    PanelReceiptValidationError,
    validate_research_panel_receipt,
)
from gateway.autoresearch_platform_validation import (
    PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL,
    DynamicPriceCoverageReceipt,
    PlatformCoverageStatus,
)
from gateway.autoresearch_readiness import (
    PlatformReadinessManifest,
    ReadinessIdentity,
    ResearchPanelProbeReceipt,
    load_xnys_calendar_evidence,
)

DEFAULT_OPENCLAW_CONFIG_PATH = Path("gateway/openclaw_config/openclaw.json")
DEFAULT_QUANTIPY_ROOT = Path("/home/dev/repos/quantipy")
DEFAULT_AUTORESEARCH_MODEL_WORKSPACE_ROOT = Path(
    "/home/dev/.openclaw/autoresearch/model-workspaces"
)
DEFAULT_AUTORESEARCH_WORKTREE_ROOT = DEFAULT_AUTORESEARCH_MODEL_WORKSPACE_ROOT
DEFAULT_AUTORESEARCH_STAGE_INBOX = Path("/home/dev/.openclaw/autoresearch/stage-inbox")
DEFAULT_AUTORESEARCH_STATE_PATH = Path("/home/dev/.openclaw/autoresearch/quantipy-state.json")
DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT = Path(
    "/home/dev/.openclaw/autoresearch/quantipy-experiment-runs"
)
AUTORESEARCH_LOCK_NAMESPACE = Path("/tmp") / f"g2-openclaw-autoresearch-locks-{os.getuid()}"
G2_OPENCLAW_REPO_ROOT = Path(__file__).resolve().parent.parent
AUTORESEARCH_STATE_SCHEMA_VERSION = 4
EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION = 2
INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION = 3
PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION = 1
LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION = 1
OPENCLAW_LONG_TASK_UNIT_RE = re.compile(r"openclaw-long-task-[0-9]+-[0-9]+\.service\Z")
INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_ENV_VAR = (
    "G2_OPENCLAW_OPERATOR_INTERRUPTED_VERIFICATION_RECOVERY"
)
INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_VALUE = "1"
PLATFORM_RUNTIME_RECOVERY_OPERATOR_ENV_VAR = "G2_OPENCLAW_OPERATOR_PLATFORM_RUNTIME_RECOVERY"
PLATFORM_RUNTIME_RECOVERY_OPERATOR_VALUE = "1"
INSTRUCTION_SOURCE_MANIFEST_VERSION = "g2-openclaw-autoresearch-instruction-manifest-v3"
INSTRUCTION_SOURCE_MANIFEST_DIGEST_DOMAIN = "g2-openclaw.autoresearch.instruction-manifest"
AUTHORITATIVE_STATE_REFERENCE_VERSION = "g2-openclaw-autoresearch-state-reference-v1"
AUTHORITATIVE_STATE_DIGEST_DOMAIN = "g2-openclaw.autoresearch.authoritative-state"
AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN = "g2-openclaw.autoresearch.state-reference"
AUTORESEARCH_STATE_LOCK_DIGEST_DOMAIN = "g2-openclaw.autoresearch.state-lock"
MAX_NEXT_ACTION_PROMPT_BYTES = 32 * 1024
# Keep one KiB of headroom below the immutable transport maximum for path and
# host-probe variation. The hard maximum remains the final fail-closed bound.
NEXT_ACTION_PROMPT_TARGET_BYTES = 31 * 1024
# Artifact files are local control-plane inputs rather than prompt payloads.
# Expanded universe receipts need more than 24 KiB while the next-action prompt
# remains bounded separately by MAX_NEXT_ACTION_PROMPT_BYTES.
MAX_ARTIFACT_FILE_BYTES = 64 * 1024
MAX_STAGE_SUBMISSION_BYTES = MAX_ARTIFACT_FILE_BYTES
CANONICAL_QUANTIPY_PYPROJECT_MAX_BYTES = 64 * 1024
CANONICAL_QUANTIPY_UV_LOCK_MAX_BYTES = 4 * 1024 * 1024
CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES = 1024 * 1024
CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES = 1024 * 1024 * 1024
# Mirror Quantipy's canonical v2 limits: both raw secure reads and normalized
# envelope validation cap run.json at 8 MiB; a nested/standalone panel receipt
# remains independently capped at 4 MiB.
QUANTIPY_PANEL_RECEIPT_MAX_BYTES = PANEL_RECEIPT_MAX_BYTES
QUANTIPY_RUN_ENVELOPE_MAX_BYTES = RUN_ENVELOPE_MAX_BYTES
QUANTIPY_EXPERIMENT_SOURCE_DIGEST_DOMAIN = "quantipy-experiment-source-v1"
QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_BYTES = 1024 * 1024
QUANTIPY_EXPERIMENT_SOURCE_TOTAL_MAX_BYTES = 8 * 1024 * 1024
QUANTIPY_EXPERIMENT_NOTEBOOK_MAX_BYTES = 8 * 1024 * 1024
QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_COUNT = 256
QUANTIPY_EXPERIMENT_SOURCE_PATH_MAX_LENGTH = 1024
QUANTIPY_EXPERIMENT_STAGE_SUMMARY_MAX_LENGTH = 4096
QUANTIPY_EXPERIMENT_FAILURE_MESSAGE_MAX_LENGTH = 2048
QUANTIPY_EXPERIMENT_IDENTITY_PATH_MAX_LENGTH = 4096
QUANTIPY_EXECUTION_NOT_STARTED_TOMBSTONE = ".g2-execution-not-started.json"
MAX_UNIVERSE_SELECTION_DATES = 2200
MAX_UNIVERSE_BATCH_DATES = 32
MAX_UNIVERSE_BATCH_RESULTS = 10_000
MAX_UNIVERSE_MEMBERS_PER_DATE = 1_000
MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS = 600_000
MAX_EXAMPLE_TICKERS = 8
MAX_FIXED_SLEEVE_SYMBOLS = 32
NEXT_SESSION_EXECUTION_POLICY = "next-session-or-later"
MEMBER_UNION_DIGEST_ALGORITHM = "sha256-uppercase-sorted-unique-newline-v1"
XNYS_CALENDAR_IDENTITY = "XNYS"
QUANTIPY_EXPERIMENT_SCHEMA_VERSION = "quantipy-experiment-v2"
QUANTIPY_EXPERIMENT_STAGE_ORDER = ("prepare", "smoke", "feasibility", "model")
QUANTIPY_EXPERIMENT_FAILURE_CATEGORIES = frozenset(
    ("manifest", "preflight", "import", "stage", "filesystem", "panel")
)
QUANTIPY_EXECUTION_NOT_STARTED_REASONS = frozenset(("focused_tests_failed",))
_T = TypeVar("_T")
_OPERATOR_PRECONDITION_MARKERS = (
    "no-code-operator",
    "operator-evidence-precondition",
)
_OPERATOR_PRECONDITION_BRIEF_MARKERS = (
    "do not enter engineer",
    "do not modify quantipy",
)
HYDRATE_CAPABLE_COMMAND_RE = re.compile(
    r"(\bqp\.prices\s*\(|\bquantipy\.prices\s*\(|\bprices\s*\(|"
    r"generate_[\w.-]*results|nbconvert|papermill|jupyter\s+execute)"
)


@dataclass(frozen=True, slots=True)
class QuantipyExecutionContract:
    """The sole argv/cwd contract for a canonical Quantipy runtime launch."""

    runtime_root: Path
    manifest_path: Path
    output_root: Path
    run_id: str

    @property
    def working_directory(self) -> Path:
        return self.runtime_root

    @property
    def command(self) -> tuple[str, ...]:
        return (
            "env",
            "PYTHONDONTWRITEBYTECODE=1",
            "uv",
            "--directory",
            str(self.runtime_root),
            "run",
            "--frozen",
            "--no-sync",
            "quantipy",
            "experiment",
            "run",
            str(self.manifest_path),
            "--output-root",
            str(self.output_root),
            "--run-id",
            self.run_id,
        )


def build_quantipy_execution_contract(
    *,
    runtime_root: Path,
    manifest_path: Path,
    output_root: Path,
    run_id: str,
) -> QuantipyExecutionContract:
    """Build the direct-argv canonical-runtime execution contract once."""
    canonical_runtime = _require_canonical_absolute_path(
        runtime_root, label="canonical Quantipy runtime root"
    )
    canonical_manifest = _require_canonical_absolute_path(
        manifest_path, label="immutable Quantipy experiment manifest"
    )
    canonical_output = _require_canonical_absolute_path(
        output_root, label="trusted Quantipy runs root"
    )
    if re.fullmatch(r"autoresearch-i[1-9][0-9]*-[0-9a-f]{7,12}(?:-v5)?", run_id) is None:
        raise AutoresearchValidationError("Quantipy execution contract run_id is invalid")
    return QuantipyExecutionContract(
        runtime_root=canonical_runtime,
        manifest_path=canonical_manifest,
        output_root=canonical_output,
        run_id=run_id,
    )


def _build_historical_v2_quantipy_execution_contract(
    *,
    runtime_root: Path,
    manifest_path: Path,
    output_root: Path,
    run_id: str,
) -> QuantipyExecutionContract:
    """Reconstruct only the exact retired v2 command for sealed recovery evidence."""
    if re.fullmatch(r"autoresearch-i[1-9][0-9]*-[0-9a-f]{7,12}-v2", run_id) is None:
        raise AutoresearchValidationError("historical Quantipy v2 run_id is invalid")
    return QuantipyExecutionContract(
        runtime_root=_require_canonical_absolute_path(
            runtime_root, label="historical canonical Quantipy runtime root"
        ),
        manifest_path=_require_canonical_absolute_path(
            manifest_path, label="historical immutable Quantipy experiment manifest"
        ),
        output_root=_require_canonical_absolute_path(
            output_root, label="historical trusted Quantipy runs root"
        ),
        run_id=run_id,
    )


class AutoresearchError(ValueError):
    """Base error for deterministic autoresearch control-plane failures."""


class AutoresearchConfigError(AutoresearchError):
    """Raised when autoresearch runtime config deviates from policy."""


class AutoresearchReceiptError(AutoresearchError):
    """Raised when a required source receipt cannot be generated."""


class AutoresearchValidationError(AutoresearchError):
    """Raised when an artifact or state is invalid."""


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


OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME = "operator-infrastructure-suspension"
OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE = (
    "Operator suspended the active iteration for infrastructure repair."
)
OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY = (
    "Active iteration suspended for operator infrastructure repair."
)
OPERATOR_INFRASTRUCTURE_SUSPENSION_ACTIVE_PHASES = frozenset(
    (
        Phase.DEBATE,
        Phase.CONSENSUS,
        Phase.IMPLEMENTATION,
        Phase.VERIFICATION,
        Phase.REVIEW,
        Phase.FIX_TEST,
        Phase.DECISION_LOG,
    )
)


class FixTriggerPhase(StrEnum):
    VERIFICATION = "verification"
    REVIEW = "review"


KEEP_DECISIONS = frozenset(
    {FinalDecision.KEEP, FinalDecision.SIGNIFICANT_KEEP, FinalDecision.STRONG_KEEP}
)
MEMPALACE_CONFIG_PLACEHOLDER = "PLACEHOLDER_RESOLVED_BY_PUSH_SCRIPT"
MEMPALACE_READONLY_SERVER_ID = "mempalace-readonly"
MEMPALACE_READONLY_WRAPPER_BASENAME = "mempalace-readonly-server.py"
G2_CONTROL_SERVER_ID = "g2-control"
G2_CONTROL_MODULE = "gateway.g2_control_mcp_server"
G2_CONTROL_TOOL_NAMES = (
    "g2_autoresearch_status",
    "g2_autoresearch_start",
    "g2_autoresearch_stop",
)
MEMPALACE_KG_OBJECT_MAX_LENGTH = 128
MEMPALACE_KG_OBJECT_SHA256_LENGTH = 64
MEMPALACE_READONLY_DISPLAY_NAMESPACE = MEMPALACE_READONLY_SERVER_ID
MEMPALACE_READONLY_RUNTIME_NAMESPACE = MEMPALACE_READONLY_SERVER_ID
G2_CONTROL_RUNTIME_NAMESPACE = G2_CONTROL_SERVER_ID
MEMPALACE_READONLY_TOOL_NAMES = (
    "mempalace_status",
    "mempalace_search",
    "mempalace_get_drawer",
    "mempalace_list_drawers",
    "mempalace_list_wings",
    "mempalace_list_rooms",
    "mempalace_get_taxonomy",
    "mempalace_get_aaak_spec",
    "mempalace_diary_read",
    "mempalace_kg_query",
    "mempalace_kg_timeline",
    "mempalace_kg_stats",
    "mempalace_traverse",
    "mempalace_find_tunnels",
    "mempalace_follow_tunnels",
    "mempalace_graph_stats",
    "mempalace_list_tunnels",
    "mempalace_list_hallways",
    "mempalace_memories_filed_away",
)


def _compile_mempalace_codex_display_tool_ids(
    tool_names: Sequence[str],
    *,
    namespace: str,
) -> tuple[str, ...]:
    return tuple(f"{namespace}.{tool_name}" for tool_name in tool_names)


def _compile_codex_mcp_runtime_tool_ids(
    tool_names: Sequence[str],
    *,
    namespace: str,
) -> tuple[str, ...]:
    return tuple(f"{namespace}__{tool_name}" for tool_name in tool_names)


PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS = (
    "sessions_spawn",
    "sessions_yield",
    "agents_list",
    "sessions_list",
    "sessions_history",
)
MEMPALACE_READONLY_DISPLAY_TOOL_IDS = _compile_mempalace_codex_display_tool_ids(
    MEMPALACE_READONLY_TOOL_NAMES,
    namespace=MEMPALACE_READONLY_DISPLAY_NAMESPACE,
)
G2_CONTROL_DISPLAY_TOOL_IDS = _compile_mempalace_codex_display_tool_ids(
    G2_CONTROL_TOOL_NAMES,
    namespace=G2_CONTROL_SERVER_ID,
)
MEMPALACE_READONLY_RUNTIME_TOOL_IDS = _compile_codex_mcp_runtime_tool_ids(
    MEMPALACE_READONLY_TOOL_NAMES,
    namespace=MEMPALACE_READONLY_RUNTIME_NAMESPACE,
)
G2_CONTROL_RUNTIME_TOOL_IDS = _compile_codex_mcp_runtime_tool_ids(
    G2_CONTROL_TOOL_NAMES,
    namespace=G2_CONTROL_RUNTIME_NAMESPACE,
)
MAIN_ALLOWED_TOOL_IDS = (*G2_CONTROL_RUNTIME_TOOL_IDS, *MEMPALACE_READONLY_RUNTIME_TOOL_IDS)
MAIN_OPENCLAW_TOOL_ALLOW_POLICY = MAIN_ALLOWED_TOOL_IDS
LEGACY_AUTORESEARCH_WORKTREE_ROOT = Path("/home/dev/.openclaw/autoresearch/worktrees")
DEFAULT_ALLOWED_TARGET_STATUS_LINES = ("?? docs/quantipy_experiment_mempalace_preload.md",)


def _ensure_mapping(raw: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise AutoresearchValidationError(f"{label} must be an object")
    return raw


def _require_exact_keys(raw: Mapping[str, object], *, label: str, expected: Sequence[str]) -> None:
    expected_keys = set(expected)
    actual_keys = set(raw)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        raise AutoresearchValidationError(
            f"{label} must contain exact keys; missing={missing}, unexpected={unexpected}"
        )


def _require_str(raw: Mapping[str, object], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise AutoresearchValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_workspace_path(raw: Mapping[str, object], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise AutoresearchValidationError(f"{field_name} must be a non-empty string")
    _validate_workspace_path(value, label=field_name)
    return value


def _validate_workspace_path(value: str, *, label: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise AutoresearchValidationError(f"{label} must not contain ASCII control characters")
    if not Path(value).expanduser().is_absolute():
        raise AutoresearchValidationError(f"{label} must be absolute")


def _validate_persisted_autoresearch_workspace_path(value: str, *, label: str) -> None:
    """Apply a filesystem-free lexical policy to persisted workspace evidence."""
    _validate_workspace_path(value, label=label)
    workspace_path = Path(value)
    if (
        not workspace_path.is_absolute()
        or value != str(workspace_path)
        or any(part in {".", ".."} for part in workspace_path.parts)
    ):
        raise AutoresearchValidationError(f"{label} must be an absolute lexically canonical path")
    try:
        workspace_path.relative_to(DEFAULT_AUTORESEARCH_WORKTREE_ROOT)
    except ValueError as exc:
        raise AutoresearchValidationError(
            f"{label} must be under the canonical autoresearch worktree root"
        ) from exc


def _render_literal(value: str) -> str:
    """Render untrusted prompt/error values as a single JSON string literal."""
    return json.dumps(value, ensure_ascii=True)


def _require_bool(raw: Mapping[str, object], field_name: str) -> bool:
    value = raw.get(field_name)
    if not isinstance(value, bool):
        raise AutoresearchValidationError(f"{field_name} must be a boolean")
    return value


def _require_int(raw: Mapping[str, object], field_name: str) -> int:
    value = raw.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AutoresearchValidationError(f"{field_name} must be an integer")
    return value


def _require_float(raw: Mapping[str, object], field_name: str) -> float:
    value = raw.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AutoresearchValidationError(f"{field_name} must be numeric")
    return float(value)


def _optional_int(raw: Mapping[str, object], field_name: str) -> int | None:
    if field_name not in raw:
        raise AutoresearchValidationError(f"{field_name} must be an integer or null")
    value = raw[field_name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AutoresearchValidationError(f"{field_name} must be an integer or null")
    return value


def _optional_float(raw: Mapping[str, object], field_name: str) -> float | None:
    if field_name not in raw:
        raise AutoresearchValidationError(f"{field_name} must be numeric or null")
    value = raw[field_name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AutoresearchValidationError(f"{field_name} must be numeric or null")
    return float(value)


def _require_string_list(raw: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    value = raw.get(field_name)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise AutoresearchValidationError(f"{field_name} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AutoresearchValidationError(f"{field_name} must be a list of strings")
        items.append(item)
    return tuple(items)


def _require_string_sequence(raw: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise AutoresearchValidationError(f"{label} must be a list of strings")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise AutoresearchValidationError(f"{label} must be a list of strings")
        items.append(item)
    return tuple(items)


def _optional_string_list(raw: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    if field_name not in raw:
        return ()
    return _require_string_list(raw, field_name)


def _validate_sha256(value: str, *, label: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AutoresearchValidationError(f"{label} must be a lowercase SHA-256 digest")


def _require_sha256(raw: Mapping[str, object], field_name: str) -> str:
    value = _require_str(raw, field_name)
    _validate_sha256(value, label=field_name)
    return value


def _validate_iso_date_value(value: str, *, label: str) -> None:
    try:
        from datetime import date

        date.fromisoformat(value)
    except ValueError as exc:
        raise AutoresearchValidationError(f"{label} must be an ISO-8601 date") from exc


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json_digest(value: object) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def canonical_member_union_digest(tickers: Sequence[str]) -> tuple[int, str]:
    """SHA-256 uppercase sorted-unique symbols joined by LF, including final LF."""
    canonical = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    if not canonical:
        raise AutoresearchValidationError("member union must contain at least one ticker")
    payload = canonical_member_union_manifest(canonical)
    return len(canonical), hashlib.sha256(payload).hexdigest()


def canonical_member_union_manifest(tickers: Sequence[str]) -> bytes:
    """Return canonical UTF-8 union bytes with one symbol per line and a trailing LF."""
    canonical = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    if not canonical:
        raise AutoresearchValidationError("member union must contain at least one ticker")
    return "".join(f"{ticker}\n" for ticker in canonical).encode("utf-8")


def quantipy_member_union_digest(tickers: Sequence[str]) -> tuple[int, str]:
    """SHA-256 over Quantipy's canonical compact JSON array member-union body."""
    canonical = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    if not canonical:
        raise AutoresearchValidationError("member union must contain at least one ticker")
    return len(canonical), _canonical_json_digest(canonical)


def price_hydration_request_digest(
    *,
    member_union_count: int,
    member_union_digest: str,
    experiment_start: str,
    experiment_end: str,
    timeframe: str,
    market_hours: str,
) -> str:
    return _canonical_json_digest(
        {
            "member_union_count": member_union_count,
            "member_union_digest": member_union_digest,
            "experiment_start": experiment_start,
            "experiment_end": experiment_end,
            "timeframe": timeframe,
            "market_hours": market_hours,
        }
    )


def price_hydration_coverage_digest(
    *, request_digest: str, operation_count: int, completed_at: str
) -> str:
    return _canonical_json_digest(
        {
            "request_digest": request_digest,
            "operation_count": operation_count,
            "completed_at": completed_at,
        }
    )


def platform_requested_sessions_digest(requested_sessions: Sequence[date]) -> str:
    """Mirror Quantipy's digest of the ordered XNYS session-label sequence."""
    if not requested_sessions or any(type(session) is not date for session in requested_sessions):
        raise AutoresearchValidationError(
            "requested sessions must be a non-empty sequence of plain dates"
        )
    canonical = tuple(requested_sessions)
    if canonical != tuple(sorted(set(canonical))):
        raise AutoresearchValidationError(
            "requested sessions must be unique and canonically ordered"
        )
    return _canonical_json_digest([session.isoformat() for session in canonical])


def _platform_receipt_has_expected_runner_provenance(
    receipt: DynamicPriceCoverageReceipt,
    *,
    preflight: PriceHydrationScopePreflight,
    universe: UniverseVerificationReceipt,
    hydration: PriceHydrationReceipt,
    requested_sessions: Sequence[date] | None = None,
) -> bool:
    """Return whether a canonical receipt is independently bound to runner evidence."""
    try:
        receipt.validate()
        preflight.validate()
        hydration.validate_against_universe(universe)
        member_union_symbols = _verify_member_union_manifest(universe)
    except ValueError:
        return False
    try:
        quantipy_member_union_count, quantipy_member_union_sha256 = quantipy_member_union_digest(
            member_union_symbols
        )
    except ValueError:
        return False
    sessions_match = True
    if requested_sessions is not None:
        sessions = tuple(requested_sessions)
        try:
            receipt.validate_requested_sessions(sessions)
        except ValueError:
            sessions_match = False
    return (
        receipt.matches_shared_contract
        and receipt.timeframe == "1min"
        and receipt.source_timeframe == "1min"
        and receipt.requested_start_date == preflight.experiment_start
        and receipt.requested_end_date == preflight.experiment_end
        and receipt.source_requested_start_date == preflight.experiment_start
        and receipt.source_requested_end_date == preflight.experiment_end
        and receipt.timeframe == preflight.timeframe == hydration.timeframe
        and receipt.market_hours.value == preflight.market_hours == hydration.market_hours
        and receipt.source_timeframe == preflight.timeframe
        and receipt.source_market_hours.value == preflight.market_hours
        and receipt.member_union_count
        == preflight.member_union_count
        == universe.member_union_count
        == hydration.member_union_count
        == quantipy_member_union_count
        and receipt.requested_session_count == preflight.session_count
        and receipt.hydrated_symbol_sessions == preflight.planned_symbol_sessions
        and universe.member_union_digest == hydration.member_union_digest
        and receipt.member_union_digest == quantipy_member_union_sha256
        and receipt.source_price_coverage_response_digest
        == hydration.source_price_coverage_response_digest
        and sessions_match
    )


def _parse_timestamp(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutoresearchValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutoresearchValidationError(f"{label} must include a UTC offset")
    return parsed


def _normalise_identifier(value: str) -> str:
    """Return the sole documented identifier form: lowercase kebab-case."""
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", normalized):
        raise AutoresearchValidationError(
            "identifier must normalize to lowercase kebab-case beginning with a letter"
        )
    return normalized


def _require_canonical_identifier(raw: Mapping[str, object], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value:
        raise AutoresearchValidationError(f"{field_name} must be a non-empty string")
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value):
        raise AutoresearchValidationError(
            f"{field_name} must be canonical lowercase kebab-case beginning with a letter"
        )
    return value


def _normalise_predicate(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def standardize_mempalace_kg_object(value: str) -> str:
    """Normalize a KG object and compact it to MemPalace's 128-character limit."""
    normalized = _normalise_predicate(value)
    if len(normalized) <= MEMPALACE_KG_OBJECT_MAX_LENGTH:
        return normalized
    prefix_length = MEMPALACE_KG_OBJECT_MAX_LENGTH - MEMPALACE_KG_OBJECT_SHA256_LENGTH - 1
    return f"{normalized[:prefix_length]}_{_sha256_text(normalized)}"


def validate_target_worktree_clean(
    status_lines: Sequence[str],
    *,
    allowed_status_lines: Sequence[str] = DEFAULT_ALLOWED_TARGET_STATUS_LINES,
) -> None:
    """Fail if the target repo has unapproved dirty files.

    The autoresearch loop may choose any strategy, but each stage must start
    from an uncontaminated target repo. Known persistent local docs can be
    allowlisted explicitly; crash residue and late writer output cannot.
    """
    allowed = set(allowed_status_lines)
    unexpected = tuple(line for line in status_lines if line and line not in allowed)
    if unexpected:
        details = "\n".join(f"- {line}" for line in unexpected)
        raise AutoresearchValidationError(
            "target repo worktree is dirty with unapproved changes:\n"
            f"{details}\n"
            "Stop stale writers and clean or commit the target repo before "
            "launching the next autoresearch stage."
        )


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    receipt_id: str
    path: Path
    sha256: str

    @property
    def label(self) -> str:
        return self.path.name

    def to_dict(self) -> dict[str, str]:
        return {
            "receipt_id": self.receipt_id,
            "path": str(self.path),
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class InstructionSourceEntry:
    receipt_id: str
    path: str
    sha256: str

    @classmethod
    def from_receipt(cls, receipt: SourceReceipt) -> InstructionSourceEntry:
        return cls(
            receipt_id=receipt.receipt_id,
            path=str(receipt.path),
            sha256=receipt.sha256,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "receipt_id": self.receipt_id,
            "path": self.path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeStateReference:
    """Digest-bound location of the complete state required by a stage agent."""

    version: str
    digest_domain: str
    path: str
    state_sha256: str
    phase: str
    iteration: int

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "digest_domain": self.digest_domain,
            "path": self.path,
            "state_sha256": self.state_sha256,
            "phase": self.phase,
            "iteration": self.iteration,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return _sha256_text(
            "\n".join(
                (
                    AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN,
                    self.version,
                    self.canonical_json(),
                )
            )
        )


@dataclass(frozen=True, slots=True)
class InstructionSourceManifest:
    version: str
    digest_domain: str
    phase: str
    expected_artifact_type: str
    target_agent_ids: tuple[str, ...]
    target_repo_root: str
    state_reference: AuthoritativeStateReference
    sources: tuple[InstructionSourceEntry, ...]

    @classmethod
    def from_context(
        cls,
        *,
        phase: Phase,
        expected_artifact_type: ArtifactType,
        target_agent_ids: Sequence[str],
        target_repo_root: Path,
        state: AutoresearchState,
        state_path: Path,
        receipts: Sequence[SourceReceipt],
    ) -> InstructionSourceManifest:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for receipt in receipts:
            if receipt.receipt_id in seen:
                duplicates.add(receipt.receipt_id)
            seen.add(receipt.receipt_id)
        if duplicates:
            raise AutoresearchReceiptError(
                "duplicate instruction source receipt ids: " + ", ".join(sorted(duplicates))
            )
        ordered = tuple(sorted(receipts, key=lambda receipt: receipt.receipt_id))
        return cls(
            version=INSTRUCTION_SOURCE_MANIFEST_VERSION,
            digest_domain=INSTRUCTION_SOURCE_MANIFEST_DIGEST_DOMAIN,
            phase=phase.value,
            expected_artifact_type=expected_artifact_type.value,
            target_agent_ids=tuple(target_agent_ids),
            target_repo_root=str(target_repo_root.expanduser().resolve(strict=False)),
            state_reference=build_authoritative_state_reference(state, state_path=state_path),
            sources=tuple(InstructionSourceEntry.from_receipt(item) for item in ordered),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "digest_domain": self.digest_domain,
            "phase": self.phase,
            "expected_artifact_type": self.expected_artifact_type,
            "target_agent_ids": list(self.target_agent_ids),
            "target_repo_root": self.target_repo_root,
            "state_reference": self.state_reference.to_dict(),
            "sources": [source.to_dict() for source in self.sources],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return _sha256_text(
            "\n".join(
                (
                    self.digest_domain,
                    self.version,
                    self.canonical_json(),
                )
            )
        )


def build_instruction_source_manifest(
    *,
    phase: Phase,
    expected_artifact_type: ArtifactType,
    target_agent_ids: Sequence[str],
    target_repo_root: Path,
    state: AutoresearchState,
    state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
    receipts: Sequence[SourceReceipt],
) -> InstructionSourceManifest:
    return InstructionSourceManifest.from_context(
        phase=phase,
        expected_artifact_type=expected_artifact_type,
        target_agent_ids=target_agent_ids,
        target_repo_root=target_repo_root,
        state=state,
        state_path=state_path,
        receipts=receipts,
    )


def instruction_source_manifest_sha256(
    *,
    phase: Phase,
    expected_artifact_type: ArtifactType,
    target_agent_ids: Sequence[str],
    target_repo_root: Path,
    state: AutoresearchState,
    state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
    receipts: Sequence[SourceReceipt],
) -> str:
    return build_instruction_source_manifest(
        phase=phase,
        expected_artifact_type=expected_artifact_type,
        target_agent_ids=target_agent_ids,
        target_repo_root=target_repo_root,
        state=state,
        state_path=state_path,
        receipts=receipts,
    ).sha256()


def expected_instruction_manifest_sha256(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    *,
    state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
) -> str:
    target = _select_phase_target(state, policy)
    required_receipts = receipts.require(PHASE_RECEIPTS[state.phase])
    return instruction_source_manifest_sha256(
        phase=state.phase,
        expected_artifact_type=target.artifact_type,
        target_agent_ids=target.agent_ids,
        target_repo_root=_target_repo_root_for_state(state),
        state=state,
        state_path=state_path,
        receipts=required_receipts,
    )


def _target_repo_root_for_state(state: AutoresearchState) -> Path:
    target_repo = (
        Path(state.setup.target_repo) if state.setup is not None else DEFAULT_QUANTIPY_ROOT
    )
    return target_repo.expanduser().resolve(strict=False)


@dataclass(frozen=True, slots=True)
class StageAgentPolicy:
    agent_id: str
    model: str
    reasoning: str
    skills: tuple[str, ...]

    def to_summary(self) -> str:
        skill_text = ", ".join(self.skills)
        return f"{self.agent_id}: {self.model} / {self.reasoning} / skills=[{skill_text}]"


@dataclass(frozen=True, slots=True)
class ComputeCapabilitySnapshot:
    """Read-only host capability probe supplied to research stages."""

    cpu_model: str
    logical_cpus: int
    memory_gib: float | None
    target_python_available: bool
    gpu_available: bool
    gpu_name: str | None
    gpu_vram_gib: float | None
    cuda_runtime_available: bool
    installed_gpu_packages: tuple[str, ...]
    probe_errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "cpu_model": self.cpu_model,
            "logical_cpus": self.logical_cpus,
            "memory_gib": self.memory_gib,
            "target_python_available": self.target_python_available,
            "gpu_available": self.gpu_available,
            "gpu_name": self.gpu_name,
            "gpu_vram_gib": self.gpu_vram_gib,
            "cuda_runtime_available": self.cuda_runtime_available,
            "installed_gpu_packages": list(self.installed_gpu_packages),
            "probe_errors": list(self.probe_errors),
        }


@dataclass(frozen=True, slots=True)
class ComputeFitArtifact:
    """Machine-readable experiment compute choice and its justification."""

    target: ComputeTarget
    rationale: str
    required_dependencies: tuple[str, ...]
    benchmark_plan: str

    @classmethod
    def from_dict(cls, raw: object) -> ComputeFitArtifact:
        data = _ensure_mapping(raw, label="compute_fit")
        _require_exact_keys(
            data,
            label="compute_fit",
            expected=("target", "rationale", "required_dependencies", "benchmark_plan"),
        )
        artifact = cls(
            target=ComputeTarget(_require_str(data, "target")),
            rationale=_require_str(data, "rationale"),
            required_dependencies=_require_string_list(data, "required_dependencies"),
            benchmark_plan=_require_str(data, "benchmark_plan"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        if not self.rationale.strip():
            raise AutoresearchValidationError("compute_fit rationale must be non-empty")
        if not self.benchmark_plan.strip():
            raise AutoresearchValidationError("compute_fit benchmark_plan must be non-empty")
        if self.target is ComputeTarget.NONE and self.required_dependencies:
            raise AutoresearchValidationError(
                "compute_fit target=none cannot require compute dependencies"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "rationale": self.rationale,
            "required_dependencies": list(self.required_dependencies),
            "benchmark_plan": self.benchmark_plan,
        }


_GPU_PROBE_MODULES = (
    "torch",
    "tensorflow",
    "jax",
    "cupy",
    "cudf",
    "xgboost",
    "lightgbm",
    "catboost",
)


def _probe_installed_gpu_packages(target_repo: Path, errors: list[str]) -> tuple[str, ...]:
    python = _target_python_path(target_repo)
    if not python.is_file():
        errors.append(f"Quantipy virtualenv not found: {python}")
        return ()
    code = (
        "from importlib.util import find_spec; "
        "mods=('torch','tensorflow','jax','cupy','cudf','xgboost','lightgbm','catboost'); "
        "print(' '.join(m for m in mods if find_spec(m) is not None))"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", code],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"GPU package probe failed: {type(exc).__name__}")
        return ()
    if result.returncode != 0:
        errors.append("GPU package probe returned a nonzero exit code")
        return ()
    return tuple(sorted(set(result.stdout.split()) & set(_GPU_PROBE_MODULES)))


def _target_python_path(target_repo: Path) -> Path:
    return target_repo.expanduser().resolve() / ".venv" / "bin" / "python"


def _read_memory_gib(errors: list[str]) -> float | None:
    try:
        memory_info = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"memory probe failed: {type(exc).__name__}")
        return None
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB$", memory_info, re.MULTILINE)
    if match is None:
        errors.append("memory probe returned no MemTotal")
        return None
    return round(int(match.group(1)) / 1024 / 1024, 2)


def _probe_nvidia(errors: list[str]) -> tuple[bool, str | None, float | None]:
    if shutil.which("nvidia-smi") is None:
        errors.append("nvidia-smi is not installed")
        return False, None, None
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"nvidia-smi probe failed: {type(exc).__name__}")
        return False, None, None
    if result.returncode != 0 or not result.stdout.strip():
        errors.append("nvidia-smi returned no usable GPU")
        return False, None, None
    name, _, memory = result.stdout.strip().splitlines()[0].partition(",")
    try:
        vram_gib = round(float(memory.strip()) / 1024, 2)
    except ValueError:
        errors.append("nvidia-smi returned an invalid memory value")
        return False, name.strip() or None, None
    return True, name.strip() or None, vram_gib


def _probe_cuda_runtime(gpu_available: bool, errors: list[str]) -> bool:
    if not gpu_available:
        return False
    if find_library("cuda") is None:
        errors.append("CUDA driver library is not available")
        return False
    return True


def collect_compute_capability_snapshot(
    target_repo: Path = DEFAULT_QUANTIPY_ROOT,
) -> ComputeCapabilitySnapshot:
    """Collect non-mutating host and target-venv compute capabilities."""
    errors: list[str] = []
    gpu_available, gpu_name, gpu_vram_gib = _probe_nvidia(errors)
    target_python_available = _target_python_path(target_repo).is_file()
    cuda_runtime_available = _probe_cuda_runtime(gpu_available, errors)
    return ComputeCapabilitySnapshot(
        cpu_model=platform.processor() or platform.machine() or "unknown",
        logical_cpus=os.cpu_count() or 1,
        memory_gib=_read_memory_gib(errors),
        target_python_available=target_python_available,
        gpu_available=gpu_available,
        gpu_name=gpu_name,
        gpu_vram_gib=gpu_vram_gib,
        cuda_runtime_available=cuda_runtime_available,
        installed_gpu_packages=_probe_installed_gpu_packages(target_repo, errors),
        probe_errors=tuple(errors),
    )


def _validate_compute_fit_environment(
    compute_fit: ComputeFitArtifact,
    target_repo: Path,
) -> None:
    compute_fit.validate()
    if compute_fit.target not in {ComputeTarget.GPU, ComputeTarget.MIXED}:
        return
    snapshot = collect_compute_capability_snapshot(target_repo)
    if snapshot.probe_errors:
        raise AutoresearchValidationError(
            "compute_fit selected GPU execution, but the capability probe failed: "
            + "; ".join(snapshot.probe_errors)
        )
    if not snapshot.target_python_available:
        raise AutoresearchValidationError(
            "compute_fit selected GPU execution, but the target Quantipy virtualenv is unavailable"
        )
    if not snapshot.gpu_available or not snapshot.cuda_runtime_available:
        raise AutoresearchValidationError(
            "compute_fit selected GPU execution, but the capability probe found no "
            "usable GPU/CUDA runtime"
        )
    available = set(snapshot.installed_gpu_packages)
    if snapshot.cuda_runtime_available:
        available.add("cuda_runtime")
    missing = sorted(
        dependency
        for dependency in compute_fit.required_dependencies
        if dependency not in available
    )
    if missing:
        raise AutoresearchValidationError(
            "compute_fit selected GPU execution with unavailable dependencies: "
            + ", ".join(missing)
        )


@dataclass(frozen=True, slots=True)
class AutoresearchPolicy:
    pm: StageAgentPolicy
    main_interface: StageAgentPolicy
    context_curator: StageAgentPolicy
    debate_agents: tuple[StageAgentPolicy, ...]
    consensus: StageAgentPolicy
    implementer: StageAgentPolicy
    reviewer: StageAgentPolicy
    fixer: StageAgentPolicy

    @property
    def debate_agent_ids(self) -> tuple[str, ...]:
        return tuple(agent.agent_id for agent in self.debate_agents)

    @property
    def all_stage_agent_ids(self) -> tuple[str, ...]:
        return (
            self.context_curator.agent_id,
            *self.debate_agent_ids,
            self.consensus.agent_id,
            self.implementer.agent_id,
            self.reviewer.agent_id,
            self.fixer.agent_id,
        )

    def model_policy_summary(self) -> str:
        """Render an indexed, lossless policy without repeating shared values."""
        agents = (
            self.pm,
            self.main_interface,
            self.context_curator,
            *self.debate_agents,
            self.consensus,
            self.implementer,
            self.reviewer,
            self.fixer,
        )
        skill_sets: list[tuple[str, ...]] = []
        models: list[str] = []
        reasoning_levels: list[str] = []
        for agent in agents:
            if agent.skills not in skill_sets:
                skill_sets.append(agent.skills)
            if agent.model not in models:
                models.append(agent.model)
            if agent.reasoning not in reasoning_levels:
                reasoning_levels.append(agent.reasoning)
        payload: dict[str, object] = {
            "agent_format": (
                "agent_id",
                "model_index",
                "reasoning_index",
                "skill_set_index",
            ),
            "models": models,
            "reasoning_levels": reasoning_levels,
            "skill_sets": skill_sets,
            "agents": [
                (
                    agent.agent_id,
                    models.index(agent.model),
                    reasoning_levels.index(agent.reasoning),
                    skill_sets.index(agent.skills),
                )
                for agent in agents
            ],
        }
        return _compact_json_block(payload)


@dataclass(frozen=True, slots=True)
class ReceiptCatalog:
    receipts: dict[str, SourceReceipt]

    def require(self, receipt_ids: Sequence[str]) -> tuple[SourceReceipt, ...]:
        ordered: list[SourceReceipt] = []
        for receipt_id in receipt_ids:
            try:
                ordered.append(self.receipts[receipt_id])
            except KeyError as exc:
                raise AutoresearchReceiptError(f"missing receipt id: {receipt_id}") from exc
        return tuple(ordered)


@dataclass(frozen=True, slots=True)
class SetupContextArtifact:
    goal: str
    metric_name: str
    metric_direction: MetricDirection
    target_repo: str
    writable_scope: str
    baseline_summary: str
    hard_constraints: tuple[str, ...]
    data_sources: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> SetupContextArtifact:
        data = _ensure_mapping(raw, label="setup_context")
        _require_exact_keys(
            data,
            label="setup_context",
            expected=(
                "goal",
                "metric_name",
                "metric_direction",
                "target_repo",
                "writable_scope",
                "baseline_summary",
                "hard_constraints",
                "data_sources",
            ),
        )
        artifact = cls(
            goal=_require_str(data, "goal"),
            metric_name=_require_str(data, "metric_name"),
            metric_direction=MetricDirection(_require_str(data, "metric_direction")),
            target_repo=_require_str(data, "target_repo"),
            writable_scope=_require_str(data, "writable_scope"),
            baseline_summary=_require_str(data, "baseline_summary"),
            hard_constraints=_optional_string_list(data, "hard_constraints"),
            data_sources=_optional_string_list(data, "data_sources"),
        )
        return artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "goal": self.goal,
            "metric_name": self.metric_name,
            "metric_direction": self.metric_direction.value,
            "target_repo": self.target_repo,
            "writable_scope": self.writable_scope,
            "baseline_summary": self.baseline_summary,
            "hard_constraints": list(self.hard_constraints),
            "data_sources": list(self.data_sources),
        }


@dataclass(frozen=True, slots=True)
class ContextPacketArtifact:
    baseline_metric: str
    current_best_metric: str
    recent_experiment_outcomes: tuple[str, ...]
    prior_findings: tuple[str, ...]
    open_proposals: tuple[str, ...]
    hard_constraints: tuple[str, ...]
    available_data_sources: tuple[str, ...]
    loaded_quantipy_sources: tuple[str, ...]
    research_mode: ResearchMode
    mode_rationale: str
    burned_theory_families: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> ContextPacketArtifact:
        data = _ensure_mapping(raw, label="context_packet")
        _require_exact_keys(
            data,
            label="context_packet",
            expected=(
                "baseline_metric",
                "current_best_metric",
                "recent_experiment_outcomes",
                "prior_findings",
                "open_proposals",
                "hard_constraints",
                "available_data_sources",
                "loaded_quantipy_sources",
                "research_mode",
                "mode_rationale",
                "burned_theory_families",
            ),
        )
        return cls(
            baseline_metric=_require_str(data, "baseline_metric"),
            current_best_metric=_require_str(data, "current_best_metric"),
            recent_experiment_outcomes=_require_string_list(data, "recent_experiment_outcomes"),
            prior_findings=_require_string_list(data, "prior_findings"),
            open_proposals=_require_string_list(data, "open_proposals"),
            hard_constraints=_require_string_list(data, "hard_constraints"),
            available_data_sources=_require_string_list(data, "available_data_sources"),
            loaded_quantipy_sources=_require_string_list(data, "loaded_quantipy_sources"),
            research_mode=ResearchMode(_require_str(data, "research_mode")),
            mode_rationale=_require_str(data, "mode_rationale"),
            burned_theory_families=tuple(
                _normalise_identifier(family)
                for family in _require_string_list(data, "burned_theory_families")
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline_metric": self.baseline_metric,
            "current_best_metric": self.current_best_metric,
            "recent_experiment_outcomes": list(self.recent_experiment_outcomes),
            "prior_findings": list(self.prior_findings),
            "open_proposals": list(self.open_proposals),
            "hard_constraints": list(self.hard_constraints),
            "available_data_sources": list(self.available_data_sources),
            "loaded_quantipy_sources": list(self.loaded_quantipy_sources),
            "research_mode": self.research_mode.value,
            "mode_rationale": self.mode_rationale,
            "burned_theory_families": list(self.burned_theory_families),
        }


@dataclass(frozen=True, slots=True)
class UniversePlanArtifact:
    profile_id: str
    profile_digest: str
    selection_dates: tuple[str, ...]
    max_members_per_date: int
    execution_policy: str

    @classmethod
    def from_dict(cls, raw: object) -> UniversePlanArtifact:
        data = _ensure_mapping(raw, label="universe_plan")
        _require_exact_keys(
            data,
            label="universe_plan",
            expected=(
                "profile_id",
                "profile_digest",
                "selection_dates",
                "max_members_per_date",
                "execution_policy",
            ),
        )
        artifact = cls(
            profile_id=_require_str(data, "profile_id"),
            profile_digest=_require_sha256(data, "profile_digest"),
            selection_dates=_require_string_list(data, "selection_dates"),
            max_members_per_date=_require_int(data, "max_members_per_date"),
            execution_policy=_require_str(data, "execution_policy"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        _validate_sha256(self.profile_digest, label="profile_digest")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.profile_id):
            raise AutoresearchValidationError("universe plan profile_id must be kebab-case")
        if not self.selection_dates:
            raise AutoresearchValidationError("universe plan requires selection dates")
        if len(self.selection_dates) > MAX_UNIVERSE_SELECTION_DATES:
            raise AutoresearchValidationError(
                f"universe plan allows at most {MAX_UNIVERSE_SELECTION_DATES} selection dates"
            )
        for selection_date in self.selection_dates:
            _validate_iso_date_value(selection_date, label="selection_date")
        if tuple(sorted(set(self.selection_dates))) != self.selection_dates:
            raise AutoresearchValidationError(
                "universe plan selection_dates must be sorted and unique"
            )
        if not 1 <= self.max_members_per_date <= MAX_UNIVERSE_MEMBERS_PER_DATE:
            raise AutoresearchValidationError(
                "universe plan max_members_per_date must be between 1 and 1000"
            )
        if self.execution_policy != NEXT_SESSION_EXECUTION_POLICY:
            raise AutoresearchValidationError(
                f"universe plan execution_policy must be {NEXT_SESSION_EXECUTION_POLICY}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_digest": self.profile_digest,
            "selection_dates": list(self.selection_dates),
            "max_members_per_date": self.max_members_per_date,
            "execution_policy": self.execution_policy,
        }


@dataclass(frozen=True, slots=True)
class DebateSubmission:
    agent_id: str
    theory_id: str
    theory_family: str
    vote_family: str
    hypothesis: str
    universe: str
    example_tickers: tuple[str, ...]
    feature_pipeline: str
    model_plan: str
    walk_forward_plan: str
    transaction_cost_model: str
    data_coverage_plan: str
    rejection_criteria: str
    objections: tuple[str, ...]
    compute_fit: ComputeFitArtifact | None = None
    materially_new_evidence: str | None = None

    @classmethod
    def from_dict(cls, raw: object) -> DebateSubmission:
        data = _ensure_mapping(raw, label="debate_submission")
        _require_exact_keys(
            data,
            label="debate_submission",
            expected=(
                "agent_id",
                "theory_id",
                "theory_family",
                "vote_family",
                "hypothesis",
                "universe",
                "example_tickers",
                "feature_pipeline",
                "model_plan",
                "walk_forward_plan",
                "transaction_cost_model",
                "data_coverage_plan",
                "rejection_criteria",
                "objections",
                "compute_fit",
                "materially_new_evidence",
            ),
        )
        exemption = data.get("materially_new_evidence")
        if exemption is not None and not isinstance(exemption, str):
            raise AutoresearchValidationError("materially_new_evidence must be a string or null")
        compute_fit_raw = data.get("compute_fit")
        artifact = cls(
            agent_id=_require_str(data, "agent_id"),
            theory_id=_require_str(data, "theory_id"),
            theory_family=_require_str(data, "theory_family"),
            vote_family=_require_str(data, "vote_family"),
            hypothesis=_require_str(data, "hypothesis"),
            universe=_require_str(data, "universe"),
            example_tickers=_require_string_list(data, "example_tickers"),
            feature_pipeline=_require_str(data, "feature_pipeline"),
            model_plan=_require_str(data, "model_plan"),
            walk_forward_plan=_require_str(data, "walk_forward_plan"),
            transaction_cost_model=_require_str(data, "transaction_cost_model"),
            data_coverage_plan=_require_str(data, "data_coverage_plan"),
            rejection_criteria=_require_str(data, "rejection_criteria"),
            objections=_require_string_list(data, "objections"),
            compute_fit=(
                ComputeFitArtifact.from_dict(compute_fit_raw)
                if compute_fit_raw is not None
                else None
            ),
            materially_new_evidence=exemption.strip() if isinstance(exemption, str) else None,
        )
        if len(artifact.example_tickers) > MAX_EXAMPLE_TICKERS:
            raise AutoresearchValidationError("example_tickers allows at most 8 symbols")
        if tuple(sorted(set(artifact.example_tickers))) != artifact.example_tickers:
            raise AutoresearchValidationError("example_tickers must be sorted and unique")
        return artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "theory_id": self.theory_id,
            "theory_family": self.theory_family,
            "vote_family": self.vote_family,
            "hypothesis": self.hypothesis,
            "universe": self.universe,
            "example_tickers": list(self.example_tickers),
            "feature_pipeline": self.feature_pipeline,
            "model_plan": self.model_plan,
            "walk_forward_plan": self.walk_forward_plan,
            "transaction_cost_model": self.transaction_cost_model,
            "data_coverage_plan": self.data_coverage_plan,
            "rejection_criteria": self.rejection_criteria,
            "objections": list(self.objections),
            "compute_fit": self.compute_fit.to_dict() if self.compute_fit is not None else None,
            "materially_new_evidence": self.materially_new_evidence,
        }


@dataclass(frozen=True, slots=True)
class DebateResultArtifact:
    round_number: int
    submissions: tuple[DebateSubmission, ...]

    @classmethod
    def from_dict(cls, raw: object) -> DebateResultArtifact:
        data = _ensure_mapping(raw, label="debate_result")
        _require_exact_keys(data, label="debate_result", expected=("round_number", "submissions"))
        submissions_raw = data.get("submissions")
        if not isinstance(submissions_raw, Sequence) or isinstance(submissions_raw, str | bytes):
            raise AutoresearchValidationError("submissions must be a list")
        submissions = tuple(DebateSubmission.from_dict(item) for item in submissions_raw)
        if len(submissions) != 5:
            raise AutoresearchValidationError("debate_result must contain exactly 5 submissions")
        return cls(round_number=_require_int(data, "round_number"), submissions=submissions)

    def to_dict(self) -> dict[str, object]:
        return {
            "round_number": self.round_number,
            "submissions": [submission.to_dict() for submission in self.submissions],
        }


@dataclass(frozen=True, slots=True)
class ConsensusResultArtifact:
    round_number: int
    status: ConsensusStatus
    winner_theory_id: str | None
    winner_theory_family: str | None
    majority_count: int
    majority_agent_ids: tuple[str, ...]
    dissenting_positions: tuple[str, ...]
    novelty_score: float
    theory_score: float
    implementation_risk_score: float
    data_adequacy_score: float
    overfit_risk_score: float
    expected_net_sharpe: float
    rejection_reasons: tuple[str, ...]
    implementation_brief: str | None
    dissent_summary: str
    universe_plan: UniversePlanArtifact | None = None

    @classmethod
    def from_dict(cls, raw: object) -> ConsensusResultArtifact:
        data = _ensure_mapping(raw, label="consensus_result")
        _require_exact_keys(
            data,
            label="consensus_result",
            expected=(
                "round_number",
                "status",
                "winner_theory_id",
                "winner_theory_family",
                "majority_count",
                "majority_agent_ids",
                "dissenting_positions",
                "novelty_score",
                "theory_score",
                "implementation_risk_score",
                "data_adequacy_score",
                "overfit_risk_score",
                "expected_net_sharpe",
                "rejection_reasons",
                "implementation_brief",
                "dissent_summary",
                "universe_plan",
            ),
        )
        winner_theory_id = data.get("winner_theory_id")
        winner_theory_family = data.get("winner_theory_family")
        implementation_brief = data.get("implementation_brief")
        if "universe_plan" not in data:
            raise AutoresearchValidationError("universe_plan must be an object or null")
        universe_plan_raw = data["universe_plan"]
        if winner_theory_id is not None and not isinstance(winner_theory_id, str):
            raise AutoresearchValidationError("winner_theory_id must be a string or null")
        if winner_theory_family is not None and not isinstance(winner_theory_family, str):
            raise AutoresearchValidationError("winner_theory_family must be a string or null")
        if implementation_brief is not None and not isinstance(implementation_brief, str):
            raise AutoresearchValidationError("implementation_brief must be a string or null")
        artifact = cls(
            round_number=_require_int(data, "round_number"),
            status=ConsensusStatus(_require_str(data, "status")),
            winner_theory_id=winner_theory_id,
            winner_theory_family=winner_theory_family,
            majority_count=_require_int(data, "majority_count"),
            majority_agent_ids=_require_string_list(data, "majority_agent_ids"),
            dissenting_positions=_require_string_list(data, "dissenting_positions"),
            novelty_score=_require_float(data, "novelty_score"),
            theory_score=_require_float(data, "theory_score"),
            implementation_risk_score=_require_float(data, "implementation_risk_score"),
            data_adequacy_score=_require_float(data, "data_adequacy_score"),
            overfit_risk_score=_require_float(data, "overfit_risk_score"),
            expected_net_sharpe=_require_float(data, "expected_net_sharpe"),
            rejection_reasons=_require_string_list(data, "rejection_reasons"),
            implementation_brief=implementation_brief.strip()
            if isinstance(implementation_brief, str)
            else None,
            dissent_summary=_require_str(data, "dissent_summary"),
            universe_plan=(
                UniversePlanArtifact.from_dict(universe_plan_raw)
                if universe_plan_raw is not None
                else None
            ),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        if self.status is ConsensusStatus.MAJORITY:
            if self.majority_count < 3:
                raise AutoresearchValidationError("majority consensus requires majority_count >= 3")
            if not self.winner_theory_id or not self.winner_theory_family:
                raise AutoresearchValidationError("majority consensus requires a winner")
            if not self.implementation_brief:
                raise AutoresearchValidationError(
                    "majority consensus requires an implementation_brief"
                )
        else:
            if self.majority_count >= 3:
                raise AutoresearchValidationError("NO_CONSENSUS cannot report a 3-of-5 majority")
            if (
                self.winner_theory_id
                or self.winner_theory_family
                or self.implementation_brief
                or self.universe_plan is not None
            ):
                raise AutoresearchValidationError(
                    "NO_CONSENSUS must not include winner, implementation brief, or universe plan"
                )
        if self.universe_plan is not None:
            self.universe_plan.validate()

    def to_dict(self) -> dict[str, object]:
        return {
            "round_number": self.round_number,
            "status": self.status.value,
            "winner_theory_id": self.winner_theory_id,
            "winner_theory_family": self.winner_theory_family,
            "majority_count": self.majority_count,
            "majority_agent_ids": list(self.majority_agent_ids),
            "dissenting_positions": list(self.dissenting_positions),
            "novelty_score": self.novelty_score,
            "theory_score": self.theory_score,
            "implementation_risk_score": self.implementation_risk_score,
            "data_adequacy_score": self.data_adequacy_score,
            "overfit_risk_score": self.overfit_risk_score,
            "expected_net_sharpe": self.expected_net_sharpe,
            "rejection_reasons": list(self.rejection_reasons),
            "implementation_brief": self.implementation_brief,
            "dissent_summary": self.dissent_summary,
            "universe_plan": self.universe_plan.to_dict()
            if self.universe_plan is not None
            else None,
        }


def _is_operator_precondition_consensus(
    consensus: ConsensusResultArtifact | None,
) -> bool:
    """Return true for a majority that deliberately requires operator action."""
    if consensus is None or consensus.status is not ConsensusStatus.MAJORITY:
        return False
    id_text = " ".join(
        value.lower()
        for value in (
            consensus.winner_theory_id,
            consensus.winner_theory_family,
        )
        if value
    )
    if any(marker in id_text for marker in _OPERATOR_PRECONDITION_MARKERS):
        return True
    brief = (consensus.implementation_brief or "").lower()
    return all(marker in brief for marker in _OPERATOR_PRECONDITION_BRIEF_MARKERS)


@dataclass(frozen=True, slots=True)
class PriceHydrationScopePreflight:
    member_union_count: int
    experiment_start: str
    experiment_end: str
    timeframe: str
    market_hours: str
    session_count: int
    planned_symbol_sessions: int
    within_budget: bool

    @classmethod
    def from_dict(cls, raw: object) -> PriceHydrationScopePreflight:
        data = _ensure_mapping(raw, label="price_hydration_scope_preflight")
        _require_exact_keys(
            data,
            label="price_hydration_scope_preflight",
            expected=(
                "member_union_count",
                "experiment_start",
                "experiment_end",
                "timeframe",
                "market_hours",
                "session_count",
                "planned_symbol_sessions",
                "within_budget",
            ),
        )
        receipt = cls(
            member_union_count=_require_int(data, "member_union_count"),
            experiment_start=_require_iso_date(data, "experiment_start"),
            experiment_end=_require_iso_date(data, "experiment_end"),
            timeframe=_require_str(data, "timeframe"),
            market_hours=_require_str(data, "market_hours"),
            session_count=_require_int(data, "session_count"),
            planned_symbol_sessions=_require_int(data, "planned_symbol_sessions"),
            within_budget=_require_bool(data, "within_budget"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_iso_date_value(self.experiment_start, label="experiment_start")
        _validate_iso_date_value(self.experiment_end, label="experiment_end")
        if self.experiment_start > self.experiment_end:
            raise AutoresearchValidationError("price preflight experiment range is invalid")
        if self.member_union_count <= 0:
            raise AutoresearchValidationError("price preflight member_union_count must be positive")
        if self.session_count <= 0:
            raise AutoresearchValidationError("price preflight session_count must be positive")
        expected = self.member_union_count * self.session_count
        if self.planned_symbol_sessions != expected:
            raise AutoresearchValidationError(
                "price preflight planned_symbol_sessions must equal "
                "member_union_count * session_count"
            )
        expected_within_budget = expected <= MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS
        if self.within_budget is not expected_within_budget:
            raise AutoresearchValidationError(
                "price preflight within_budget must match the alpha hydration budget"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "member_union_count": self.member_union_count,
            "experiment_start": self.experiment_start,
            "experiment_end": self.experiment_end,
            "timeframe": self.timeframe,
            "market_hours": self.market_hours,
            "session_count": self.session_count,
            "planned_symbol_sessions": self.planned_symbol_sessions,
            "within_budget": self.within_budget,
        }


@dataclass(frozen=True, slots=True)
class QuantipyExperimentFailureEvidence:
    category: str
    message: str

    @classmethod
    def from_dict(cls, raw: object) -> QuantipyExperimentFailureEvidence:
        data = _ensure_mapping(raw, label="quantipy_experiment_evidence.failure")
        _require_exact_keys(
            data,
            label="quantipy_experiment_evidence.failure",
            expected=("category", "message"),
        )
        failure = cls(
            category=_require_str(data, "category"),
            message=_require_str(data, "message"),
        )
        failure.validate()
        return failure

    def validate(self) -> None:
        if self.category not in QUANTIPY_EXPERIMENT_FAILURE_CATEGORIES:
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.failure.category is not a Quantipy failure category"
            )

    def to_dict(self) -> dict[str, object]:
        return {"category": self.category, "message": self.message}


@dataclass(frozen=True, slots=True)
class QuantipyExperimentPanelEvidence:
    panel_path: str
    panel_sha256: str
    receipt_path: str
    receipt_sha256: str
    request_sha256: str
    coverage_sha256: str

    @classmethod
    def from_dict(cls, raw: object) -> QuantipyExperimentPanelEvidence:
        data = _ensure_mapping(raw, label="quantipy_experiment_evidence.panel")
        _require_exact_keys(
            data,
            label="quantipy_experiment_evidence.panel",
            expected=(
                "panel_path",
                "panel_sha256",
                "receipt_path",
                "receipt_sha256",
                "request_sha256",
                "coverage_sha256",
            ),
        )
        evidence = cls(
            panel_path=_require_str(data, "panel_path"),
            panel_sha256=_require_sha256(data, "panel_sha256"),
            receipt_path=_require_str(data, "receipt_path"),
            receipt_sha256=_require_sha256(data, "receipt_sha256"),
            request_sha256=_require_sha256(data, "request_sha256"),
            coverage_sha256=_require_sha256(data, "coverage_sha256"),
        )
        evidence.validate()
        return evidence

    def validate(self) -> None:
        if self.panel_path != "panel/panel.parquet":
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.panel.panel_path must be panel/panel.parquet"
            )
        if self.receipt_path != "panel/receipt.json":
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.panel.receipt_path must be panel/receipt.json"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "panel_path": self.panel_path,
            "panel_sha256": self.panel_sha256,
            "receipt_path": self.receipt_path,
            "receipt_sha256": self.receipt_sha256,
            "request_sha256": self.request_sha256,
            "coverage_sha256": self.coverage_sha256,
        }


@dataclass(frozen=True, slots=True)
class QuantipyExperimentEvidence:
    manifest_path: str
    manifest_sha256: str
    detached_run_directory: str
    detached_run_manifest_sha256: str
    run_id: str
    run_json_path: str
    run_json_sha256: str
    success: bool
    completed_stages: tuple[str, ...]
    terminal_stage: str | None
    terminal_status: str | None
    failure: QuantipyExperimentFailureEvidence | None
    panel: QuantipyExperimentPanelEvidence | None

    @classmethod
    def from_dict(cls, raw: object) -> QuantipyExperimentEvidence:
        data = _ensure_mapping(raw, label="quantipy_experiment_evidence")
        _require_exact_keys(
            data,
            label="quantipy_experiment_evidence",
            expected=(
                "manifest_path",
                "manifest_sha256",
                "detached_run_directory",
                "detached_run_manifest_sha256",
                "run_id",
                "run_json_path",
                "run_json_sha256",
                "success",
                "completed_stages",
                "terminal_stage",
                "terminal_status",
                "failure",
                "panel",
            ),
        )
        failure_raw = data.get("failure")
        panel_raw = data.get("panel")
        evidence = cls(
            manifest_path=_require_workspace_path(data, "manifest_path"),
            manifest_sha256=_require_sha256(data, "manifest_sha256"),
            detached_run_directory=_require_workspace_path(data, "detached_run_directory"),
            detached_run_manifest_sha256=_require_sha256(
                data,
                "detached_run_manifest_sha256",
            ),
            run_id=_require_str(data, "run_id"),
            run_json_path=_require_workspace_path(data, "run_json_path"),
            run_json_sha256=_require_sha256(data, "run_json_sha256"),
            success=_require_bool(data, "success"),
            completed_stages=_require_string_list(data, "completed_stages"),
            terminal_stage=(
                _require_str(data, "terminal_stage")
                if data.get("terminal_stage") is not None
                else None
            ),
            terminal_status=(
                _require_str(data, "terminal_status")
                if data.get("terminal_status") is not None
                else None
            ),
            failure=(
                QuantipyExperimentFailureEvidence.from_dict(failure_raw)
                if failure_raw is not None
                else None
            ),
            panel=(
                QuantipyExperimentPanelEvidence.from_dict(panel_raw)
                if panel_raw is not None
                else None
            ),
        )
        evidence.validate()
        return evidence

    def validate(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.run_id) is None:
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.run_id must be a safe Quantipy run ID"
            )
        if self.completed_stages != QUANTIPY_EXPERIMENT_STAGE_ORDER[: len(self.completed_stages)]:
            raise AutoresearchValidationError(
                "quantipy_experiment_evidence.completed_stages must be an ordered Quantipy prefix"
            )
        if self.success and self.completed_stages != QUANTIPY_EXPERIMENT_STAGE_ORDER:
            raise AutoresearchValidationError(
                "successful Quantipy experiment evidence requires all four completed stages"
            )
        if self.success and self.failure is not None:
            raise AutoresearchValidationError(
                "successful Quantipy experiment evidence cannot contain failure evidence"
            )
        if self.success and (self.terminal_stage is not None or self.terminal_status is not None):
            raise AutoresearchValidationError(
                "successful Quantipy experiment evidence cannot contain a terminal failure stage"
            )
        if (self.terminal_stage is None) != (self.terminal_status is None):
            raise AutoresearchValidationError(
                "Quantipy experiment terminal stage and status must both be present or null"
            )
        if (
            self.terminal_stage is not None
            and self.terminal_stage not in QUANTIPY_EXPERIMENT_STAGE_ORDER
        ):
            raise AutoresearchValidationError("Quantipy experiment terminal stage is invalid")
        if self.terminal_status is not None and self.terminal_status not in {"rejected", "failed"}:
            raise AutoresearchValidationError("Quantipy experiment terminal status is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "detached_run_directory": self.detached_run_directory,
            "detached_run_manifest_sha256": self.detached_run_manifest_sha256,
            "run_id": self.run_id,
            "run_json_path": self.run_json_path,
            "run_json_sha256": self.run_json_sha256,
            "success": self.success,
            "completed_stages": list(self.completed_stages),
            "terminal_stage": self.terminal_stage,
            "terminal_status": self.terminal_status,
            "failure": self.failure.to_dict() if self.failure is not None else None,
            "panel": self.panel.to_dict() if self.panel is not None else None,
        }


@dataclass(frozen=True, slots=True)
class QuantipyExecutionNotStartedEvidence:
    manifest_path: str
    manifest_sha256: str
    expected_run_id: str
    expected_run_json_path: str
    reason: str
    command: str
    evidence: str

    @classmethod
    def from_dict(cls, raw: object) -> QuantipyExecutionNotStartedEvidence:
        data = _ensure_mapping(raw, label="quantipy_execution_not_started")
        _require_exact_keys(
            data,
            label="quantipy_execution_not_started",
            expected=(
                "manifest_path",
                "manifest_sha256",
                "expected_run_id",
                "expected_run_json_path",
                "reason",
                "command",
                "evidence",
            ),
        )
        receipt = cls(
            manifest_path=_require_workspace_path(data, "manifest_path"),
            manifest_sha256=_require_sha256(data, "manifest_sha256"),
            expected_run_id=_require_str(data, "expected_run_id"),
            expected_run_json_path=_require_workspace_path(data, "expected_run_json_path"),
            reason=_require_str(data, "reason"),
            command=_require_str(data, "command"),
            evidence=_require_str(data, "evidence"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        if self.reason not in QUANTIPY_EXECUTION_NOT_STARTED_REASONS:
            raise AutoresearchValidationError(
                "quantipy_execution_not_started.reason must be focused_tests_failed"
            )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.expected_run_id) is None:
            raise AutoresearchValidationError(
                "quantipy_execution_not_started.expected_run_id is invalid"
            )
        if not self.command.strip() or not self.evidence.strip():
            raise AutoresearchValidationError(
                "quantipy_execution_not_started requires exact command and evidence"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "expected_run_id": self.expected_run_id,
            "expected_run_json_path": self.expected_run_json_path,
            "reason": self.reason,
            "command": self.command,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class ImplementationResultArtifact:
    summary: str
    workspace_path: str
    commit_sha: str
    module_path: str
    notebook_path: str
    tests_added_or_updated: tuple[str, ...]
    commands_run: tuple[str, ...]
    experiment_manifest_path: str = str(
        DEFAULT_AUTORESEARCH_WORKTREE_ROOT / "unverified" / "experiment-manifest.json"
    )
    experiment_manifest_sha256: str = "0" * 64
    compute_fit: ComputeFitArtifact | None = None
    price_hydration_scope_preflight: PriceHydrationScopePreflight | None = None

    @classmethod
    def from_dict(cls, raw: object) -> ImplementationResultArtifact:
        data = _ensure_mapping(raw, label="implementation_result")
        _require_exact_keys(
            data,
            label="implementation_result",
            expected=(
                "summary",
                "workspace_path",
                "commit_sha",
                "module_path",
                "notebook_path",
                "tests_added_or_updated",
                "commands_run",
                "experiment_manifest_path",
                "experiment_manifest_sha256",
                "compute_fit",
                "price_hydration_scope_preflight",
            ),
        )
        compute_fit_raw = data.get("compute_fit")
        preflight_raw = data.get("price_hydration_scope_preflight")
        artifact = cls(
            summary=_require_str(data, "summary"),
            workspace_path=_require_workspace_path(data, "workspace_path"),
            commit_sha=_require_str(data, "commit_sha"),
            module_path=_require_str(data, "module_path"),
            notebook_path=_require_str(data, "notebook_path"),
            tests_added_or_updated=_require_string_list(data, "tests_added_or_updated"),
            commands_run=_require_string_list(data, "commands_run"),
            experiment_manifest_path=_require_workspace_path(data, "experiment_manifest_path"),
            experiment_manifest_sha256=_require_sha256(data, "experiment_manifest_sha256"),
            compute_fit=(
                ComputeFitArtifact.from_dict(compute_fit_raw)
                if compute_fit_raw is not None
                else None
            ),
            price_hydration_scope_preflight=(
                PriceHydrationScopePreflight.from_dict(preflight_raw)
                if preflight_raw is not None
                else None
            ),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        _validate_workspace_path(
            self.workspace_path,
            label="implementation_result workspace_path",
        )
        if not re.fullmatch(r"[0-9a-f]{7,40}", self.commit_sha):
            raise AutoresearchValidationError(
                "implementation_result commit_sha must be a Git commit SHA"
            )
        _validate_workspace_path(
            self.experiment_manifest_path,
            label="implementation_result experiment_manifest_path",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "workspace_path": self.workspace_path,
            "commit_sha": self.commit_sha,
            "module_path": self.module_path,
            "notebook_path": self.notebook_path,
            "tests_added_or_updated": list(self.tests_added_or_updated),
            "commands_run": list(self.commands_run),
            "experiment_manifest_path": self.experiment_manifest_path,
            "experiment_manifest_sha256": self.experiment_manifest_sha256,
            "compute_fit": self.compute_fit.to_dict() if self.compute_fit is not None else None,
            "price_hydration_scope_preflight": (
                self.price_hydration_scope_preflight.to_dict()
                if self.price_hydration_scope_preflight is not None
                else None
            ),
        }


def _require_iso_date(raw: Mapping[str, object], field_name: str) -> str:
    value = _require_str(raw, field_name)
    _validate_iso_date_value(value, label=field_name)
    return value


@dataclass(frozen=True, slots=True)
class AutoresearchValidationContext:
    readiness_identity: ReadinessIdentity | None
    xnys_evidence_digest: str
    xnys_sessions: tuple[date, ...]
    xnys_range_start: date | None = None
    xnys_range_end: date | None = None
    quantipy_commit: str | None = None

    @classmethod
    def from_readiness(cls, readiness: PlatformReadinessManifest) -> AutoresearchValidationContext:
        try:
            digest, evidence = load_xnys_calendar_evidence(readiness)
            identity = readiness.require_ready()
        except ValueError as exc:
            raise AutoresearchValidationError(str(exc)) from exc
        quantipy_commit = identity.quantipy_commit
        if quantipy_commit is None:
            raise AutoresearchValidationError(
                "READY platform identity requires the pinned Quantipy commit"
            )
        sessions = evidence.sessions
        if not sessions:
            raise AutoresearchValidationError("XNYS calendar evidence contains no sessions")
        return cls(
            identity,
            digest,
            sessions,
            quantipy_commit=quantipy_commit,
            xnys_range_start=evidence.range_start,
            xnys_range_end=evidence.range_end,
        )

    def validate_for_state(self, state: AutoresearchState) -> None:
        state_identity = state.platform_readiness
        context_identity = self.readiness_identity
        if state_identity != context_identity:
            raise AutoresearchValidationError(
                "validation context readiness identity must match pinned state readiness"
            )

    def validate_universe_receipt(self, receipt: UniverseVerificationReceipt) -> None:
        _verify_member_union_manifest(receipt)
        for item in (date_receipt for batch in receipt.batches for date_receipt in batch.dates):
            if item.calendar_identity != XNYS_CALENDAR_IDENTITY:
                raise AutoresearchValidationError("calendar_identity must be XNYS")
            if item.calendar_digest != self.xnys_evidence_digest:
                raise AutoresearchValidationError(
                    "calendar_digest must exactly match pinned readiness XNYS evidence"
                )
            selection_date = date.fromisoformat(item.selection_date)
            index = bisect_right(self.xnys_sessions, selection_date)
            if index == len(self.xnys_sessions):
                raise AutoresearchValidationError(
                    "XNYS evidence does not cover a session after selection_date"
                )
            expected = self.xnys_sessions[index].isoformat()
            if item.earliest_execution_date != expected:
                raise AutoresearchValidationError(
                    "earliest_execution_date must equal the first actual XNYS session "
                    f"after selection_date ({expected})"
                )


@dataclass(frozen=True, slots=True)
class AuthoritativeSnapshotReceipt:
    as_of_date: str
    source: str
    result_count: int
    identity_digest: str
    content_digest: str
    completed_at: str

    @classmethod
    def from_dict(cls, raw: object) -> AuthoritativeSnapshotReceipt:
        data = _ensure_mapping(raw, label="authoritative_snapshot_receipt")
        _require_exact_keys(
            data,
            label="authoritative_snapshot_receipt",
            expected=(
                "as_of_date",
                "source",
                "result_count",
                "identity_digest",
                "content_digest",
                "completed_at",
            ),
        )
        receipt = cls(
            as_of_date=_require_iso_date(data, "as_of_date"),
            source=_require_str(data, "source"),
            result_count=_require_int(data, "result_count"),
            identity_digest=_require_sha256(data, "identity_digest"),
            content_digest=_require_sha256(data, "content_digest"),
            completed_at=_require_str(data, "completed_at"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_iso_date_value(self.as_of_date, label="as_of_date")
        _validate_sha256(self.identity_digest, label="identity_digest")
        _validate_sha256(self.content_digest, label="content_digest")
        _parse_timestamp(self.completed_at, label="completed_at")
        if self.result_count < 0:
            raise AutoresearchValidationError("snapshot result_count must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of_date": self.as_of_date,
            "source": self.source,
            "result_count": self.result_count,
            "identity_digest": self.identity_digest,
            "content_digest": self.content_digest,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class GroupedSummaryReceipt:
    summary_date: str
    source: str
    result_count: int
    identity_digest: str
    content_digest: str
    completed_at: str
    adjusted: bool

    @classmethod
    def from_dict(cls, raw: object) -> GroupedSummaryReceipt:
        data = _ensure_mapping(raw, label="grouped_summary_receipt")
        _require_exact_keys(
            data,
            label="grouped_summary_receipt",
            expected=(
                "summary_date",
                "source",
                "result_count",
                "identity_digest",
                "content_digest",
                "completed_at",
                "adjusted",
            ),
        )
        receipt = cls(
            summary_date=_require_iso_date(data, "summary_date"),
            source=_require_str(data, "source"),
            result_count=_require_int(data, "result_count"),
            identity_digest=_require_sha256(data, "identity_digest"),
            content_digest=_require_sha256(data, "content_digest"),
            completed_at=_require_str(data, "completed_at"),
            adjusted=_require_bool(data, "adjusted"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_iso_date_value(self.summary_date, label="summary_date")
        _validate_sha256(self.identity_digest, label="identity_digest")
        _validate_sha256(self.content_digest, label="content_digest")
        _parse_timestamp(self.completed_at, label="completed_at")
        if self.result_count < 0:
            raise AutoresearchValidationError("summary result_count must be non-negative")
        if self.adjusted:
            raise AutoresearchValidationError("grouped summary receipt requires adjusted=false")

    def to_dict(self) -> dict[str, object]:
        return {
            "summary_date": self.summary_date,
            "source": self.source,
            "result_count": self.result_count,
            "identity_digest": self.identity_digest,
            "content_digest": self.content_digest,
            "completed_at": self.completed_at,
            "adjusted": self.adjusted,
        }


@dataclass(frozen=True, slots=True)
class UniverseDateVerificationReceipt:
    selection_date: str
    earliest_execution_date: str
    calendar_identity: str
    calendar_digest: str
    selected_member_count: int
    snapshot: AuthoritativeSnapshotReceipt
    summary: GroupedSummaryReceipt

    @classmethod
    def from_dict(cls, raw: object) -> UniverseDateVerificationReceipt:
        data = _ensure_mapping(raw, label="universe_date_verification_receipt")
        _require_exact_keys(
            data,
            label="universe_date_verification_receipt",
            expected=(
                "selection_date",
                "earliest_execution_date",
                "calendar_identity",
                "calendar_digest",
                "selected_member_count",
                "snapshot",
                "summary",
            ),
        )
        receipt = cls(
            selection_date=_require_iso_date(data, "selection_date"),
            earliest_execution_date=_require_iso_date(data, "earliest_execution_date"),
            calendar_identity=_require_str(data, "calendar_identity"),
            calendar_digest=_require_sha256(data, "calendar_digest"),
            selected_member_count=_require_int(data, "selected_member_count"),
            snapshot=AuthoritativeSnapshotReceipt.from_dict(data["snapshot"]),
            summary=GroupedSummaryReceipt.from_dict(data["summary"]),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_iso_date_value(self.selection_date, label="selection_date")
        _validate_iso_date_value(self.earliest_execution_date, label="earliest_execution_date")
        _validate_sha256(self.calendar_digest, label="calendar_digest")
        if self.selected_member_count < 0:
            raise AutoresearchValidationError("selected_member_count must be non-negative")
        if self.earliest_execution_date <= self.selection_date:
            raise AutoresearchValidationError(
                "earliest_execution_date must be after selection_date"
            )
        self.snapshot.validate()
        self.summary.validate()
        if self.snapshot.as_of_date != self.selection_date:
            raise AutoresearchValidationError(
                "snapshot receipt as_of_date must match selection_date"
            )
        if self.summary.summary_date != self.selection_date:
            raise AutoresearchValidationError(
                "summary receipt summary_date must match selection_date"
            )
        if self.snapshot.source != self.summary.source:
            raise AutoresearchValidationError(
                "snapshot and summary sources must match for each selection date"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "selection_date": self.selection_date,
            "earliest_execution_date": self.earliest_execution_date,
            "calendar_identity": self.calendar_identity,
            "calendar_digest": self.calendar_digest,
            "selected_member_count": self.selected_member_count,
            "snapshot": self.snapshot.to_dict(),
            "summary": self.summary.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class UniverseHistoryBatchReceipt:
    contract_digest: str
    operation_count: int
    dates: tuple[UniverseDateVerificationReceipt, ...]

    @classmethod
    def from_dict(cls, raw: object) -> UniverseHistoryBatchReceipt:
        data = _ensure_mapping(raw, label="universe_history_batch_receipt")
        _require_exact_keys(
            data,
            label="universe_history_batch_receipt",
            expected=("contract_digest", "operation_count", "dates"),
        )
        dates_raw = data["dates"]
        if not isinstance(dates_raw, Sequence) or isinstance(dates_raw, str | bytes):
            raise AutoresearchValidationError("batch dates must be a list")
        receipt = cls(
            contract_digest=_require_sha256(data, "contract_digest"),
            operation_count=_require_int(data, "operation_count"),
            dates=tuple(UniverseDateVerificationReceipt.from_dict(item) for item in dates_raw),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_sha256(self.contract_digest, label="contract_digest")
        if self.operation_count != 1:
            raise AutoresearchValidationError(
                "each universe history batch requires operation_count=1"
            )
        if not 1 <= len(self.dates) <= MAX_UNIVERSE_BATCH_DATES:
            raise AutoresearchValidationError("universe history batch requires 1 to 32 dates")
        for receipt in self.dates:
            receipt.validate()
        dates = tuple(receipt.selection_date for receipt in self.dates)
        if tuple(sorted(set(dates))) != dates:
            raise AutoresearchValidationError(
                "universe history batch dates must be sorted and unique"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_digest": self.contract_digest,
            "operation_count": self.operation_count,
            "dates": [receipt.to_dict() for receipt in self.dates],
        }


@dataclass(frozen=True, slots=True)
class MemberUnionManifestReceipt:
    path: str
    sha256: str

    @classmethod
    def from_dict(cls, raw: object) -> MemberUnionManifestReceipt:
        data = _ensure_mapping(raw, label="member_union_manifest_receipt")
        _require_exact_keys(
            data,
            label="member_union_manifest_receipt",
            expected=("path", "sha256"),
        )
        receipt = cls(path=_require_str(data, "path"), sha256=_require_sha256(data, "sha256"))
        receipt.validate()
        return receipt

    def validate(self) -> None:
        if not Path(self.path).is_absolute():
            raise AutoresearchValidationError("member union manifest path must be absolute")
        _validate_sha256(self.sha256, label="member union manifest sha256")

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class UniverseVerificationReceipt:
    profile_id: str
    profile_digest: str
    execution_policy: str
    max_members_per_date: int
    batches: tuple[UniverseHistoryBatchReceipt, ...]
    member_union_digest_algorithm: str
    member_union_count: int
    member_union_digest: str
    member_union_manifest: MemberUnionManifestReceipt

    @classmethod
    def from_dict(cls, raw: object) -> UniverseVerificationReceipt:
        data = _ensure_mapping(raw, label="universe_verification_receipt")
        _require_exact_keys(
            data,
            label="universe_verification_receipt",
            expected=(
                "profile_id",
                "profile_digest",
                "execution_policy",
                "max_members_per_date",
                "batches",
                "member_union_digest_algorithm",
                "member_union_count",
                "member_union_digest",
                "member_union_manifest",
            ),
        )
        batches_raw = data["batches"]
        if not isinstance(batches_raw, Sequence) or isinstance(batches_raw, str | bytes):
            raise AutoresearchValidationError("batches must be a list")
        receipt = cls(
            profile_id=_require_str(data, "profile_id"),
            profile_digest=_require_sha256(data, "profile_digest"),
            execution_policy=_require_str(data, "execution_policy"),
            max_members_per_date=_require_int(data, "max_members_per_date"),
            batches=tuple(UniverseHistoryBatchReceipt.from_dict(item) for item in batches_raw),
            member_union_digest_algorithm=_require_str(data, "member_union_digest_algorithm"),
            member_union_count=_require_int(data, "member_union_count"),
            member_union_digest=_require_sha256(data, "member_union_digest"),
            member_union_manifest=MemberUnionManifestReceipt.from_dict(
                data["member_union_manifest"]
            ),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_sha256(self.profile_digest, label="profile_digest")
        _validate_sha256(self.member_union_digest, label="member_union_digest")
        self.member_union_manifest.validate()
        if not self.batches:
            raise AutoresearchValidationError("universe verification requires history batches")
        for batch in self.batches:
            batch.validate()
        if self.execution_policy != NEXT_SESSION_EXECUTION_POLICY:
            raise AutoresearchValidationError(
                f"universe execution_policy must be {NEXT_SESSION_EXECUTION_POLICY}"
            )
        if self.member_union_count <= 0:
            raise AutoresearchValidationError("member_union_count must be positive")
        if self.member_union_digest_algorithm != MEMBER_UNION_DIGEST_ALGORITHM:
            raise AutoresearchValidationError(
                f"member_union_digest_algorithm must be {MEMBER_UNION_DIGEST_ALGORITHM}"
            )
        if not 1 <= self.max_members_per_date <= MAX_UNIVERSE_MEMBERS_PER_DATE:
            raise AutoresearchValidationError(
                "universe verification max_members_per_date must be between 1 and 1000"
            )
        selected_member_counts = tuple(
            item.selected_member_count for batch in self.batches for item in batch.dates
        )
        if self.member_union_count < max(selected_member_counts):
            raise AutoresearchValidationError(
                "member_union_count must be at least the largest selected_member_count"
            )
        if self.member_union_count > sum(selected_member_counts):
            raise AutoresearchValidationError(
                "member_union_count cannot exceed the sum of selected_member_count values"
            )

    def validate_against_plan(self, plan: UniversePlanArtifact) -> None:
        self.validate()
        plan.validate()
        for field_name in (
            "profile_id",
            "profile_digest",
            "execution_policy",
            "max_members_per_date",
        ):
            if getattr(self, field_name) != getattr(plan, field_name):
                raise AutoresearchValidationError(
                    f"universe verification {field_name} must match universe plan"
                )
        flattened = tuple(item.selection_date for batch in self.batches for item in batch.dates)
        if flattened != plan.selection_dates:
            raise AutoresearchValidationError(
                "universe verification batches must exactly cover plan dates "
                "without gaps or overlap"
            )
        max_batch_dates = min(
            MAX_UNIVERSE_BATCH_DATES,
            MAX_UNIVERSE_BATCH_RESULTS // plan.max_members_per_date,
        )
        if max_batch_dates < 1:
            raise AutoresearchValidationError("max_members_per_date cannot fit one history batch")
        expected_batches = tuple(
            plan.selection_dates[index : index + max_batch_dates]
            for index in range(0, len(plan.selection_dates), max_batch_dates)
        )
        actual_batches = tuple(
            tuple(item.selection_date for item in batch.dates) for batch in self.batches
        )
        if actual_batches != expected_batches:
            raise AutoresearchValidationError(
                "universe history batches must use deterministic contiguous canonical chunks"
            )
        if any(
            len(batch.dates) * plan.max_members_per_date > MAX_UNIVERSE_BATCH_RESULTS
            for batch in self.batches
        ):
            raise AutoresearchValidationError("universe history batch exceeds 10000 results")
        date_receipts = tuple(item for batch in self.batches for item in batch.dates)
        calendar_bindings = {
            (item.calendar_identity, item.calendar_digest) for item in date_receipts
        }
        if len(calendar_bindings) != 1:
            raise AutoresearchValidationError(
                "all execution dates must bind the same XNYS calendar receipt"
            )
        for item in date_receipts:
            if item.selected_member_count > plan.max_members_per_date:
                raise AutoresearchValidationError(
                    "selected_member_count exceeds plan max_members_per_date"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_digest": self.profile_digest,
            "execution_policy": self.execution_policy,
            "max_members_per_date": self.max_members_per_date,
            "batches": [receipt.to_dict() for receipt in self.batches],
            "member_union_digest_algorithm": self.member_union_digest_algorithm,
            "member_union_count": self.member_union_count,
            "member_union_digest": self.member_union_digest,
            "member_union_manifest": self.member_union_manifest.to_dict(),
        }


def _verify_member_union_manifest(receipt: UniverseVerificationReceipt) -> tuple[str, ...]:
    manifest = receipt.member_union_manifest
    descriptor: int | None = None
    try:
        descriptor = os.open(
            manifest.path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AutoresearchValidationError("member union manifest must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise AutoresearchValidationError(
            f"cannot read member union manifest: {manifest.path}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    content = b"".join(chunks)
    if hashlib.sha256(content).hexdigest() != manifest.sha256:
        raise AutoresearchValidationError("member union manifest SHA-256 mismatch")
    try:
        symbols = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise AutoresearchValidationError("member union manifest must be UTF-8") from exc
    canonical = canonical_member_union_manifest(symbols)
    if content != canonical:
        raise AutoresearchValidationError(
            "member union manifest must be uppercase sorted unique UTF-8 lines "
            "with one trailing newline"
        )
    count, digest = canonical_member_union_digest(symbols)
    if count != receipt.member_union_count or digest != receipt.member_union_digest:
        raise AutoresearchValidationError(
            "member union manifest must recompute the persisted count and digest"
        )
    return tuple(symbols)


def _validate_alpha_verification_price_preflight(state: AutoresearchState) -> None:
    if state.phase is not Phase.VERIFICATION or state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH verification requires implementation_result"
        )
    if state.implementation_result.price_hydration_scope_preflight is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH verification requires implementation_result."
            "price_hydration_scope_preflight before dispatch"
        )


def _validate_alpha_implementation_price_preflight(
    state: AutoresearchState,
    artifact: ImplementationResultArtifact,
) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    preflight = artifact.price_hydration_scope_preflight
    if preflight is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH implementation_result requires price_hydration_scope_preflight"
        )
    if preflight.within_budget:
        return
    hydrate_commands = tuple(
        command for command in artifact.commands_run if HYDRATE_CAPABLE_COMMAND_RE.search(command)
    )
    if hydrate_commands:
        raise AutoresearchValidationError(
            "over-budget ALPHA implementation_result must not include hydrate-capable "
            f"commands: {', '.join(hydrate_commands)}"
        )


def _latest_verification_is_price_scope_bug_signal(state: AutoresearchState) -> bool:
    latest = state.latest_verification
    return (
        latest is not None
        and latest.status is VerificationStatus.BUG_SIGNAL
        and any("price_hydration_scope_exceeds_budget" in signal for signal in latest.bug_signals)
    )


def _validate_price_scope_fix_result_commands(
    state: AutoresearchState,
    artifact: FixResultArtifact,
) -> None:
    if not _latest_verification_is_price_scope_bug_signal(state):
        return
    hydrate_commands = tuple(
        command for command in artifact.tests_rerun if HYDRATE_CAPABLE_COMMAND_RE.search(command)
    )
    if hydrate_commands:
        raise AutoresearchValidationError(
            "price-scope BUG_SIGNAL fix_result must not include hydrate-capable "
            f"commands: {', '.join(hydrate_commands)}"
        )


def _validate_alpha_price_preflight_matches_receipts(
    preflight: PriceHydrationScopePreflight,
    artifact: VerificationResultArtifact,
) -> None:
    if isinstance(artifact.data_coverage, DynamicUniverseCoverageReceipt):
        for field_name in (
            "member_union_count",
            "experiment_start",
            "experiment_end",
            "timeframe",
            "market_hours",
        ):
            if getattr(artifact.data_coverage, field_name) != getattr(preflight, field_name):
                raise AutoresearchValidationError(
                    f"dynamic coverage {field_name} must match price preflight"
                )
        if artifact.data_coverage.expected_symbol_sessions != preflight.planned_symbol_sessions:
            raise AutoresearchValidationError(
                "dynamic coverage expected_symbol_sessions must match price preflight"
            )
    if artifact.price_hydration_receipt is not None:
        for field_name in (
            "member_union_count",
            "experiment_start",
            "experiment_end",
            "timeframe",
            "market_hours",
        ):
            if getattr(artifact.price_hydration_receipt, field_name) != getattr(
                preflight, field_name
            ):
                raise AutoresearchValidationError(
                    f"price hydration {field_name} must match price preflight"
                )


def _validate_alpha_price_scope_verification(
    state: AutoresearchState,
    artifact: VerificationResultArtifact,
) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH verification requires implementation_result"
        )
    preflight = state.implementation_result.price_hydration_scope_preflight
    if preflight is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH verification requires implementation_result."
            "price_hydration_scope_preflight before artifact acceptance"
        )
    if preflight.within_budget:
        _validate_alpha_price_preflight_matches_receipts(preflight, artifact)
        if (
            artifact.status is VerificationStatus.PASS
            and isinstance(artifact.data_coverage, DynamicUniverseCoverageReceipt)
            and preflight.planned_symbol_sessions > MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS
        ):
            raise AutoresearchValidationError(
                "ALPHA_RESEARCH PASS dynamic coverage exceeds the alpha price "
                f"hydration budget of {MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS}"
            )
        return
    if artifact.status is not VerificationStatus.BUG_SIGNAL:
        raise AutoresearchValidationError(
            "over-budget ALPHA price hydration preflight requires BUG_SIGNAL verification"
        )
    if not any("price_hydration_scope_exceeds_budget" in signal for signal in artifact.bug_signals):
        raise AutoresearchValidationError(
            "over-budget ALPHA price hydration preflight requires "
            "price_hydration_scope_exceeds_budget bug signal"
        )
    if (
        artifact.data_coverage is not None
        or artifact.universe_verification_receipt is not None
        or artifact.price_hydration_receipt is not None
        or artifact.is_walk_forward_sharpe_net is not None
        or artifact.oos_sharpe_net is not None
        or artifact.max_drawdown_pct is not None
        or artifact.win_rate is not None
        or artifact.trade_count is not None
        or artifact.trades_per_day is not None
        or artifact.oos_trading_days is not None
    ):
        raise AutoresearchValidationError(
            "over-budget ALPHA price hydration BUG_SIGNAL must not include "
            "hydrate-dependent metrics, coverage, or receipts"
        )


def _require_g0_platform_provenance(
    state: AutoresearchState,
    artifact: VerificationResultArtifact,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    if state.mode is not ResearchMode.DATA_INFRA_G0:
        return
    is_contract_mismatch = (
        artifact.status is VerificationStatus.BUG_SIGNAL
        and artifact.bug_signals == (PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL,)
    )
    if is_contract_mismatch or artifact.status is not VerificationStatus.PASS:
        return
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform coverage requires implementation_result"
        )
    preflight = state.implementation_result.price_hydration_scope_preflight
    receipt = artifact.platform_coverage_validation
    universe = artifact.universe_verification_receipt
    hydration = artifact.price_hydration_receipt
    if preflight is None or receipt is None or universe is None or hydration is None:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform coverage requires runner-checkable preflight, "
            "universe, price hydration, and platform coverage provenance; use "
            "platform_coverage_contract_mismatch BUG_SIGNAL when unavailable or mismatched"
        )
    if validation_context is not None:
        validation_context.validate_universe_receipt(universe)
    requested_sessions = _requested_sessions_for_preflight(preflight, validation_context)
    if not _platform_receipt_has_expected_runner_provenance(
        receipt,
        preflight=preflight,
        universe=universe,
        hydration=hydration,
        requested_sessions=requested_sessions,
    ):
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform coverage receipt is not bound to the exact runner "
            "preflight, universe, and price hydration evidence; use "
            "platform_coverage_contract_mismatch BUG_SIGNAL"
        )


def _requested_sessions_for_preflight(
    preflight: PriceHydrationScopePreflight,
    validation_context: AutoresearchValidationContext | None,
) -> tuple[date, ...]:
    if validation_context is None:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform coverage requires a strict readiness validation context"
        )
    start = date.fromisoformat(preflight.experiment_start)
    end = date.fromisoformat(preflight.experiment_end)
    if not validation_context.xnys_sessions:
        raise AutoresearchValidationError("XNYS calendar evidence contains no sessions")
    evidence_start = validation_context.xnys_range_start or validation_context.xnys_sessions[0]
    evidence_end = validation_context.xnys_range_end or validation_context.xnys_sessions[-1]
    if start < evidence_start or end > evidence_end:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform preflight range extends outside pinned XNYS evidence"
        )
    session_labels = set(validation_context.xnys_sessions)
    if start not in session_labels or end not in session_labels:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform preflight start/end must be actual XNYS session labels "
            "in pinned evidence"
        )
    sessions = tuple(
        session for session in validation_context.xnys_sessions if start <= session <= end
    )
    if len(sessions) != preflight.session_count:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 platform preflight session_count must match pinned XNYS sessions"
        )
    return sessions


@dataclass(frozen=True, slots=True)
class PriceHydrationReceipt:
    member_union_count: int
    member_union_digest: str
    experiment_start: str
    experiment_end: str
    timeframe: str
    market_hours: str
    operation_count: int
    request_digest: str
    coverage_receipt_digest: str
    source_price_coverage_response_digest: str
    completed_at: str
    folds_started_at: str

    def request_identity(self) -> dict[str, object]:
        return {
            "member_union_count": self.member_union_count,
            "member_union_digest": self.member_union_digest,
            "experiment_start": self.experiment_start,
            "experiment_end": self.experiment_end,
            "timeframe": self.timeframe,
            "market_hours": self.market_hours,
        }

    def coverage_identity(self) -> dict[str, object]:
        return {
            "request_digest": self.request_digest,
            "operation_count": self.operation_count,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> PriceHydrationReceipt:
        data = _ensure_mapping(raw, label="price_hydration_receipt")
        _require_exact_keys(
            data,
            label="price_hydration_receipt",
            expected=(
                "member_union_count",
                "member_union_digest",
                "experiment_start",
                "experiment_end",
                "timeframe",
                "market_hours",
                "operation_count",
                "request_digest",
                "coverage_receipt_digest",
                "source_price_coverage_response_digest",
                "completed_at",
                "folds_started_at",
            ),
        )
        receipt = cls(
            member_union_count=_require_int(data, "member_union_count"),
            member_union_digest=_require_sha256(data, "member_union_digest"),
            experiment_start=_require_iso_date(data, "experiment_start"),
            experiment_end=_require_iso_date(data, "experiment_end"),
            timeframe=_require_str(data, "timeframe"),
            market_hours=_require_str(data, "market_hours"),
            operation_count=_require_int(data, "operation_count"),
            request_digest=_require_sha256(data, "request_digest"),
            coverage_receipt_digest=_require_sha256(data, "coverage_receipt_digest"),
            source_price_coverage_response_digest=_require_sha256(
                data, "source_price_coverage_response_digest"
            ),
            completed_at=_require_str(data, "completed_at"),
            folds_started_at=_require_str(data, "folds_started_at"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_sha256(self.member_union_digest, label="member_union_digest")
        _validate_iso_date_value(self.experiment_start, label="experiment_start")
        _validate_iso_date_value(self.experiment_end, label="experiment_end")
        if self.member_union_count <= 0:
            raise AutoresearchValidationError("member_union_count must be positive")
        if self.experiment_start > self.experiment_end:
            raise AutoresearchValidationError("price hydration experiment range is invalid")
        if self.operation_count != 1:
            raise AutoresearchValidationError("price hydration requires operation_count=1")
        if self.request_digest != price_hydration_request_digest(
            member_union_count=self.member_union_count,
            member_union_digest=self.member_union_digest,
            experiment_start=self.experiment_start,
            experiment_end=self.experiment_end,
            timeframe=self.timeframe,
            market_hours=self.market_hours,
        ):
            raise AutoresearchValidationError("price hydration request_digest is not canonical")
        if self.coverage_receipt_digest != price_hydration_coverage_digest(
            request_digest=self.request_digest,
            operation_count=self.operation_count,
            completed_at=self.completed_at,
        ):
            raise AutoresearchValidationError(
                "price hydration coverage_receipt_digest is not canonical"
            )
        _validate_sha256(
            self.source_price_coverage_response_digest,
            label="source_price_coverage_response_digest",
        )
        completed_at = _parse_timestamp(self.completed_at, label="completed_at")
        folds_started_at = _parse_timestamp(self.folds_started_at, label="folds_started_at")
        if completed_at >= folds_started_at:
            raise AutoresearchValidationError("price hydration must complete before folds start")

    def validate_against_universe(self, universe: UniverseVerificationReceipt) -> None:
        self.validate()
        universe.validate()
        if (
            self.member_union_count != universe.member_union_count
            or self.member_union_digest != universe.member_union_digest
        ):
            raise AutoresearchValidationError(
                "price hydration member union must match universe verification"
            )
        hydration_completed_at = _parse_timestamp(self.completed_at, label="completed_at")
        for date_receipt in (item for batch in universe.batches for item in batch.dates):
            for label, completed_at in (
                ("snapshot", date_receipt.snapshot.completed_at),
                ("summary", date_receipt.summary.completed_at),
            ):
                materialization_completed_at = _parse_timestamp(
                    completed_at,
                    label=f"{label} completed_at",
                )
                if materialization_completed_at > hydration_completed_at:
                    raise AutoresearchValidationError(
                        f"{label} materialization must complete before or at price hydration"
                    )

    def to_dict(self) -> dict[str, object]:
        return {
            "member_union_count": self.member_union_count,
            "member_union_digest": self.member_union_digest,
            "experiment_start": self.experiment_start,
            "experiment_end": self.experiment_end,
            "timeframe": self.timeframe,
            "market_hours": self.market_hours,
            "operation_count": self.operation_count,
            "request_digest": self.request_digest,
            "coverage_receipt_digest": self.coverage_receipt_digest,
            "source_price_coverage_response_digest": self.source_price_coverage_response_digest,
            "completed_at": self.completed_at,
            "folds_started_at": self.folds_started_at,
        }


@dataclass(frozen=True, slots=True)
class DynamicUniverseCoverageReceipt:
    member_union_count: int
    member_union_digest: str
    experiment_start: str
    experiment_end: str
    oos_start: str
    oos_end: str
    timeframe: str
    market_hours: str
    expected_symbol_sessions: int
    covered_symbol_sessions: int
    missing_symbol_count: int
    missing_symbol_sessions: int
    default_fold_count: int
    fallback_fold_count: int

    @classmethod
    def from_dict(cls, raw: object) -> DynamicUniverseCoverageReceipt:
        data = _ensure_mapping(raw, label="dynamic_universe_coverage_receipt")
        _require_exact_keys(
            data,
            label="dynamic_universe_coverage_receipt",
            expected=(
                "member_union_count",
                "member_union_digest",
                "experiment_start",
                "experiment_end",
                "oos_start",
                "oos_end",
                "timeframe",
                "market_hours",
                "expected_symbol_sessions",
                "covered_symbol_sessions",
                "missing_symbol_count",
                "missing_symbol_sessions",
                "default_fold_count",
                "fallback_fold_count",
            ),
        )
        receipt = cls(
            member_union_count=_require_int(data, "member_union_count"),
            member_union_digest=_require_sha256(data, "member_union_digest"),
            experiment_start=_require_iso_date(data, "experiment_start"),
            experiment_end=_require_iso_date(data, "experiment_end"),
            oos_start=_require_iso_date(data, "oos_start"),
            oos_end=_require_iso_date(data, "oos_end"),
            timeframe=_require_str(data, "timeframe"),
            market_hours=_require_str(data, "market_hours"),
            expected_symbol_sessions=_require_int(data, "expected_symbol_sessions"),
            covered_symbol_sessions=_require_int(data, "covered_symbol_sessions"),
            missing_symbol_count=_require_int(data, "missing_symbol_count"),
            missing_symbol_sessions=_require_int(data, "missing_symbol_sessions"),
            default_fold_count=_require_int(data, "default_fold_count"),
            fallback_fold_count=_require_int(data, "fallback_fold_count"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_sha256(self.member_union_digest, label="member_union_digest")
        _validate_iso_date_value(self.experiment_start, label="experiment_start")
        _validate_iso_date_value(self.experiment_end, label="experiment_end")
        if self.member_union_count <= 0 or self.experiment_start > self.experiment_end:
            raise AutoresearchValidationError("dynamic universe coverage identity is invalid")
        if not self.experiment_start <= self.oos_start <= self.oos_end <= self.experiment_end:
            raise AutoresearchValidationError(
                "dynamic universe OOS range must fit experiment range"
            )
        if not 0 <= self.covered_symbol_sessions <= self.expected_symbol_sessions:
            raise AutoresearchValidationError("dynamic universe symbol-session counts are invalid")
        if self.expected_symbol_sessions <= 0:
            raise AutoresearchValidationError("expected_symbol_sessions must be positive")
        if (
            min(
                self.missing_symbol_count,
                self.missing_symbol_sessions,
                self.default_fold_count,
                self.fallback_fold_count,
            )
            < 0
        ):
            raise AutoresearchValidationError("dynamic universe counts must be non-negative")

    def validate_against_hydration(
        self, hydration: PriceHydrationReceipt, *, require_complete: bool
    ) -> None:
        self.validate()
        hydration.validate()
        for field_name in (
            "member_union_count",
            "member_union_digest",
            "experiment_start",
            "experiment_end",
            "timeframe",
            "market_hours",
        ):
            if getattr(self, field_name) != getattr(hydration, field_name):
                raise AutoresearchValidationError(
                    f"dynamic coverage {field_name} must match price hydration"
                )
        if require_complete and (
            self.missing_symbol_count != 0
            or self.missing_symbol_sessions != 0
            or self.covered_symbol_sessions != self.expected_symbol_sessions
        ):
            raise AutoresearchValidationError(
                "PASS dynamic coverage requires zero missing symbols and symbol sessions"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "member_union_count": self.member_union_count,
            "member_union_digest": self.member_union_digest,
            "experiment_start": self.experiment_start,
            "experiment_end": self.experiment_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "timeframe": self.timeframe,
            "market_hours": self.market_hours,
            "expected_symbol_sessions": self.expected_symbol_sessions,
            "covered_symbol_sessions": self.covered_symbol_sessions,
            "missing_symbol_count": self.missing_symbol_count,
            "missing_symbol_sessions": self.missing_symbol_sessions,
            "default_fold_count": self.default_fold_count,
            "fallback_fold_count": self.fallback_fold_count,
        }


@dataclass(frozen=True, slots=True)
class CoverageReceipt:
    symbol: str
    declared_intended_start: str
    declared_intended_end: str
    actual_common_start: str
    actual_common_end: str
    oos_start: str
    oos_end: str
    expected_trading_days: int
    actual_trading_days: int
    coverage_percent: float
    missing_reason: str | None
    default_fold_count: int
    fallback_fold_count: int
    cap_provenance_available: bool
    fixed_sleeve_local_data: bool

    @classmethod
    def from_dict(cls, raw: object) -> CoverageReceipt:
        data = _ensure_mapping(raw, label="coverage_receipt")
        _require_exact_keys(
            data,
            label="coverage_receipt",
            expected=(
                "symbol",
                "declared_intended_start",
                "declared_intended_end",
                "actual_common_start",
                "actual_common_end",
                "oos_start",
                "oos_end",
                "expected_trading_days",
                "actual_trading_days",
                "coverage_percent",
                "missing_reason",
                "default_fold_count",
                "fallback_fold_count",
                "cap_provenance_available",
                "fixed_sleeve_local_data",
            ),
        )
        missing_reason = data.get("missing_reason")
        if missing_reason is not None and not isinstance(missing_reason, str):
            raise AutoresearchValidationError("missing_reason must be a string or null")
        receipt = cls(
            symbol=_require_str(data, "symbol"),
            declared_intended_start=_require_iso_date(data, "declared_intended_start"),
            declared_intended_end=_require_iso_date(data, "declared_intended_end"),
            actual_common_start=_require_iso_date(data, "actual_common_start"),
            actual_common_end=_require_iso_date(data, "actual_common_end"),
            oos_start=_require_iso_date(data, "oos_start"),
            oos_end=_require_iso_date(data, "oos_end"),
            expected_trading_days=_require_int(data, "expected_trading_days"),
            actual_trading_days=_require_int(data, "actual_trading_days"),
            coverage_percent=_require_float(data, "coverage_percent"),
            missing_reason=missing_reason.strip() if isinstance(missing_reason, str) else None,
            default_fold_count=_require_int(data, "default_fold_count"),
            fallback_fold_count=_require_int(data, "fallback_fold_count"),
            cap_provenance_available=_require_bool(data, "cap_provenance_available"),
            fixed_sleeve_local_data=_require_bool(data, "fixed_sleeve_local_data"),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        _validate_coverage_values(self, label=f"coverage receipt for {self.symbol}")

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "declared_intended_start": self.declared_intended_start,
            "declared_intended_end": self.declared_intended_end,
            "actual_common_start": self.actual_common_start,
            "actual_common_end": self.actual_common_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "expected_trading_days": self.expected_trading_days,
            "actual_trading_days": self.actual_trading_days,
            "coverage_percent": self.coverage_percent,
            "missing_reason": self.missing_reason,
            "default_fold_count": self.default_fold_count,
            "fallback_fold_count": self.fallback_fold_count,
            "cap_provenance_available": self.cap_provenance_available,
            "fixed_sleeve_local_data": self.fixed_sleeve_local_data,
        }


def _validate_coverage_values(receipt: CoverageReceipt, *, label: str) -> None:
    if not (
        receipt.declared_intended_start
        <= receipt.actual_common_start
        <= receipt.actual_common_end
        <= receipt.declared_intended_end
    ):
        raise AutoresearchValidationError(f"{label} actual common range must fit intended range")
    if not (
        receipt.actual_common_start
        <= receipt.oos_start
        <= receipt.oos_end
        <= receipt.actual_common_end
    ):
        raise AutoresearchValidationError(f"{label} OOS range must fit actual common range")
    if (
        receipt.expected_trading_days <= 0
        or not 0 <= receipt.actual_trading_days <= receipt.expected_trading_days
    ):
        raise AutoresearchValidationError(f"{label} trading day counts are invalid")
    if not 0.0 <= receipt.coverage_percent <= 100.0:
        raise AutoresearchValidationError(f"{label} coverage_percent must be between 0 and 100")
    expected_percent = receipt.actual_trading_days / receipt.expected_trading_days * 100.0
    if abs(receipt.coverage_percent - expected_percent) > 0.01:
        raise AutoresearchValidationError(f"{label} coverage_percent must match trading day counts")
    if receipt.actual_trading_days < receipt.expected_trading_days and not receipt.missing_reason:
        raise AutoresearchValidationError(
            f"{label} missing_reason is required for missing trading days"
        )
    if receipt.actual_trading_days == receipt.expected_trading_days and receipt.missing_reason:
        raise AutoresearchValidationError(
            f"{label} missing_reason is only valid for missing trading days"
        )
    if receipt.default_fold_count < 0 or receipt.fallback_fold_count < 0:
        raise AutoresearchValidationError(f"{label} fold counts must be non-negative")
    if receipt.fixed_sleeve_local_data and receipt.cap_provenance_available:
        raise AutoresearchValidationError(
            f"{label} fixed_sleeve_local_data cannot claim cap_provenance_available"
        )


@dataclass(frozen=True, slots=True)
class AggregateCoverageReceipt:
    declared_intended_start: str
    declared_intended_end: str
    actual_common_start: str
    actual_common_end: str
    oos_start: str
    oos_end: str
    expected_trading_days: int
    actual_trading_days: int
    coverage_percent: float
    missing_reason: str | None
    default_fold_count: int
    fallback_fold_count: int
    cap_provenance_available: bool
    fixed_sleeve_local_data: bool
    per_symbol: tuple[CoverageReceipt, ...]

    @classmethod
    def from_dict(cls, raw: object) -> AggregateCoverageReceipt:
        data = _ensure_mapping(raw, label="aggregate_coverage_receipt")
        _require_exact_keys(
            data,
            label="aggregate_coverage_receipt",
            expected=(
                "declared_intended_start",
                "declared_intended_end",
                "actual_common_start",
                "actual_common_end",
                "oos_start",
                "oos_end",
                "expected_trading_days",
                "actual_trading_days",
                "coverage_percent",
                "missing_reason",
                "default_fold_count",
                "fallback_fold_count",
                "cap_provenance_available",
                "fixed_sleeve_local_data",
                "per_symbol",
            ),
        )
        symbols_raw = data.get("per_symbol")
        if not isinstance(symbols_raw, Sequence) or isinstance(symbols_raw, str | bytes):
            raise AutoresearchValidationError("per_symbol must be a list")
        missing_reason = data.get("missing_reason")
        if missing_reason is not None and not isinstance(missing_reason, str):
            raise AutoresearchValidationError("missing_reason must be a string or null")
        receipt = cls(
            declared_intended_start=_require_iso_date(data, "declared_intended_start"),
            declared_intended_end=_require_iso_date(data, "declared_intended_end"),
            actual_common_start=_require_iso_date(data, "actual_common_start"),
            actual_common_end=_require_iso_date(data, "actual_common_end"),
            oos_start=_require_iso_date(data, "oos_start"),
            oos_end=_require_iso_date(data, "oos_end"),
            expected_trading_days=_require_int(data, "expected_trading_days"),
            actual_trading_days=_require_int(data, "actual_trading_days"),
            coverage_percent=_require_float(data, "coverage_percent"),
            missing_reason=missing_reason.strip() if isinstance(missing_reason, str) else None,
            default_fold_count=_require_int(data, "default_fold_count"),
            fallback_fold_count=_require_int(data, "fallback_fold_count"),
            cap_provenance_available=_require_bool(data, "cap_provenance_available"),
            fixed_sleeve_local_data=_require_bool(data, "fixed_sleeve_local_data"),
            per_symbol=tuple(CoverageReceipt.from_dict(item) for item in symbols_raw),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        if not self.per_symbol:
            raise AutoresearchValidationError(
                "aggregate coverage requires at least one per-symbol receipt"
            )
        if len(self.per_symbol) > MAX_FIXED_SLEEVE_SYMBOLS:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 coverage allows at most 32 per-symbol receipts"
            )
        synthetic = CoverageReceipt(
            symbol="aggregate",
            declared_intended_start=self.declared_intended_start,
            declared_intended_end=self.declared_intended_end,
            actual_common_start=self.actual_common_start,
            actual_common_end=self.actual_common_end,
            oos_start=self.oos_start,
            oos_end=self.oos_end,
            expected_trading_days=self.expected_trading_days,
            actual_trading_days=self.actual_trading_days,
            coverage_percent=self.coverage_percent,
            missing_reason=self.missing_reason,
            default_fold_count=self.default_fold_count,
            fallback_fold_count=self.fallback_fold_count,
            cap_provenance_available=self.cap_provenance_available,
            fixed_sleeve_local_data=self.fixed_sleeve_local_data,
        )
        _validate_coverage_values(synthetic, label="aggregate coverage")
        if len({receipt.symbol for receipt in self.per_symbol}) != len(self.per_symbol):
            raise AutoresearchValidationError("aggregate coverage cannot contain duplicate symbols")
        for receipt in self.per_symbol:
            receipt.validate()
            if (
                receipt.declared_intended_start != self.declared_intended_start
                or receipt.declared_intended_end != self.declared_intended_end
            ):
                raise AutoresearchValidationError(
                    "aggregate declared intended range must match every per-symbol receipt"
                )
            if receipt.fixed_sleeve_local_data != self.fixed_sleeve_local_data:
                raise AutoresearchValidationError(
                    "per-symbol fixed_sleeve_local_data must match aggregate"
                )
            if receipt.cap_provenance_available != self.cap_provenance_available:
                raise AutoresearchValidationError("per-symbol cap provenance must match aggregate")
        expected_common_start = max(receipt.actual_common_start for receipt in self.per_symbol)
        expected_common_end = min(receipt.actual_common_end for receipt in self.per_symbol)
        if self.actual_common_start != expected_common_start:
            raise AutoresearchValidationError(
                "aggregate actual_common_start must equal the latest per-symbol actual start"
            )
        if self.actual_common_end != expected_common_end:
            raise AutoresearchValidationError(
                "aggregate actual_common_end must equal the earliest per-symbol actual end"
            )
        expected_oos_start = max(receipt.oos_start for receipt in self.per_symbol)
        expected_oos_end = min(receipt.oos_end for receipt in self.per_symbol)
        if self.oos_start != expected_oos_start or self.oos_end != expected_oos_end:
            raise AutoresearchValidationError(
                "aggregate OOS range must equal the common per-symbol OOS intersection"
            )
        if any(
            receipt.expected_trading_days != self.expected_trading_days
            for receipt in self.per_symbol
        ):
            raise AutoresearchValidationError(
                "aggregate expected_trading_days must match every per-symbol common calendar"
            )
        if any(
            receipt.actual_trading_days != self.actual_trading_days for receipt in self.per_symbol
        ):
            raise AutoresearchValidationError(
                "aggregate actual_trading_days must match every per-symbol common calendar"
            )
        if any(receipt.coverage_percent != self.coverage_percent for receipt in self.per_symbol):
            raise AutoresearchValidationError(
                "aggregate coverage_percent must match every per-symbol common calendar"
            )
        if any(receipt.missing_reason != self.missing_reason for receipt in self.per_symbol):
            raise AutoresearchValidationError(
                "aggregate missing_reason must match every per-symbol common calendar"
            )
        expected_default_folds = min(receipt.default_fold_count for receipt in self.per_symbol)
        expected_fallback_folds = min(receipt.fallback_fold_count for receipt in self.per_symbol)
        if self.default_fold_count != expected_default_folds:
            raise AutoresearchValidationError(
                "aggregate default_fold_count must equal the fewest per-symbol default folds"
            )
        if self.fallback_fold_count != expected_fallback_folds:
            raise AutoresearchValidationError(
                "aggregate fallback_fold_count must equal the fewest per-symbol fallback folds"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "declared_intended_start": self.declared_intended_start,
            "declared_intended_end": self.declared_intended_end,
            "actual_common_start": self.actual_common_start,
            "actual_common_end": self.actual_common_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "expected_trading_days": self.expected_trading_days,
            "actual_trading_days": self.actual_trading_days,
            "coverage_percent": self.coverage_percent,
            "missing_reason": self.missing_reason,
            "default_fold_count": self.default_fold_count,
            "fallback_fold_count": self.fallback_fold_count,
            "cap_provenance_available": self.cap_provenance_available,
            "fixed_sleeve_local_data": self.fixed_sleeve_local_data,
            "per_symbol": [receipt.to_dict() for receipt in self.per_symbol],
        }


@dataclass(frozen=True, slots=True)
class VerificationResultArtifact:
    status: VerificationStatus
    is_walk_forward_sharpe_net: float | None
    oos_sharpe_net: float | None
    max_drawdown_pct: float | None
    win_rate: float | None
    trade_count: int | None
    trades_per_day: float | None
    oos_trading_days: int | None
    feature_importances_summary: str
    null_test_summary: str
    bug_signals: tuple[str, ...]
    tests_passed: bool
    commands_run: tuple[str, ...]
    data_coverage: DynamicUniverseCoverageReceipt | AggregateCoverageReceipt | None
    platform_coverage_validation: DynamicPriceCoverageReceipt | None = None
    infra_gate_outcome: InfraGateOutcome | None = None
    infra_rationale: str | None = None
    universe_verification_receipt: UniverseVerificationReceipt | None = None
    price_hydration_receipt: PriceHydrationReceipt | None = None
    quantipy_experiment_evidence: QuantipyExperimentEvidence | None = None
    quantipy_execution_not_started: QuantipyExecutionNotStartedEvidence | None = None

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        mode: ResearchMode | None = None,
    ) -> VerificationResultArtifact:
        data = _ensure_mapping(raw, label="verification_result")
        expected_fields: tuple[str, ...] = (
            "status",
            "is_walk_forward_sharpe_net",
            "oos_sharpe_net",
            "max_drawdown_pct",
            "win_rate",
            "trade_count",
            "trades_per_day",
            "oos_trading_days",
            "feature_importances_summary",
            "null_test_summary",
            "bug_signals",
            "tests_passed",
            "commands_run",
            "data_coverage",
            "platform_coverage_validation",
            "infra_gate_outcome",
            "infra_rationale",
            "universe_verification_receipt",
            "price_hydration_receipt",
            "quantipy_experiment_evidence",
            "quantipy_execution_not_started",
        )
        _require_exact_keys(
            data,
            label="verification_result",
            expected=expected_fields,
        )
        infra_gate_raw = data.get("infra_gate_outcome")
        infra_rationale = data.get("infra_rationale")
        if infra_gate_raw is not None and not isinstance(infra_gate_raw, str):
            raise AutoresearchValidationError("infra_gate_outcome must be a string or null")
        if infra_rationale is not None and not isinstance(infra_rationale, str):
            raise AutoresearchValidationError("infra_rationale must be a string or null")
        if "data_coverage" not in data:
            raise AutoresearchValidationError("data_coverage must be an object or null")
        data_coverage_raw = data["data_coverage"]
        data_coverage: DynamicUniverseCoverageReceipt | AggregateCoverageReceipt | None
        if data_coverage_raw is None:
            data_coverage = None
        else:
            coverage_data = _ensure_mapping(data_coverage_raw, label="data_coverage")
            if mode is ResearchMode.DATA_INFRA_G0:
                dynamic_keys = {
                    "member_union_count",
                    "member_union_digest",
                    "experiment_start",
                    "experiment_end",
                    "oos_start",
                    "oos_end",
                    "timeframe",
                    "market_hours",
                    "expected_symbol_sessions",
                    "covered_symbol_sessions",
                    "missing_symbol_count",
                    "missing_symbol_sessions",
                    "default_fold_count",
                    "fallback_fold_count",
                }
                data_coverage = (
                    DynamicUniverseCoverageReceipt.from_dict(coverage_data)
                    if set(coverage_data) == dynamic_keys
                    else AggregateCoverageReceipt.from_dict(coverage_data)
                )
            else:
                data_coverage = DynamicUniverseCoverageReceipt.from_dict(coverage_data)
        if "universe_verification_receipt" not in data:
            raise AutoresearchValidationError(
                "universe_verification_receipt must be an object or null"
            )
        if "price_hydration_receipt" not in data:
            raise AutoresearchValidationError("price_hydration_receipt must be an object or null")
        universe_receipt_raw = data["universe_verification_receipt"]
        hydration_receipt_raw = data["price_hydration_receipt"]
        platform_coverage_raw = data.get("platform_coverage_validation")
        quantipy_evidence_raw = data.get("quantipy_experiment_evidence")
        quantipy_not_started_raw = data.get("quantipy_execution_not_started")
        if platform_coverage_raw is not None and not isinstance(platform_coverage_raw, Mapping):
            raise AutoresearchValidationError(
                "platform_coverage_validation must be an object or null"
            )
        artifact = cls(
            status=VerificationStatus(_require_str(data, "status")),
            is_walk_forward_sharpe_net=_optional_float(data, "is_walk_forward_sharpe_net"),
            oos_sharpe_net=_optional_float(data, "oos_sharpe_net"),
            max_drawdown_pct=_optional_float(data, "max_drawdown_pct"),
            win_rate=_optional_float(data, "win_rate"),
            trade_count=_optional_int(data, "trade_count"),
            trades_per_day=_optional_float(data, "trades_per_day"),
            oos_trading_days=_optional_int(data, "oos_trading_days"),
            feature_importances_summary=_require_str(data, "feature_importances_summary"),
            null_test_summary=_require_str(data, "null_test_summary"),
            bug_signals=_require_string_list(data, "bug_signals"),
            tests_passed=_require_bool(data, "tests_passed"),
            commands_run=_require_string_list(data, "commands_run"),
            data_coverage=data_coverage,
            platform_coverage_validation=(
                DynamicPriceCoverageReceipt.from_dict(platform_coverage_raw)
                if platform_coverage_raw is not None
                else None
            ),
            infra_gate_outcome=InfraGateOutcome(infra_gate_raw)
            if infra_gate_raw is not None
            else None,
            infra_rationale=infra_rationale.strip() if isinstance(infra_rationale, str) else None,
            universe_verification_receipt=(
                UniverseVerificationReceipt.from_dict(universe_receipt_raw)
                if universe_receipt_raw is not None
                else None
            ),
            price_hydration_receipt=(
                PriceHydrationReceipt.from_dict(hydration_receipt_raw)
                if hydration_receipt_raw is not None
                else None
            ),
            quantipy_experiment_evidence=(
                QuantipyExperimentEvidence.from_dict(quantipy_evidence_raw)
                if quantipy_evidence_raw is not None
                else None
            ),
            quantipy_execution_not_started=(
                QuantipyExecutionNotStartedEvidence.from_dict(quantipy_not_started_raw)
                if quantipy_not_started_raw is not None
                else None
            ),
        )
        artifact.validate(
            mode=mode,
        )
        return artifact

    def validate(
        self,
        *,
        mode: ResearchMode | None = None,
        infra_gate_outcome: InfraGateOutcome | None = None,
    ) -> None:
        if self.status is VerificationStatus.PASS and (self.bug_signals or not self.tests_passed):
            raise AutoresearchValidationError(
                "PASS verification cannot include bug signals or failing tests"
            )
        if self.status is VerificationStatus.BUG_SIGNAL and not self.bug_signals:
            raise AutoresearchValidationError(
                "BUG_SIGNAL verification requires at least one bug signal"
            )
        if self.status is VerificationStatus.TEST_FAILURE and self.tests_passed:
            raise AutoresearchValidationError(
                "TEST_FAILURE verification cannot mark tests_passed=true"
            )
        evidence = self.quantipy_experiment_evidence
        not_started = self.quantipy_execution_not_started
        if evidence is not None and not_started is not None:
            raise AutoresearchValidationError(
                "verification cannot contain both Quantipy runtime and "
                "execution-not-started evidence"
            )
        if self.status is VerificationStatus.PASS and not_started is not None:
            raise AutoresearchValidationError(
                "PASS verification cannot contain execution-not-started evidence"
            )
        if evidence is not None:
            evidence.validate()
        if evidence is None:
            if self.status is VerificationStatus.BUG_SIGNAL and not self.bug_signals:
                raise AutoresearchValidationError(
                    "BUG_SIGNAL without a Quantipy run requires an explicit bug signal"
                )
            if (
                self.status is VerificationStatus.TEST_FAILURE
                and not self.null_test_summary.strip()
            ):
                raise AutoresearchValidationError(
                    "TEST_FAILURE without a Quantipy run requires an explicit rationale"
                )
        elif self.status is VerificationStatus.PASS and not evidence.success:
            raise AutoresearchValidationError(
                "PASS verification cannot claim a failed Quantipy experiment run"
            )
        elif self.status is VerificationStatus.TEST_FAILURE and evidence.success:
            raise AutoresearchValidationError(
                "TEST_FAILURE verification cannot claim a successful Quantipy experiment run"
            )
        elif (
            self.status is VerificationStatus.BUG_SIGNAL
            and evidence.success
            and not self.tests_passed
        ):
            raise AutoresearchValidationError(
                "BUG_SIGNAL successful Quantipy experiment requires tests_passed=true"
            )
        if (
            self.status is VerificationStatus.PASS
            and mode is not ResearchMode.DATA_INFRA_G0
            and (
                self.is_walk_forward_sharpe_net is None
                or self.oos_sharpe_net is None
                or self.max_drawdown_pct is None
                or self.win_rate is None
                or self.trade_count is None
                or self.trades_per_day is None
                or self.oos_trading_days is None
                or self.data_coverage is None
            )
        ):
            raise AutoresearchValidationError(
                "PASS verification requires complete metrics and data_coverage"
            )
        if self.data_coverage is not None:
            self.data_coverage.validate()
        if self.platform_coverage_validation is not None:
            self.platform_coverage_validation.validate()
        if (self.universe_verification_receipt is None) != (self.price_hydration_receipt is None):
            raise AutoresearchValidationError(
                "universe and price hydration receipts must both be present or both be null"
            )
        if (
            self.universe_verification_receipt is not None
            and self.price_hydration_receipt is not None
        ):
            self.price_hydration_receipt.validate_against_universe(
                self.universe_verification_receipt
            )
        if self.status is VerificationStatus.PASS and mode is ResearchMode.ALPHA_RESEARCH:
            if self.universe_verification_receipt is None or self.price_hydration_receipt is None:
                raise AutoresearchValidationError(
                    "ALPHA_RESEARCH PASS requires universe and price hydration receipts"
                )
            if isinstance(self.data_coverage, DynamicUniverseCoverageReceipt):
                self.data_coverage.validate_against_hydration(
                    self.price_hydration_receipt, require_complete=True
                )
            else:
                raise AutoresearchValidationError(
                    "ALPHA_RESEARCH PASS requires compact dynamic universe coverage"
                )
        outcome = infra_gate_outcome if infra_gate_outcome is not None else self.infra_gate_outcome
        if mode is ResearchMode.DATA_INFRA_G0:
            is_contract_mismatch = (
                self.status is VerificationStatus.BUG_SIGNAL
                and self.bug_signals == (PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL,)
            )
            if is_contract_mismatch:
                if (
                    outcome is not None
                    or self.infra_rationale is not None
                    or self.platform_coverage_validation is not None
                ):
                    raise AutoresearchValidationError(
                        "platform coverage contract mismatch must have null infrastructure "
                        "outcome, rationale, and receipt"
                    )
            else:
                receipt = self.platform_coverage_validation
                if self.status is VerificationStatus.PASS and (
                    receipt is None
                    or self.universe_verification_receipt is None
                    or self.price_hydration_receipt is None
                ):
                    raise AutoresearchValidationError(
                        "DATA_INFRA_G0 PASS requires paired universe, price hydration, "
                        "and platform coverage receipts; use "
                        "platform_coverage_contract_mismatch BUG_SIGNAL when unavailable "
                        "or mismatched"
                    )
                if receipt is None:
                    raise AutoresearchValidationError(
                        "DATA_INFRA_G0 verification requires platform_coverage_validation"
                    )
                elif not receipt.matches_shared_contract:
                    raise AutoresearchValidationError(
                        "Quantipy platform coverage scope or source contract mismatch requires "
                        "the canonical BUG_SIGNAL artifact"
                    )
                if outcome is None or not self.infra_rationale:
                    raise AutoresearchValidationError(
                        "DATA_INFRA_G0 verification requires infra_gate_outcome and infra_rationale"
                    )
                if self.status is VerificationStatus.PASS and receipt is not None:
                    expected_status = (
                        PlatformCoverageStatus.COMPLETE
                        if outcome is InfraGateOutcome.GATE_PASSED
                        else PlatformCoverageStatus.REMEDIATION_REQUIRED
                    )
                    if receipt.status is not expected_status:
                        raise AutoresearchValidationError(
                            "DATA_INFRA_G0 PASS gate outcome must match platform coverage "
                            "receipt status"
                        )
        if mode is ResearchMode.ALPHA_RESEARCH and (outcome is not None or self.infra_rationale):
            raise AutoresearchValidationError(
                "ALPHA_RESEARCH verification cannot contain infrastructure gate outcomes"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "is_walk_forward_sharpe_net": self.is_walk_forward_sharpe_net,
            "oos_sharpe_net": self.oos_sharpe_net,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
            "trade_count": self.trade_count,
            "trades_per_day": self.trades_per_day,
            "oos_trading_days": self.oos_trading_days,
            "feature_importances_summary": self.feature_importances_summary,
            "null_test_summary": self.null_test_summary,
            "bug_signals": list(self.bug_signals),
            "tests_passed": self.tests_passed,
            "commands_run": list(self.commands_run),
            "data_coverage": self.data_coverage.to_dict()
            if self.data_coverage is not None
            else None,
            "platform_coverage_validation": self.platform_coverage_validation.to_dict()
            if self.platform_coverage_validation is not None
            else None,
            "infra_gate_outcome": self.infra_gate_outcome.value
            if self.infra_gate_outcome is not None
            else None,
            "infra_rationale": self.infra_rationale,
            "universe_verification_receipt": self.universe_verification_receipt.to_dict()
            if self.universe_verification_receipt is not None
            else None,
            "price_hydration_receipt": self.price_hydration_receipt.to_dict()
            if self.price_hydration_receipt is not None
            else None,
            "quantipy_experiment_evidence": self.quantipy_experiment_evidence.to_dict()
            if self.quantipy_experiment_evidence is not None
            else None,
            "quantipy_execution_not_started": self.quantipy_execution_not_started.to_dict()
            if self.quantipy_execution_not_started is not None
            else None,
        }


@dataclass(frozen=True, slots=True)
class ReviewResultArtifact:
    reviewer_agent_id: str
    verdict: ReviewVerdict
    recommended_metric_name: str
    recommended_metric_value: float
    critical_issues: tuple[str, ...]
    noncritical_issues: tuple[str, ...]
    fix_requests: tuple[str, ...]
    summary: str

    @classmethod
    def from_dict(cls, raw: object) -> ReviewResultArtifact:
        data = _ensure_mapping(raw, label="review_result")
        _require_exact_keys(
            data,
            label="review_result",
            expected=(
                "reviewer_agent_id",
                "verdict",
                "recommended_metric_name",
                "recommended_metric_value",
                "critical_issues",
                "noncritical_issues",
                "fix_requests",
                "summary",
            ),
        )
        artifact = cls(
            reviewer_agent_id=_require_str(data, "reviewer_agent_id"),
            verdict=ReviewVerdict(_require_str(data, "verdict")),
            recommended_metric_name=_require_str(data, "recommended_metric_name"),
            recommended_metric_value=_require_float(data, "recommended_metric_value"),
            critical_issues=_require_string_list(data, "critical_issues"),
            noncritical_issues=_require_string_list(data, "noncritical_issues"),
            fix_requests=_require_string_list(data, "fix_requests"),
            summary=_require_str(data, "summary"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        needs_fix = self.verdict is ReviewVerdict.FAIL or bool(self.critical_issues)
        if needs_fix and not self.fix_requests:
            raise AutoresearchValidationError(
                "review_result with critical issues or FAIL verdict requires fix_requests"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "reviewer_agent_id": self.reviewer_agent_id,
            "verdict": self.verdict.value,
            "recommended_metric_name": self.recommended_metric_name,
            "recommended_metric_value": self.recommended_metric_value,
            "critical_issues": list(self.critical_issues),
            "noncritical_issues": list(self.noncritical_issues),
            "fix_requests": list(self.fix_requests),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class FixResultArtifact:
    trigger_phase: FixTriggerPhase
    summary: str
    workspace_path: str
    commit_sha: str
    fixes_applied: tuple[str, ...]
    tests_rerun: tuple[str, ...]
    remaining_issues: tuple[str, ...]
    price_hydration_scope_preflight: PriceHydrationScopePreflight | None = None

    @classmethod
    def from_dict(cls, raw: object) -> FixResultArtifact:
        data = _ensure_mapping(raw, label="fix_result")
        _require_exact_keys(
            data,
            label="fix_result",
            expected=(
                "trigger_phase",
                "summary",
                "workspace_path",
                "commit_sha",
                "fixes_applied",
                "tests_rerun",
                "remaining_issues",
                "price_hydration_scope_preflight",
            ),
        )
        preflight_raw = data.get("price_hydration_scope_preflight")
        artifact = cls(
            trigger_phase=FixTriggerPhase(_require_str(data, "trigger_phase")),
            summary=_require_str(data, "summary"),
            workspace_path=_require_workspace_path(data, "workspace_path"),
            commit_sha=_require_str(data, "commit_sha"),
            fixes_applied=_require_string_list(data, "fixes_applied"),
            tests_rerun=_require_string_list(data, "tests_rerun"),
            remaining_issues=_require_string_list(data, "remaining_issues"),
            price_hydration_scope_preflight=(
                PriceHydrationScopePreflight.from_dict(preflight_raw)
                if preflight_raw is not None
                else None
            ),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        _validate_workspace_path(self.workspace_path, label="fix_result workspace_path")
        if not re.fullmatch(r"[0-9a-f]{7,40}", self.commit_sha):
            raise AutoresearchValidationError("fix_result commit_sha must be a Git commit SHA")

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger_phase": self.trigger_phase.value,
            "summary": self.summary,
            "workspace_path": self.workspace_path,
            "commit_sha": self.commit_sha,
            "fixes_applied": list(self.fixes_applied),
            "tests_rerun": list(self.tests_rerun),
            "remaining_issues": list(self.remaining_issues),
            "price_hydration_scope_preflight": (
                self.price_hydration_scope_preflight.to_dict()
                if self.price_hydration_scope_preflight is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class FinalDecisionArtifact:
    experiment_id: str
    decision: FinalDecision
    recommended_metric_name: str
    recommended_metric_value: float | None
    reviewer_verdict: FinalReviewerVerdict
    rationale: str
    log_summary: str
    continue_loop: bool
    memory_write_required: bool
    infra_rationale: str | None = None

    @classmethod
    def from_dict(cls, raw: object) -> FinalDecisionArtifact:
        data = _ensure_mapping(raw, label="final_decision")
        _require_exact_keys(
            data,
            label="final_decision",
            expected=(
                "experiment_id",
                "decision",
                "recommended_metric_name",
                "recommended_metric_value",
                "reviewer_verdict",
                "rationale",
                "log_summary",
                "continue_loop",
                "memory_write_required",
                "infra_rationale",
            ),
        )
        metric_value = data.get("recommended_metric_value")
        if metric_value is not None and (
            isinstance(metric_value, bool) or not isinstance(metric_value, int | float)
        ):
            raise AutoresearchValidationError("recommended_metric_value must be numeric or null")
        infra_rationale = data.get("infra_rationale")
        if infra_rationale is not None and not isinstance(infra_rationale, str):
            raise AutoresearchValidationError("infra_rationale must be a string or null")
        return cls(
            experiment_id=_require_canonical_identifier(data, "experiment_id"),
            decision=FinalDecision(_require_str(data, "decision")),
            recommended_metric_name=_require_str(data, "recommended_metric_name"),
            recommended_metric_value=float(metric_value) if metric_value is not None else None,
            reviewer_verdict=FinalReviewerVerdict(_require_str(data, "reviewer_verdict")),
            rationale=_require_str(data, "rationale"),
            log_summary=_require_str(data, "log_summary"),
            continue_loop=_require_bool(data, "continue_loop"),
            memory_write_required=_require_bool(data, "memory_write_required"),
            infra_rationale=infra_rationale.strip() if isinstance(infra_rationale, str) else None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "decision": self.decision.value,
            "recommended_metric_name": self.recommended_metric_name,
            "recommended_metric_value": self.recommended_metric_value,
            "reviewer_verdict": self.reviewer_verdict.value,
            "rationale": self.rationale,
            "log_summary": self.log_summary,
            "continue_loop": self.continue_loop,
            "memory_write_required": self.memory_write_required,
            "infra_rationale": self.infra_rationale,
        }


@dataclass(frozen=True, slots=True)
class MemoryVerificationReceipt:
    experiment_id: str
    kg_path: str
    predicates: tuple[str, ...]
    verified_rows_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "kg_path": self.kg_path,
            "predicates": list(self.predicates),
            "verified_rows_digest": self.verified_rows_digest,
        }

    @classmethod
    def from_dict(cls, raw: object) -> MemoryVerificationReceipt:
        data = _ensure_mapping(raw, label="memory_verification_receipt")
        _require_exact_keys(
            data,
            label="memory_verification_receipt",
            expected=("experiment_id", "kg_path", "predicates", "verified_rows_digest"),
        )
        digest = _require_str(data, "verified_rows_digest")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AutoresearchValidationError("verified_rows_digest must be a SHA-256 hex digest")
        return cls(
            experiment_id=_require_canonical_identifier(data, "experiment_id"),
            kg_path=_require_str(data, "kg_path"),
            predicates=tuple(
                sorted(
                    _normalise_predicate(item) for item in _require_string_list(data, "predicates")
                )
            ),
            verified_rows_digest=digest,
        )


@dataclass(frozen=True, slots=True)
class NextAction:
    phase: Phase
    next_agent_ids: tuple[str, ...]
    expected_artifact_type: ArtifactType
    required_receipts: tuple[SourceReceipt, ...]
    instruction_source_manifest: InstructionSourceManifest
    source_manifest_sha256: str
    state_reference_sha256: str
    prompt_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "next_agent_ids": list(self.next_agent_ids),
            "expected_artifact_type": self.expected_artifact_type.value,
            "required_receipts": [receipt.to_dict() for receipt in self.required_receipts],
            "instruction_source_manifest": self.instruction_source_manifest.to_dict(),
            "source_manifest_sha256": self.source_manifest_sha256,
            "state_reference_sha256": self.state_reference_sha256,
            "prompt_text": self.prompt_text,
        }


@dataclass(frozen=True, slots=True)
class PhaseTarget:
    agent_ids: tuple[str, ...]
    artifact_type: ArtifactType


@dataclass(frozen=True, slots=True)
class InterruptedVerificationAttemptReceipt:
    """Immutable proof that one exact detached verification attempt was stopped."""

    expected_run_id: str
    interrupted_attempt: int
    implementation_commit: str
    implementation_manifest_sha256: str
    detached_run_directory: str
    detached_run_manifest_sha256: str
    detached_run_status_sha256: str
    state_sha256: str
    state_reference_sha256: str
    instruction_manifest_sha256: str
    prior_retry_receipt_sha256: str
    prior_retry_receipt: ExternalVerificationRetryReceipt
    verification_history_sha256: tuple[str, ...]
    operator_reason: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise AutoresearchValidationError(
                "unsupported interrupted verification attempt receipt schema_version"
            )
        if self.interrupted_attempt != 3:
            raise AutoresearchValidationError(
                "interrupted verification recovery accepts only the current v3 attempt"
            )
        if (
            re.fullmatch(
                rf"autoresearch-i[1-9][0-9]*-{self.implementation_commit[:12]}-v{self.interrupted_attempt}",
                self.expected_run_id,
            )
            is None
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt expected_run_id is invalid"
            )
        if (
            not isinstance(self.detached_run_directory, str)
            or not Path(self.detached_run_directory).is_absolute()
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt detached_run_directory must be absolute"
            )
        if re.fullmatch(r"[0-9a-f]{7,64}", self.implementation_commit) is None:
            raise AutoresearchValidationError(
                "interrupted verification receipt implementation_commit is invalid"
            )
        for label, digest in (
            ("implementation_manifest_sha256", self.implementation_manifest_sha256),
            ("detached_run_manifest_sha256", self.detached_run_manifest_sha256),
            ("detached_run_status_sha256", self.detached_run_status_sha256),
            ("state_sha256", self.state_sha256),
            ("state_reference_sha256", self.state_reference_sha256),
            ("instruction_manifest_sha256", self.instruction_manifest_sha256),
            ("prior_retry_receipt_sha256", self.prior_retry_receipt_sha256),
        ):
            _validate_sha256(digest, label=f"interrupted_verification_attempt_receipt.{label}")
        if not isinstance(self.prior_retry_receipt, ExternalVerificationRetryReceipt):
            raise AutoresearchValidationError(
                "interrupted verification receipt requires the immutable prior retry receipt"
            )
        if (
            self.prior_retry_receipt.schema_version
            != EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt requires a schema-v2 prior retry receipt"
            )
        if (
            self.prior_retry_receipt.retry_attempt != self.interrupted_attempt
            or self.prior_retry_receipt.expected_run_id != self.expected_run_id
            or self.prior_retry_receipt.implementation_commit != self.implementation_commit
            or self.prior_retry_receipt.manifest_sha256 != self.implementation_manifest_sha256
            or self.prior_retry_receipt_sha256
            != _canonical_json_digest(self.prior_retry_receipt.to_dict())
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt prior retry receipt binding is invalid"
            )
        if (
            not isinstance(self.verification_history_sha256, tuple)
            or len(self.verification_history_sha256) != 2
        ):
            raise AutoresearchValidationError(
                "interrupted verification receipt requires the ordered v1/v2 history"
            )
        for index, digest in enumerate(self.verification_history_sha256, start=1):
            _validate_sha256(
                digest,
                label=(
                    f"interrupted_verification_attempt_receipt.verification_history_sha256[{index}]"
                ),
            )
        if not self.operator_reason or self.operator_reason.strip() != self.operator_reason:
            raise AutoresearchValidationError(
                "interrupted verification receipt requires a trimmed operator reason"
            )

    @classmethod
    def from_dict(cls, raw: object) -> InterruptedVerificationAttemptReceipt:
        data = _ensure_mapping(raw, label="interrupted_verification_attempt_receipt")
        _require_exact_keys(
            data,
            label="interrupted_verification_attempt_receipt",
            expected=(
                "expected_run_id",
                "interrupted_attempt",
                "implementation_commit",
                "implementation_manifest_sha256",
                "detached_run_directory",
                "detached_run_manifest_sha256",
                "detached_run_status_sha256",
                "state_sha256",
                "state_reference_sha256",
                "instruction_manifest_sha256",
                "prior_retry_receipt_sha256",
                "prior_retry_receipt",
                "verification_history_sha256",
                "operator_reason",
                "schema_version",
            ),
        )
        history = data["verification_history_sha256"]
        if not isinstance(history, list):
            raise AutoresearchValidationError(
                "interrupted verification receipt verification_history_sha256 must be a list"
            )
        return cls(
            expected_run_id=_require_str(data, "expected_run_id"),
            interrupted_attempt=_require_int(data, "interrupted_attempt"),
            implementation_commit=_require_str(data, "implementation_commit"),
            implementation_manifest_sha256=_require_sha256(data, "implementation_manifest_sha256"),
            detached_run_directory=_require_str(data, "detached_run_directory"),
            detached_run_manifest_sha256=_require_sha256(data, "detached_run_manifest_sha256"),
            detached_run_status_sha256=_require_sha256(data, "detached_run_status_sha256"),
            state_sha256=_require_sha256(data, "state_sha256"),
            state_reference_sha256=_require_sha256(data, "state_reference_sha256"),
            instruction_manifest_sha256=_require_sha256(data, "instruction_manifest_sha256"),
            prior_retry_receipt_sha256=_require_sha256(data, "prior_retry_receipt_sha256"),
            prior_retry_receipt=ExternalVerificationRetryReceipt.from_dict(
                data["prior_retry_receipt"]
            ),
            verification_history_sha256=tuple(
                _require_sha256({"value": digest}, "value") for digest in history
            ),
            operator_reason=_require_str(data, "operator_reason"),
            schema_version=_require_int(data, "schema_version"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_run_id": self.expected_run_id,
            "interrupted_attempt": self.interrupted_attempt,
            "implementation_commit": self.implementation_commit,
            "implementation_manifest_sha256": self.implementation_manifest_sha256,
            "detached_run_directory": self.detached_run_directory,
            "detached_run_manifest_sha256": self.detached_run_manifest_sha256,
            "detached_run_status_sha256": self.detached_run_status_sha256,
            "state_sha256": self.state_sha256,
            "state_reference_sha256": self.state_reference_sha256,
            "instruction_manifest_sha256": self.instruction_manifest_sha256,
            "prior_retry_receipt_sha256": self.prior_retry_receipt_sha256,
            "prior_retry_receipt": self.prior_retry_receipt.to_dict(),
            "verification_history_sha256": list(self.verification_history_sha256),
            "operator_reason": self.operator_reason,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExternalVerificationRetryReceipt:
    """One operator-authorized retry of an externally failed verification run."""

    expected_run_id: str
    prior_verification_sha256: str
    probe: ResearchPanelProbeReceipt
    retry_attempt: int
    implementation_commit: str
    manifest_sha256: str
    readiness_manifest_id: str
    readiness_snapshot_id: str
    operator_reason: str
    verification_history_sha256: tuple[str, ...] = field(default_factory=tuple)
    interruption_history_sha256: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in {
            LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        }:
            raise AutoresearchValidationError(
                "unsupported external verification retry receipt schema_version"
            )
        if self.retry_attempt not in {2, 3, 4, 5}:
            raise AutoresearchValidationError(
                "external verification retry receipt attempt is not supported"
            )
        _validate_sha256(
            self.prior_verification_sha256,
            label="external_verification_retry_receipt.prior_verification_sha256",
        )
        if not isinstance(self.verification_history_sha256, tuple):
            raise AutoresearchValidationError(
                "external verification retry receipt verification_history_sha256 must be a tuple"
            )
        if self.schema_version == LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
            if (
                self.retry_attempt != 2
                or self.verification_history_sha256
                or self.interruption_history_sha256
            ):
                raise AutoresearchValidationError(
                    "legacy external verification retry receipt only accepts the live v2 bootstrap"
                )
        elif self.schema_version == EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
            if self.retry_attempt not in {2, 3}:
                raise AutoresearchValidationError(
                    "schema-v2 external verification retry receipt only accepts v2 or v3"
                )
            if len(self.verification_history_sha256) != self.retry_attempt - 1:
                raise AutoresearchValidationError(
                    "external verification retry receipt must bind every prior verification "
                    "artifact"
                )
            if self.interruption_history_sha256:
                raise AutoresearchValidationError(
                    "schema-v2 external verification retry receipt cannot bind interruptions"
                )
        else:
            if self.retry_attempt not in {4, 5}:
                raise AutoresearchValidationError(
                    "interrupted external verification retry receipt only accepts v4 or v5"
                )
            if not isinstance(self.interruption_history_sha256, tuple):
                raise AutoresearchValidationError(
                    "interrupted external verification retry receipt interruption "
                    "history must be a tuple"
                )
            if (
                len(self.verification_history_sha256) + len(self.interruption_history_sha256)
                != self.retry_attempt - 1
            ):
                raise AutoresearchValidationError(
                    "interrupted external verification retry receipt must bind every prior attempt"
                )
            for history_name, history in (
                ("verification_history_sha256", self.verification_history_sha256),
                ("interruption_history_sha256", self.interruption_history_sha256),
            ):
                for index, digest in enumerate(history, start=1):
                    _validate_sha256(
                        digest,
                        label=f"external_verification_retry_receipt.{history_name}[{index}]",
                    )
            for index, digest in enumerate(self.verification_history_sha256, start=1):
                _validate_sha256(
                    digest,
                    label=(
                        f"external_verification_retry_receipt.verification_history_sha256[{index}]"
                    ),
                )
        if re.fullmatch(r"[0-9a-f]{7,64}", self.implementation_commit) is None:
            raise AutoresearchValidationError("implementation_commit is invalid")
        _validate_sha256(self.manifest_sha256, label="manifest_sha256")
        if not self.readiness_manifest_id or not self.readiness_snapshot_id:
            raise AutoresearchValidationError(
                "external verification retry receipt requires readiness identities"
            )
        if not self.operator_reason or self.operator_reason.strip() != self.operator_reason:
            raise AutoresearchValidationError(
                "external verification retry receipt requires a trimmed operator reason"
            )
        if (
            re.fullmatch(
                rf"autoresearch-i[1-9][0-9]*-[0-9a-f]{{7,12}}-v{self.retry_attempt}",
                self.expected_run_id,
            )
            is None
        ):
            raise AutoresearchValidationError(
                "external verification retry receipt expected_run_id is invalid"
            )
        if not isinstance(self.probe, ResearchPanelProbeReceipt):
            raise AutoresearchValidationError(
                "external verification retry receipt requires a research-panel probe"
            )

    @classmethod
    def for_state(
        cls,
        state: AutoresearchState,
        probe: ResearchPanelProbeReceipt,
        operator_reason: str,
    ) -> ExternalVerificationRetryReceipt:
        attempt = _validate_external_verification_retry_eligibility(state)
        assert state.implementation_result is not None
        assert state.latest_verification is not None
        assert state.platform_readiness is not None
        commit_sha = state.implementation_result.commit_sha
        return cls(
            expected_run_id=_deterministic_quantipy_run_id(
                state.iteration,
                commit_sha,
                attempt=attempt,
            ),
            prior_verification_sha256=_canonical_json_digest(state.latest_verification.to_dict()),
            probe=probe,
            retry_attempt=attempt,
            implementation_commit=commit_sha,
            manifest_sha256=state.implementation_result.experiment_manifest_sha256,
            readiness_manifest_id=state.platform_readiness.manifest_id,
            readiness_snapshot_id=state.platform_readiness.snapshot_id,
            operator_reason=operator_reason,
            verification_history_sha256=tuple(
                _canonical_json_digest(artifact.to_dict())
                for artifact in state.verification_history
            ),
            interruption_history_sha256=tuple(
                _canonical_json_digest(interruption.to_dict())
                for interruption in state.interrupted_verification_history
            ),
            schema_version=(
                INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
                if state.interrupted_verification_history
                else EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
            ),
        )

    @classmethod
    def from_dict(cls, raw: object) -> ExternalVerificationRetryReceipt:
        data = _ensure_mapping(raw, label="external_verification_retry_receipt")
        schema_version = _require_int(data, "schema_version")
        expected: tuple[str, ...] = (
            "expected_run_id",
            "prior_verification_sha256",
            "probe",
            "retry_attempt",
            "implementation_commit",
            "manifest_sha256",
            "readiness_manifest_id",
            "readiness_snapshot_id",
            "operator_reason",
            "schema_version",
        )
        if schema_version in {
            EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        }:
            expected = (*expected, "verification_history_sha256")
        if schema_version == INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
            expected = (*expected, "interruption_history_sha256")
        if schema_version not in {
            LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        }:
            raise AutoresearchValidationError(
                "unsupported external verification retry receipt schema_version"
            )
        _require_exact_keys(
            data,
            label="external_verification_retry_receipt",
            expected=expected,
        )
        history_raw = data.get("verification_history_sha256", [])
        if not isinstance(history_raw, list):
            raise AutoresearchValidationError(
                "external verification retry receipt verification_history_sha256 must be a list"
            )
        history: list[str] = []
        for index, digest in enumerate(history_raw):
            if not isinstance(digest, str):
                raise AutoresearchValidationError(
                    "external verification retry receipt verification_history_sha256 "
                    f"entry {index} must be a SHA-256"
                )
            _validate_sha256(
                digest,
                label=(f"external_verification_retry_receipt.verification_history_sha256[{index}]"),
            )
            history.append(digest)
        interruptions_raw = data.get("interruption_history_sha256", [])
        if not isinstance(interruptions_raw, list):
            raise AutoresearchValidationError(
                "external verification retry receipt interruption_history_sha256 must be a list"
            )
        interruptions = tuple(
            _require_sha256({"value": digest}, "value") for digest in interruptions_raw
        )
        return cls(
            expected_run_id=_require_str(data, "expected_run_id"),
            prior_verification_sha256=_require_sha256(data, "prior_verification_sha256"),
            probe=ResearchPanelProbeReceipt.from_dict(data["probe"]),
            retry_attempt=_require_int(data, "retry_attempt"),
            implementation_commit=_require_str(data, "implementation_commit"),
            manifest_sha256=_require_sha256(data, "manifest_sha256"),
            readiness_manifest_id=_require_str(data, "readiness_manifest_id"),
            readiness_snapshot_id=_require_str(data, "readiness_snapshot_id"),
            operator_reason=_require_str(data, "operator_reason"),
            verification_history_sha256=tuple(history),
            interruption_history_sha256=interruptions,
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, object]:
        receipt = {
            "expected_run_id": self.expected_run_id,
            "prior_verification_sha256": self.prior_verification_sha256,
            "probe": self.probe.to_dict(),
            "retry_attempt": self.retry_attempt,
            "implementation_commit": self.implementation_commit,
            "manifest_sha256": self.manifest_sha256,
            "readiness_manifest_id": self.readiness_manifest_id,
            "readiness_snapshot_id": self.readiness_snapshot_id,
            "operator_reason": self.operator_reason,
            "schema_version": self.schema_version,
        }
        if self.schema_version in {
            EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
            INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        }:
            receipt["verification_history_sha256"] = list(self.verification_history_sha256)
        if self.schema_version == INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
            receipt["interruption_history_sha256"] = list(self.interruption_history_sha256)
        return receipt


@dataclass(frozen=True, slots=True)
class CanonicalQuantipyRuntimeAttestation:
    """Pinned proof that a canonical run resolves Quantipy from the canonical runtime."""

    root: str
    commit_sha: str
    readiness_quantipy_commit: str
    pyproject_sha256: str
    uv_lock_sha256: str
    venv_prefix: str
    executable_path: str
    executable_sha256: str
    executable_size_bytes: int
    executable_mode: int
    executable_owner_uid: int
    import_path: str
    base_interpreter_path: str
    base_interpreter_version: str
    base_interpreter_sha256: str
    base_interpreter_size_bytes: int
    base_interpreter_mode: int
    base_interpreter_owner_uid: int
    schema_version: int = 3

    def __post_init__(self) -> None:
        if self.schema_version != 3:
            raise AutoresearchValidationError(
                "unsupported canonical Quantipy runtime schema_version"
            )
        root = _require_canonical_absolute_path(self.root, label="canonical Quantipy runtime root")
        if str(root) != self.root:
            raise AutoresearchValidationError("canonical Quantipy runtime root is invalid")
        for label, value in (
            ("commit_sha", self.commit_sha),
            ("readiness_quantipy_commit", self.readiness_quantipy_commit),
        ):
            if re.fullmatch(r"[0-9a-f]{7,64}", value) is None:
                raise AutoresearchValidationError(f"canonical Quantipy runtime {label} is invalid")
        for label, value in (
            ("pyproject_sha256", self.pyproject_sha256),
            ("uv_lock_sha256", self.uv_lock_sha256),
            ("executable_sha256", self.executable_sha256),
            ("base_interpreter_sha256", self.base_interpreter_sha256),
        ):
            _validate_sha256(value, label=f"canonical_quantipy_runtime.{label}")
        for label, value in (
            ("venv_prefix", self.venv_prefix),
            ("executable_path", self.executable_path),
            ("import_path", self.import_path),
            ("base_interpreter_path", self.base_interpreter_path),
        ):
            path = _require_canonical_absolute_path(
                value, label=f"canonical Quantipy runtime {label}"
            )
            if str(path) != value:
                raise AutoresearchValidationError(f"canonical Quantipy runtime {label} is invalid")
        root_path = Path(self.root)
        venv = root_path / ".venv"
        if Path(self.venv_prefix) != venv:
            raise AutoresearchValidationError("canonical Quantipy runtime venv prefix is invalid")
        if Path(self.executable_path) != venv / "bin" / "quantipy":
            raise AutoresearchValidationError(
                "canonical Quantipy runtime executable is not the .venv quantipy entrypoint"
            )
        if (
            not isinstance(self.executable_size_bytes, int)
            or isinstance(self.executable_size_bytes, bool)
            or not 0 <= self.executable_size_bytes <= CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime executable size is invalid"
            )
        if (
            not isinstance(self.executable_mode, int)
            or isinstance(self.executable_mode, bool)
            or not 0 <= self.executable_mode <= 0o777
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime executable mode is invalid"
            )
        if (
            not isinstance(self.executable_owner_uid, int)
            or isinstance(self.executable_owner_uid, bool)
            or self.executable_owner_uid < 0
            or self.executable_owner_uid != os.getuid()
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime executable owner UID is invalid"
            )
        if Path(self.import_path) != root_path / "src" / "quantipy":
            raise AutoresearchValidationError(
                "canonical Quantipy runtime import is not the src/quantipy package"
            )
        if _path_is_within(Path(self.base_interpreter_path), root_path):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter must be uv-managed external"
            )
        if not self.base_interpreter_version:
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter version is invalid"
            )
        if (
            not isinstance(self.base_interpreter_size_bytes, int)
            or isinstance(self.base_interpreter_size_bytes, bool)
            or not 0
            <= self.base_interpreter_size_bytes
            <= CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter size is invalid"
            )
        if (
            not isinstance(self.base_interpreter_mode, int)
            or isinstance(self.base_interpreter_mode, bool)
            or not 0 <= self.base_interpreter_mode <= 0o777
            or self.base_interpreter_mode & 0o002
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter mode is invalid"
            )
        if (
            not isinstance(self.base_interpreter_owner_uid, int)
            or isinstance(self.base_interpreter_owner_uid, bool)
            or self.base_interpreter_owner_uid < 0
            or self.base_interpreter_owner_uid != os.getuid()
        ):
            raise AutoresearchValidationError(
                "canonical Quantipy runtime base interpreter owner UID is invalid"
            )

    @classmethod
    def from_dict(cls, raw: object) -> CanonicalQuantipyRuntimeAttestation:
        data = _ensure_mapping(raw, label="canonical_quantipy_runtime")
        _require_exact_keys(
            data,
            label="canonical_quantipy_runtime",
            expected=(
                "root",
                "commit_sha",
                "readiness_quantipy_commit",
                "pyproject_sha256",
                "uv_lock_sha256",
                "venv_prefix",
                "executable_path",
                "executable_sha256",
                "executable_size_bytes",
                "executable_mode",
                "executable_owner_uid",
                "import_path",
                "base_interpreter_path",
                "base_interpreter_version",
                "base_interpreter_sha256",
                "base_interpreter_size_bytes",
                "base_interpreter_mode",
                "base_interpreter_owner_uid",
                "schema_version",
            ),
        )
        return cls(
            root=_require_str(data, "root"),
            commit_sha=_require_str(data, "commit_sha"),
            readiness_quantipy_commit=_require_str(data, "readiness_quantipy_commit"),
            pyproject_sha256=_require_sha256(data, "pyproject_sha256"),
            uv_lock_sha256=_require_sha256(data, "uv_lock_sha256"),
            venv_prefix=_require_str(data, "venv_prefix"),
            executable_path=_require_str(data, "executable_path"),
            executable_sha256=_require_sha256(data, "executable_sha256"),
            executable_size_bytes=_require_int(data, "executable_size_bytes"),
            executable_mode=_require_int(data, "executable_mode"),
            executable_owner_uid=_require_int(data, "executable_owner_uid"),
            import_path=_require_str(data, "import_path"),
            base_interpreter_path=_require_str(data, "base_interpreter_path"),
            base_interpreter_version=_require_str(data, "base_interpreter_version"),
            base_interpreter_sha256=_require_sha256(data, "base_interpreter_sha256"),
            base_interpreter_size_bytes=_require_int(data, "base_interpreter_size_bytes"),
            base_interpreter_mode=_require_int(data, "base_interpreter_mode"),
            base_interpreter_owner_uid=_require_int(data, "base_interpreter_owner_uid"),
            schema_version=_require_int(data, "schema_version"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "commit_sha": self.commit_sha,
            "readiness_quantipy_commit": self.readiness_quantipy_commit,
            "pyproject_sha256": self.pyproject_sha256,
            "uv_lock_sha256": self.uv_lock_sha256,
            "venv_prefix": self.venv_prefix,
            "executable_path": self.executable_path,
            "executable_sha256": self.executable_sha256,
            "executable_size_bytes": self.executable_size_bytes,
            "executable_mode": self.executable_mode,
            "executable_owner_uid": self.executable_owner_uid,
            "import_path": self.import_path,
            "base_interpreter_path": self.base_interpreter_path,
            "base_interpreter_version": self.base_interpreter_version,
            "base_interpreter_sha256": self.base_interpreter_sha256,
            "base_interpreter_size_bytes": self.base_interpreter_size_bytes,
            "base_interpreter_mode": self.base_interpreter_mode,
            "base_interpreter_owner_uid": self.base_interpreter_owner_uid,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class PlatformRuntimeRecoveryReceipt:
    """Versioned, immutable authorization for the exact historical v4→v5 repair."""

    expected_run_id: str
    implementation_commit: str
    implementation_manifest_sha256: str
    verification_history_sha256: tuple[str, ...]
    interruption_sha256: str
    prior_retry_receipt_sha256: str
    v4_verification_sha256: str
    v4_detached_run_manifest_sha256: str
    v4_detached_run_status_sha256: str
    old_worktree_runtime_commit: str
    runtime: CanonicalQuantipyRuntimeAttestation
    execution_command_sha256: str
    probe: ResearchPanelProbeReceipt
    operator_reason: str
    schema_version: int = PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION:
            raise AutoresearchValidationError(
                "unsupported platform runtime recovery receipt schema_version"
            )
        if (
            re.fullmatch(r"autoresearch-i[1-9][0-9]*-[0-9a-f]{7,12}-v5", self.expected_run_id)
            is None
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery receipt expected_run_id is invalid"
            )
        if re.fullmatch(r"[0-9a-f]{7,64}", self.implementation_commit) is None:
            raise AutoresearchValidationError(
                "platform runtime recovery implementation_commit is invalid"
            )
        if re.fullmatch(r"[0-9a-f]{7,64}", self.old_worktree_runtime_commit) is None:
            raise AutoresearchValidationError(
                "platform runtime recovery old runtime commit is invalid"
            )
        if len(self.verification_history_sha256) != 3:
            raise AutoresearchValidationError(
                "platform runtime recovery receipt requires exact v1/v2/v4 verification history"
            )
        for index, digest in enumerate(
            (
                *self.verification_history_sha256,
                self.implementation_manifest_sha256,
                self.interruption_sha256,
                self.prior_retry_receipt_sha256,
                self.v4_verification_sha256,
                self.v4_detached_run_manifest_sha256,
                self.v4_detached_run_status_sha256,
                self.execution_command_sha256,
            ),
            start=1,
        ):
            _validate_sha256(digest, label=f"platform_runtime_recovery_receipt.digest[{index}]")
        if not isinstance(self.runtime, CanonicalQuantipyRuntimeAttestation):
            raise AutoresearchValidationError(
                "platform runtime recovery receipt requires runtime attestation"
            )
        if not isinstance(self.probe, ResearchPanelProbeReceipt):
            raise AutoresearchValidationError(
                "platform runtime recovery receipt requires a research-panel probe"
            )
        if not self.operator_reason or self.operator_reason.strip() != self.operator_reason:
            raise AutoresearchValidationError(
                "platform runtime recovery receipt requires a trimmed operator reason"
            )

    @classmethod
    def from_dict(cls, raw: object) -> PlatformRuntimeRecoveryReceipt:
        data = _ensure_mapping(raw, label="platform_runtime_recovery_receipt")
        _require_exact_keys(
            data,
            label="platform_runtime_recovery_receipt",
            expected=(
                "expected_run_id",
                "implementation_commit",
                "implementation_manifest_sha256",
                "verification_history_sha256",
                "interruption_sha256",
                "prior_retry_receipt_sha256",
                "v4_verification_sha256",
                "v4_detached_run_manifest_sha256",
                "v4_detached_run_status_sha256",
                "old_worktree_runtime_commit",
                "runtime",
                "execution_command_sha256",
                "probe",
                "operator_reason",
                "schema_version",
            ),
        )
        history = data["verification_history_sha256"]
        if not isinstance(history, list):
            raise AutoresearchValidationError(
                "platform runtime recovery verification_history_sha256 must be a list"
            )
        return cls(
            expected_run_id=_require_str(data, "expected_run_id"),
            implementation_commit=_require_str(data, "implementation_commit"),
            implementation_manifest_sha256=_require_sha256(data, "implementation_manifest_sha256"),
            verification_history_sha256=tuple(
                _require_sha256({"value": digest}, "value") for digest in history
            ),
            interruption_sha256=_require_sha256(data, "interruption_sha256"),
            prior_retry_receipt_sha256=_require_sha256(data, "prior_retry_receipt_sha256"),
            v4_verification_sha256=_require_sha256(data, "v4_verification_sha256"),
            v4_detached_run_manifest_sha256=_require_sha256(
                data, "v4_detached_run_manifest_sha256"
            ),
            v4_detached_run_status_sha256=_require_sha256(data, "v4_detached_run_status_sha256"),
            old_worktree_runtime_commit=_require_str(data, "old_worktree_runtime_commit"),
            runtime=CanonicalQuantipyRuntimeAttestation.from_dict(data["runtime"]),
            execution_command_sha256=_require_sha256(data, "execution_command_sha256"),
            probe=ResearchPanelProbeReceipt.from_dict(data["probe"]),
            operator_reason=_require_str(data, "operator_reason"),
            schema_version=_require_int(data, "schema_version"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_run_id": self.expected_run_id,
            "implementation_commit": self.implementation_commit,
            "implementation_manifest_sha256": self.implementation_manifest_sha256,
            "verification_history_sha256": list(self.verification_history_sha256),
            "interruption_sha256": self.interruption_sha256,
            "prior_retry_receipt_sha256": self.prior_retry_receipt_sha256,
            "v4_verification_sha256": self.v4_verification_sha256,
            "v4_detached_run_manifest_sha256": self.v4_detached_run_manifest_sha256,
            "v4_detached_run_status_sha256": self.v4_detached_run_status_sha256,
            "old_worktree_runtime_commit": self.old_worktree_runtime_commit,
            "runtime": self.runtime.to_dict(),
            "execution_command_sha256": self.execution_command_sha256,
            "probe": self.probe.to_dict(),
            "operator_reason": self.operator_reason,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class AutoresearchState:
    phase: Phase = Phase.SETUP_CONTEXT
    iteration: int = 1
    consensus_retry_count: int = 0
    verification_fix_attempts: int = 0
    setup: SetupContextArtifact | None = None
    context_packet: ContextPacketArtifact | None = None
    debate_rounds: tuple[DebateResultArtifact, ...] = field(default_factory=tuple)
    consensus_history: tuple[ConsensusResultArtifact, ...] = field(default_factory=tuple)
    implementation_result: ImplementationResultArtifact | None = None
    verification_history: tuple[VerificationResultArtifact, ...] = field(default_factory=tuple)
    review_history: tuple[ReviewResultArtifact, ...] = field(default_factory=tuple)
    fix_history: tuple[FixResultArtifact, ...] = field(default_factory=tuple)
    pending_fix_trigger: FixTriggerPhase | None = None
    final_decision: FinalDecisionArtifact | None = None
    memory_written: bool = False
    mode: ResearchMode | None = None
    memory_verification_receipt: MemoryVerificationReceipt | None = None
    platform_readiness: ReadinessIdentity | None = None
    external_verification_retry_receipt: ExternalVerificationRetryReceipt | None = None
    interrupted_verification_history: tuple[InterruptedVerificationAttemptReceipt, ...] = field(
        default_factory=tuple
    )
    platform_runtime_recovery_receipt: PlatformRuntimeRecoveryReceipt | None = None
    canonical_quantipy_runtime_attestation: CanonicalQuantipyRuntimeAttestation | None = None
    suspended: bool = False
    suspension_reason: str | None = None

    @property
    def latest_debate(self) -> DebateResultArtifact | None:
        return self.debate_rounds[-1] if self.debate_rounds else None

    @property
    def latest_consensus(self) -> ConsensusResultArtifact | None:
        return self.consensus_history[-1] if self.consensus_history else None

    @property
    def latest_verification(self) -> VerificationResultArtifact | None:
        return self.verification_history[-1] if self.verification_history else None

    @property
    def latest_review(self) -> ReviewResultArtifact | None:
        return self.review_history[-1] if self.review_history else None

    @property
    def latest_fix(self) -> FixResultArtifact | None:
        return self.fix_history[-1] if self.fix_history else None

    @classmethod
    def from_dict(cls, raw: object) -> AutoresearchState:
        data = _ensure_mapping(raw, label="autoresearch_state")
        if "schema_version" not in data:
            raise AutoresearchValidationError(
                "autoresearch state missing schema_version is unsupported; archive it and "
                "initialize a schema-v4 state with `gateway-cli autoresearch-init-state`; "
                f"expected schema_version={AUTORESEARCH_STATE_SCHEMA_VERSION}"
            )
        schema_version = _require_int(data, "schema_version")
        if schema_version != AUTORESEARCH_STATE_SCHEMA_VERSION:
            raise AutoresearchValidationError(
                "autoresearch state cannot be migrated in place; stop the supervisor, archive "
                f"the live schema-v{schema_version} state, and initialize a fresh schema-v"
                f"{AUTORESEARCH_STATE_SCHEMA_VERSION} state with "
                "`gateway-cli autoresearch-init-state` before restart"
            )

        def _parse_tuple(
            field_name: str,
            parser: Callable[[object], _T],
        ) -> tuple[_T, ...]:
            value = data.get(field_name, [])
            if not isinstance(value, Sequence) or isinstance(value, str | bytes):
                raise AutoresearchValidationError(f"{field_name} must be a list")
            return tuple(parser(item) for item in value)

        setup_raw = data.get("setup")
        context_raw = data.get("context_packet")
        implementation_raw = data.get("implementation_result")
        decision_raw = data.get("final_decision")
        receipt_raw = data.get("memory_verification_receipt")
        platform_readiness_raw = data.get("platform_readiness")
        external_retry_raw = data.get("external_verification_retry_receipt")
        platform_runtime_recovery_raw = data.get("platform_runtime_recovery_receipt")
        canonical_runtime_raw = data.get("canonical_quantipy_runtime_attestation")
        pending_fix_trigger_raw = data.get("pending_fix_trigger")
        if pending_fix_trigger_raw is not None and not isinstance(pending_fix_trigger_raw, str):
            raise AutoresearchValidationError("pending_fix_trigger must be a string or null")
        if "mode" not in data:
            raise AutoresearchValidationError(
                "mode must be explicit in persisted autoresearch state"
            )
        expected_state_keys: tuple[str, ...] = (
            "schema_version",
            "phase",
            "iteration",
            "consensus_retry_count",
            "verification_fix_attempts",
            "setup",
            "context_packet",
            "debate_rounds",
            "consensus_history",
            "implementation_result",
            "verification_history",
            "review_history",
            "fix_history",
            "pending_fix_trigger",
            "final_decision",
            "memory_written",
            "mode",
            "memory_verification_receipt",
            "platform_readiness",
            "external_verification_retry_receipt",
            "interrupted_verification_history",
            "platform_runtime_recovery_receipt",
            "canonical_quantipy_runtime_attestation",
            "suspended",
            "suspension_reason",
        )
        if "interrupted_verification_history" not in data:
            expected_state_keys = tuple(
                key for key in expected_state_keys if key != "interrupted_verification_history"
            )
        if "platform_runtime_recovery_receipt" not in data:
            expected_state_keys = tuple(
                key for key in expected_state_keys if key != "platform_runtime_recovery_receipt"
            )
        if "canonical_quantipy_runtime_attestation" not in data:
            expected_state_keys = tuple(
                key
                for key in expected_state_keys
                if key != "canonical_quantipy_runtime_attestation"
            )
        _require_exact_keys(data, label="autoresearch_state", expected=expected_state_keys)
        mode_raw = data.get("mode")
        if mode_raw is not None and not isinstance(mode_raw, str):
            raise AutoresearchValidationError("mode must be a string or null")

        state = cls(
            phase=Phase(_require_str(data, "phase")) if "phase" in data else Phase.SETUP_CONTEXT,
            iteration=_require_int(data, "iteration") if "iteration" in data else 1,
            consensus_retry_count=_require_int(data, "consensus_retry_count")
            if "consensus_retry_count" in data
            else 0,
            verification_fix_attempts=_require_int(data, "verification_fix_attempts")
            if "verification_fix_attempts" in data
            else 0,
            setup=SetupContextArtifact.from_dict(setup_raw) if setup_raw is not None else None,
            context_packet=ContextPacketArtifact.from_dict(context_raw)
            if context_raw is not None
            else None,
            debate_rounds=_parse_tuple("debate_rounds", DebateResultArtifact.from_dict),
            consensus_history=_parse_tuple("consensus_history", ConsensusResultArtifact.from_dict),
            implementation_result=ImplementationResultArtifact.from_dict(implementation_raw)
            if implementation_raw is not None
            else None,
            verification_history=_parse_tuple(
                "verification_history",
                lambda item: VerificationResultArtifact.from_dict(
                    item,
                    mode=ResearchMode(mode_raw) if mode_raw is not None else None,
                ),
            ),
            review_history=_parse_tuple("review_history", ReviewResultArtifact.from_dict),
            fix_history=_parse_tuple("fix_history", FixResultArtifact.from_dict),
            pending_fix_trigger=FixTriggerPhase(pending_fix_trigger_raw)
            if pending_fix_trigger_raw is not None
            else None,
            final_decision=FinalDecisionArtifact.from_dict(decision_raw)
            if decision_raw is not None
            else None,
            memory_written=_require_bool(data, "memory_written")
            if "memory_written" in data
            else False,
            mode=ResearchMode(mode_raw) if mode_raw is not None else None,
            memory_verification_receipt=MemoryVerificationReceipt.from_dict(receipt_raw)
            if receipt_raw is not None
            else None,
            platform_readiness=ReadinessIdentity.from_dict(platform_readiness_raw)
            if platform_readiness_raw is not None
            else None,
            external_verification_retry_receipt=ExternalVerificationRetryReceipt.from_dict(
                external_retry_raw
            )
            if external_retry_raw is not None
            else None,
            interrupted_verification_history=_parse_tuple(
                "interrupted_verification_history",
                InterruptedVerificationAttemptReceipt.from_dict,
            ),
            platform_runtime_recovery_receipt=PlatformRuntimeRecoveryReceipt.from_dict(
                platform_runtime_recovery_raw
            )
            if platform_runtime_recovery_raw is not None
            else None,
            canonical_quantipy_runtime_attestation=(
                CanonicalQuantipyRuntimeAttestation.from_dict(canonical_runtime_raw)
                if canonical_runtime_raw is not None
                else None
            ),
            suspended=_require_bool(data, "suspended") if "suspended" in data else False,
            suspension_reason=(
                _require_str(data, "suspension_reason")
                if data.get("suspension_reason") is not None
                else None
            ),
        )
        return state

    def to_dict(self) -> dict[str, object]:
        verification_history = [artifact.to_dict() for artifact in self.verification_history]
        state: dict[str, object] = {
            "schema_version": AUTORESEARCH_STATE_SCHEMA_VERSION,
            "phase": self.phase.value,
            "iteration": self.iteration,
            "consensus_retry_count": self.consensus_retry_count,
            "verification_fix_attempts": self.verification_fix_attempts,
            "setup": self.setup.to_dict() if self.setup else None,
            "context_packet": self.context_packet.to_dict() if self.context_packet else None,
            "debate_rounds": [artifact.to_dict() for artifact in self.debate_rounds],
            "consensus_history": [artifact.to_dict() for artifact in self.consensus_history],
            "implementation_result": self.implementation_result.to_dict()
            if self.implementation_result
            else None,
            "verification_history": verification_history,
            "review_history": [artifact.to_dict() for artifact in self.review_history],
            "fix_history": [artifact.to_dict() for artifact in self.fix_history],
            "pending_fix_trigger": self.pending_fix_trigger.value
            if self.pending_fix_trigger is not None
            else None,
            "final_decision": self.final_decision.to_dict() if self.final_decision else None,
            "memory_written": self.memory_written,
            "mode": self.mode.value if self.mode is not None else None,
            "memory_verification_receipt": self.memory_verification_receipt.to_dict()
            if self.memory_verification_receipt is not None
            else None,
            "platform_readiness": self.platform_readiness.to_dict()
            if self.platform_readiness is not None
            else None,
            "external_verification_retry_receipt": (
                self.external_verification_retry_receipt.to_dict()
                if self.external_verification_retry_receipt is not None
                else None
            ),
            "suspended": self.suspended,
            "suspension_reason": self.suspension_reason,
        }
        if self.interrupted_verification_history:
            state["interrupted_verification_history"] = [
                receipt.to_dict() for receipt in self.interrupted_verification_history
            ]
        if self.platform_runtime_recovery_receipt is not None:
            state["platform_runtime_recovery_receipt"] = (
                self.platform_runtime_recovery_receipt.to_dict()
            )
        if self.canonical_quantipy_runtime_attestation is not None:
            state["canonical_quantipy_runtime_attestation"] = (
                self.canonical_quantipy_runtime_attestation.to_dict()
            )
        return state


def _deterministic_quantipy_run_id(iteration: int, commit_sha: str, *, attempt: int) -> str:
    if iteration < 1:
        raise AutoresearchValidationError("iteration must be >= 1")
    if re.fullmatch(r"[0-9a-f]{7,64}", commit_sha) is None:
        raise AutoresearchValidationError("implementation_result commit_sha is invalid")
    if attempt < 1:
        raise AutoresearchValidationError("Quantipy verification attempt must be >= 1")
    base = f"autoresearch-i{iteration}-{commit_sha[:12]}"
    return base if attempt == 1 else f"{base}-v{attempt}"


def _expected_quantipy_verification_run_id(state: AutoresearchState, commit_sha: str) -> str:
    receipt = state.external_verification_retry_receipt
    if receipt is not None:
        expected = _deterministic_quantipy_run_id(
            state.iteration,
            commit_sha,
            attempt=receipt.retry_attempt,
        )
        if receipt.expected_run_id != expected:
            raise AutoresearchValidationError(
                "external verification retry receipt run ID is stale for the implementation commit"
            )
        return receipt.expected_run_id
    return _deterministic_quantipy_run_id(state.iteration, commit_sha, attempt=1)


def _validate_external_verification_retry_eligibility(state: AutoresearchState) -> int:
    if state.phase is not Phase.FIX_TEST:
        raise AutoresearchValidationError("external verification retry requires fix_test phase")
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        raise AutoresearchValidationError(
            "external verification retry requires an ALPHA_RESEARCH iteration"
        )
    if state.pending_fix_trigger is not FixTriggerPhase.VERIFICATION:
        raise AutoresearchValidationError(
            "external verification retry requires a verification-triggered fix_test"
        )
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "external verification retry requires the preserved implementation_result"
        )
    if (
        state.latest_verification is None
        or state.latest_verification.status is not VerificationStatus.TEST_FAILURE
    ):
        raise AutoresearchValidationError(
            "external verification retry requires a typed TEST_FAILURE verification"
        )
    current_receipt = state.external_verification_retry_receipt
    if current_receipt is not None:
        _validate_external_verification_retry_history(state, current_receipt)
    evidence = state.latest_verification.quantipy_experiment_evidence
    if evidence is None:
        raise AutoresearchValidationError(
            "external verification retry requires a completed failed Quantipy run artifact"
        )
    failure = evidence.failure
    if current_receipt is None:
        if (
            len(state.verification_history) != 1
            or state.fix_history
            or state.verification_fix_attempts
        ):
            raise AutoresearchValidationError(
                "external verification retry without a prior retry requires the initial "
                "panel failure"
            )
        if (
            failure is None
            or failure.category != "panel"
            or not _is_historically_authorized_local_research_panel_http_404(state, failure.message)
        ):
            raise AutoresearchValidationError(
                "external verification retry requires the historical local research-panel HTTP "
                "404 failure"
            )
        return 2
    if (
        failure is None
        or failure.category != "panel"
        or not _is_manifest_bound_legacy_local_research_panel_http_413(state, failure.message)
    ):
        raise AutoresearchValidationError(
            "external verification retry requires the exact local research-panel HTTP 413 failure"
        )
    if current_receipt.retry_attempt == 3:
        raise AutoresearchValidationError(
            "interrupted verification recovery accepts only the exact pending v3 topology"
        )
    if current_receipt.retry_attempt == 4:
        raise AutoresearchValidationError(
            "v4 platform receipt failure requires operator platform runtime recovery"
        )
    if evidence.success or evidence.panel is not None or evidence.completed_stages:
        raise AutoresearchValidationError(
            "external verification retry requires a failed pre-stage panel run without evidence"
        )
    if evidence.run_id != current_receipt.expected_run_id:
        raise AutoresearchValidationError(
            "external verification retry requires the prior expected Quantipy run artifact"
        )
    return current_receipt.retry_attempt + 1


def _validate_external_verification_retry_receipt(
    state: AutoresearchState,
    validation_context: AutoresearchValidationContext | None,
) -> None:
    receipt = state.external_verification_retry_receipt
    if receipt is None:
        if state.interrupted_verification_history:
            raise AutoresearchValidationError(
                "interrupted verification history requires an external verification retry receipt"
            )
        return
    if state.mode is not ResearchMode.ALPHA_RESEARCH or state.implementation_result is None:
        raise AutoresearchValidationError(
            "external verification retry receipt requires an ALPHA_RESEARCH implementation"
        )
    _validate_external_verification_retry_history(state, receipt)
    implementation = state.implementation_result
    readiness = state.platform_readiness
    if (
        receipt.implementation_commit != implementation.commit_sha
        or receipt.manifest_sha256 != implementation.experiment_manifest_sha256
        or readiness is None
        or receipt.readiness_manifest_id != readiness.manifest_id
        or receipt.readiness_snapshot_id != readiness.snapshot_id
    ):
        raise AutoresearchValidationError(
            "external verification retry receipt does not bind implementation/readiness identity"
        )
    expected = _deterministic_quantipy_run_id(
        state.iteration,
        implementation.commit_sha,
        attempt=receipt.retry_attempt,
    )
    if receipt.expected_run_id != expected:
        raise AutoresearchValidationError(
            "external verification retry receipt run ID does not bind the implementation"
        )
    _validate_interrupted_verification_history(state, receipt)
    _validate_platform_runtime_recovery_receipt(state, receipt, validation_context)


def _validate_platform_runtime_recovery_receipt(
    state: AutoresearchState,
    receipt: ExternalVerificationRetryReceipt,
    validation_context: AutoresearchValidationContext | None,
) -> None:
    recovery = state.platform_runtime_recovery_receipt
    if recovery is None:
        if receipt.retry_attempt == 5:
            raise AutoresearchValidationError(
                "v5 external verification retry requires a platform runtime recovery receipt"
            )
        return
    implementation = state.implementation_result
    if implementation is None:
        raise AutoresearchValidationError(
            "platform runtime recovery receipt requires implementation"
        )
    if validation_context is None or validation_context.quantipy_commit is None:
        raise AutoresearchValidationError(
            "platform runtime recovery receipt requires the readiness-pinned Quantipy commit"
        )
    if (
        state.phase is not Phase.VERIFICATION
        or receipt.retry_attempt != 5
        or receipt.schema_version != INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
        or len(state.interrupted_verification_history) != 1
        or len(state.verification_history) != 3
        or recovery.expected_run_id != receipt.expected_run_id
        or recovery.implementation_commit != implementation.commit_sha
        or recovery.implementation_manifest_sha256 != implementation.experiment_manifest_sha256
        or recovery.probe != receipt.probe
        or recovery.operator_reason != receipt.operator_reason
    ):
        raise AutoresearchValidationError(
            "platform runtime recovery receipt does not bind the exact pending v5 topology"
        )
    history_sha256 = tuple(
        _canonical_json_digest(artifact.to_dict()) for artifact in state.verification_history
    )
    interruption = state.interrupted_verification_history[0]
    v4 = state.verification_history[-1]
    evidence = v4.quantipy_experiment_evidence
    failure = evidence.failure if evidence is not None else None
    if (
        recovery.verification_history_sha256 != history_sha256
        or recovery.interruption_sha256 != _canonical_json_digest(interruption.to_dict())
        or recovery.prior_retry_receipt_sha256
        != _canonical_json_digest(interruption.prior_retry_receipt.to_dict())
        or recovery.v4_verification_sha256 != history_sha256[-1]
        or recovery.v4_detached_run_manifest_sha256
        != (evidence.detached_run_manifest_sha256 if evidence is not None else "")
        or recovery.old_worktree_runtime_commit != implementation.commit_sha
        or v4.status is not VerificationStatus.TEST_FAILURE
        or v4.tests_passed
        or evidence is None
        or evidence.success
        or evidence.panel is not None
        or evidence.completed_stages
        or evidence.terminal_stage is not None
        or evidence.terminal_status is not None
        or failure is None
        or failure.category != "panel"
        or failure.message != "ExperimentPanelError: Research panel receipt is invalid."
    ):
        raise AutoresearchValidationError(
            "platform runtime recovery receipt does not bind the exact v4 panel receipt failure"
        )
    try:
        import gateway.autoresearch_runs as detached_runs

        record = detached_runs.read_run_record(
            run_dir=Path(evidence.detached_run_directory),
            runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "platform runtime recovery receipt v4 detached record is unavailable or invalid"
        ) from exc
    if recovery.v4_detached_run_status_sha256 != _canonical_json_digest(record.status.to_dict()):
        raise AutoresearchValidationError(
            "platform runtime recovery receipt v4 detached status changed"
        )
    if (
        recovery.runtime.readiness_quantipy_commit != validation_context.quantipy_commit
        or _attest_canonical_quantipy_runtime(
            state,
            implementation,
            readiness_quantipy_commit=validation_context.quantipy_commit,
        )
        != recovery.runtime
    ):
        raise AutoresearchValidationError(
            "platform runtime recovery receipt canonical runtime attestation changed"
        )
    contract = build_quantipy_execution_contract(
        runtime_root=Path(recovery.runtime.root),
        manifest_path=Path(implementation.experiment_manifest_path),
        output_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        run_id=receipt.expected_run_id,
    )
    command_sha256 = hashlib.sha256("\0".join(contract.command).encode("utf-8")).hexdigest()
    if recovery.execution_command_sha256 != command_sha256:
        raise AutoresearchValidationError(
            "platform runtime recovery receipt does not bind the canonical v5 command"
        )


def _validate_interrupted_verification_history(
    state: AutoresearchState,
    receipt: ExternalVerificationRetryReceipt,
) -> None:
    """Validate the one supported, recorded gap between the v2 and v4 artifacts."""
    interruptions = state.interrupted_verification_history
    if not interruptions:
        return
    if len(interruptions) != 1 or receipt.retry_attempt < 4:
        raise AutoresearchValidationError(
            "interrupted verification recovery accepts exactly one recorded v3 interruption"
        )
    interruption = interruptions[0]
    implementation = state.implementation_result
    assert implementation is not None
    expected_v3 = _deterministic_quantipy_run_id(
        state.iteration, implementation.commit_sha, attempt=3
    )
    history_sha256 = tuple(
        _canonical_json_digest(artifact.to_dict()) for artifact in state.verification_history[:2]
    )
    if (
        interruption.expected_run_id != expected_v3
        or interruption.implementation_commit != implementation.commit_sha
        or interruption.implementation_manifest_sha256 != implementation.experiment_manifest_sha256
        or interruption.verification_history_sha256 != history_sha256
        or receipt.interruption_history_sha256 != (_canonical_json_digest(interruption.to_dict()),)
    ):
        raise AutoresearchValidationError(
            "interrupted verification receipt topology does not bind the preserved v1/v2 history"
        )
    prior_receipt = interruption.prior_retry_receipt
    if (
        prior_receipt.expected_run_id != expected_v3
        or prior_receipt.prior_verification_sha256 != history_sha256[-1]
        or prior_receipt.retry_attempt != 3
        or prior_receipt.implementation_commit != implementation.commit_sha
        or prior_receipt.manifest_sha256 != implementation.experiment_manifest_sha256
        or prior_receipt.verification_history_sha256 != history_sha256
        or prior_receipt.readiness_manifest_id != receipt.readiness_manifest_id
        or prior_receipt.readiness_snapshot_id != receipt.readiness_snapshot_id
    ):
        raise AutoresearchValidationError(
            "interrupted verification receipt does not preserve the immutable prior retry receipt"
        )
    predecessor = replace(
        state,
        phase=Phase.VERIFICATION,
        pending_fix_trigger=None,
        verification_history=state.verification_history[:2],
        external_verification_retry_receipt=prior_receipt,
        interrupted_verification_history=(),
        platform_runtime_recovery_receipt=None,
        canonical_quantipy_runtime_attestation=None,
    )
    predecessor_sha256 = _canonical_json_digest(predecessor.to_dict())
    readiness = predecessor.platform_readiness
    if (
        interruption.state_sha256 != predecessor_sha256
        and receipt.retry_attempt == 5
        and state.platform_runtime_recovery_receipt is not None
        and readiness is not None
        and readiness.quantipy_commit is not None
    ):
        historical_predecessor = replace(
            predecessor,
            platform_readiness=replace(readiness, quantipy_commit=None),
        )
        predecessor_sha256 = _canonical_json_digest(historical_predecessor.to_dict())
    if (
        interruption.prior_retry_receipt_sha256 != _canonical_json_digest(prior_receipt.to_dict())
        or interruption.state_sha256 != predecessor_sha256
    ):
        raise AutoresearchValidationError(
            "interrupted verification receipt does not bind the pre-recovery state "
            "and retry receipt"
        )
    try:
        import gateway.autoresearch_runs as detached_runs

        record = detached_runs.read_run_record(
            run_dir=Path(interruption.detached_run_directory),
            runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "interrupted verification receipt detached v3 record is unavailable or invalid"
        ) from exc
    if (
        record.manifest.state_reference_sha256 != interruption.state_reference_sha256
        or record.manifest.instruction_manifest_sha256 != interruption.instruction_manifest_sha256
        or record.status.manifest_sha256 != interruption.detached_run_manifest_sha256
        or _canonical_json_digest(record.status.to_dict())
        != interruption.detached_run_status_sha256
    ):
        raise AutoresearchValidationError(
            "interrupted verification receipt detached v3 manifest/status digest does not match"
        )


def _validate_external_verification_retry_history(
    state: AutoresearchState,
    receipt: ExternalVerificationRetryReceipt,
) -> None:
    """Fail closed unless every sealed retry artifact forms one exact chain."""
    interruptions = state.interrupted_verification_history
    if (
        interruptions
        and receipt.schema_version != INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
    ):
        raise AutoresearchValidationError(
            "interrupted verification history requires the interruption-aware retry receipt"
        )
    if (
        not interruptions
        and receipt.schema_version == INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
    ):
        raise AutoresearchValidationError(
            "interruption-aware retry receipt requires an interruption history"
        )
    interruption_count = len(interruptions)
    pending_history_length = receipt.retry_attempt - 1 - interruption_count
    sealed_history_length = receipt.retry_attempt - interruption_count
    if pending_history_length < 1:
        raise AutoresearchValidationError(
            "external verification retry interruption topology is invalid"
        )
    history_length = len(state.verification_history)
    if history_length not in (pending_history_length, sealed_history_length):
        raise AutoresearchValidationError(
            "external verification retry verification history topology is invalid"
        )
    if history_length == pending_history_length and state.phase is not Phase.VERIFICATION:
        raise AutoresearchValidationError(
            "external verification retry pending verification history topology is invalid"
        )
    if history_length == sealed_history_length and state.phase is Phase.VERIFICATION:
        raise AutoresearchValidationError(
            "external verification retry sealed verification history topology is invalid"
        )
    if state.fix_history or state.verification_fix_attempts:
        raise AutoresearchValidationError(
            "external verification retry verification history topology permits no fixer attempts"
        )
    implementation = state.implementation_result
    assert implementation is not None
    interruption_attempts = tuple(
        interruption.interrupted_attempt for interruption in interruptions
    )
    if interruption_attempts != tuple(sorted(interruption_attempts)) or len(
        set(interruption_attempts)
    ) != len(interruption_attempts):
        raise AutoresearchValidationError(
            "interrupted verification history attempts must be ordered and unique"
        )
    for index, artifact in enumerate(state.verification_history, start=1):
        attempt = index + sum(
            1 for interruption_attempt in interruption_attempts if interruption_attempt <= index
        )
        _validate_external_verification_retry_history_artifact(
            state,
            artifact,
            attempt=attempt,
            implementation=implementation,
        )
    prior = state.verification_history[pending_history_length - 1]
    if receipt.prior_verification_sha256 != _canonical_json_digest(prior.to_dict()):
        raise AutoresearchValidationError(
            "external verification retry receipt does not bind the immediately prior artifact"
        )
    if receipt.schema_version == LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION:
        return
    expected_history_sha256 = tuple(
        _canonical_json_digest(artifact.to_dict())
        for artifact in state.verification_history[:pending_history_length]
    )
    if receipt.verification_history_sha256 != expected_history_sha256:
        raise AutoresearchValidationError(
            "external verification retry receipt does not bind the complete ordered "
            "verification history"
        )
    expected_interruption_history_sha256 = tuple(
        _canonical_json_digest(interruption.to_dict()) for interruption in interruptions
    )
    if receipt.schema_version == INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION and (
        receipt.interruption_history_sha256 != expected_interruption_history_sha256
    ):
        raise AutoresearchValidationError(
            "external verification retry receipt does not bind the complete ordered "
            "interruption history"
        )


def _validate_external_verification_retry_history_artifact(
    state: AutoresearchState,
    artifact: VerificationResultArtifact,
    *,
    attempt: int,
    implementation: ImplementationResultArtifact,
) -> None:
    evidence = artifact.quantipy_experiment_evidence
    failure = evidence.failure if evidence is not None else None
    if (
        artifact.status is not VerificationStatus.TEST_FAILURE
        or evidence is None
        or evidence.run_id
        != _deterministic_quantipy_run_id(
            state.iteration, implementation.commit_sha, attempt=attempt
        )
        or evidence.success
        or evidence.panel is not None
        or evidence.completed_stages
        or evidence.terminal_stage is not None
        or evidence.terminal_status is not None
        or failure is None
        or failure.category != "panel"
    ):
        raise AutoresearchValidationError(
            "external verification retry verification history topology is invalid"
        )
    if attempt == 4 and state.platform_runtime_recovery_receipt is not None:
        exact_failure = (
            failure.message == "ExperimentPanelError: Research panel receipt is invalid."
        )
        status = "exact platform runtime v4 panel receipt"
    else:
        exact_failure = (
            _is_historically_authorized_local_research_panel_http_404
            if attempt == 1
            else _is_manifest_bound_legacy_local_research_panel_http_413
        )(state, failure.message)
        status = (
            "historical local research-panel HTTP 404"
            if attempt == 1
            else "exact local research-panel HTTP 413"
        )
    if not exact_failure:
        raise AutoresearchValidationError(
            f"external verification retry verification history requires the {status} failure"
        )


def _is_manifest_bound_legacy_local_research_panel_http_413(
    state: AutoresearchState,
    message: str,
) -> bool:
    """Accept solely the manifest-bound httpx 413 text from the preserved v2 artifact."""
    expected = _expected_local_research_panel_http_error_message(
        state,
        status=413,
        reason="Request Entity Too Large",
    )
    return expected is not None and message == expected


def _is_historically_authorized_local_research_panel_http_404(
    state: AutoresearchState,
    message: str,
) -> bool:
    """Validate the narrow 404 contract used only to issue the original v2 receipt."""
    expected = _expected_local_research_panel_http_error_message(
        state,
        status=404,
        reason="Not Found",
    )
    return expected is not None and message == expected


def _expected_local_research_panel_http_error_message(
    state: AutoresearchState,
    *,
    status: int,
    reason: str,
) -> str | None:
    try:
        request = _verified_panel_request_for_state(state)
        tickers = request["tickers"]
        start = _parse_utc_request_datetime(request["start"])
        end = _parse_utc_request_datetime(request["end"])
    except AutoresearchValidationError:
        return None
    if not isinstance(tickers, list):
        return None
    url = "http://127.0.0.1:8000/price-data/research-panel?" + urlencode(
        (
            ("tickers", ",".join(tickers)),
            ("start", start.isoformat()),
            ("end", end.isoformat()),
            ("timeframe", request["timeframe"]),
            ("market_hours", request["market_hours"]),
        )
    )
    return (
        f"ExperimentPanelError: Client error '{status} {reason}' for url '{url}'\n"
        f"For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{status}"
    )


def _parse_utc_request_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise AutoresearchValidationError("panel request datetime is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutoresearchValidationError("panel request datetime is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise AutoresearchValidationError("panel request datetime must be UTC-aware")
    return parsed.astimezone(UTC)


def _verified_panel_request_for_state(state: AutoresearchState) -> Mapping[str, object]:
    implementation = state.implementation_result
    if implementation is None:
        raise AutoresearchValidationError("panel request requires implementation_result")
    snapshot = _secure_open_snapshot(
        Path(implementation.experiment_manifest_path),
        label="implementation_result experiment_manifest_path",
    )
    manifest = _validate_quantipy_v2_manifest(
        snapshot,
        workspace=Path(implementation.workspace_path),
        commit_sha=implementation.commit_sha,
        expected_sha256=implementation.experiment_manifest_sha256,
    )
    panel = manifest.get("panel")
    if not isinstance(panel, Mapping):
        raise AutoresearchValidationError("experiment manifest must contain a panel request")
    request = panel.get("request")
    if not isinstance(request, Mapping):
        raise AutoresearchValidationError("experiment manifest panel request is invalid")
    return _validate_panel_request(request, label="experiment manifest panel request")


def retry_external_verification(
    state: AutoresearchState,
    receipt: ExternalVerificationRetryReceipt,
) -> AutoresearchState:
    """Resume only the current external TEST_FAILURE without invoking the alpha fixer."""
    _validate_external_verification_retry_eligibility(state)
    expected = ExternalVerificationRetryReceipt.for_state(
        state,
        receipt.probe,
        receipt.operator_reason,
    )
    if receipt != expected:
        raise AutoresearchValidationError(
            "external verification retry receipt does not match the current failed state"
        )
    return replace(
        state,
        external_verification_retry_receipt=receipt,
        pending_fix_trigger=None,
        phase=Phase.VERIFICATION,
    )


def _is_fail_closed_g0_platform_contract_bug_signal(
    verification: VerificationResultArtifact | None,
) -> bool:
    return (
        verification is not None
        and verification.status is VerificationStatus.BUG_SIGNAL
        and verification.bug_signals == (PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL,)
        and verification.infra_gate_outcome is None
        and verification.infra_rationale is None
        and verification.platform_coverage_validation is None
    )


def build_authoritative_state_reference(
    state: AutoresearchState,
    *,
    state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
) -> AuthoritativeStateReference:
    """Bind a stage dispatch to one canonical, complete persisted state."""
    canonical_state_model = AutoresearchState.from_dict(state.to_dict())
    canonical_state = _compact_json_block(canonical_state_model.to_dict())
    state_sha256 = _sha256_text("\n".join((AUTHORITATIVE_STATE_DIGEST_DOMAIN, canonical_state)))
    return AuthoritativeStateReference(
        version=AUTHORITATIVE_STATE_REFERENCE_VERSION,
        digest_domain=AUTHORITATIVE_STATE_DIGEST_DOMAIN,
        path=str(state_path.expanduser().resolve(strict=False)),
        state_sha256=state_sha256,
        phase=canonical_state_model.phase.value,
        iteration=canonical_state_model.iteration,
    )


def validate_authoritative_state_reference(
    reference: AuthoritativeStateReference,
) -> AutoresearchState:
    """Load the referenced state and reject any content, phase, or path mismatch."""
    if reference.version != AUTHORITATIVE_STATE_REFERENCE_VERSION:
        raise AutoresearchValidationError("authoritative state reference version is invalid")
    if reference.digest_domain != AUTHORITATIVE_STATE_DIGEST_DOMAIN:
        raise AutoresearchValidationError("authoritative state reference domain is invalid")
    _validate_sha256(reference.state_sha256, label="authoritative state reference state_sha256")
    state_path = Path(reference.path).expanduser().resolve(strict=False)
    state = load_state_file(state_path)
    expected = build_authoritative_state_reference(state, state_path=state_path)
    if expected != reference:
        raise AutoresearchValidationError(
            "authoritative state reference does not match the current state file"
        )
    return state


def _validate_persisted_state_matches(
    state: AutoresearchState,
    *,
    state_path: Path,
) -> AutoresearchState:
    """Reject an artifact handoff if its input state changed after dispatch."""
    supplied_reference = build_authoritative_state_reference(state, state_path=state_path)
    persisted_state = load_state_file(state_path)
    persisted_reference = build_authoritative_state_reference(
        persisted_state,
        state_path=state_path,
    )
    if persisted_reference != supplied_reference:
        raise AutoresearchValidationError(
            "persisted state does not match the supplied authoritative state"
        )
    return persisted_state


LOCAL_RECEIPT_PATHS: dict[str, Path] = {
    "g2.autoresearch_skill": Path("gateway/agent_config/skills/autoresearch/SKILL.md"),
    "g2.quantipy_methodology": Path("gateway/agent_config/skills/quantipy-methodology/SKILL.md"),
    "g2.quantipy_data_contract": Path(
        "gateway/agent_config/skills/quantipy-data-contract/SKILL.md"
    ),
}

QUANTIPY_RECEIPT_PATHS: dict[str, Path] = {
    "quantipy.agents": Path("AGENTS.md"),
    "quantipy.skill.backend_python": Path(".agents/skills/backend-python/SKILL.md"),
    "quantipy.skill.backtesting": Path(".agents/skills/backtesting/SKILL.md"),
    "quantipy.skill.data_collection": Path(".agents/skills/data-collection/SKILL.md"),
    "quantipy.skill.data_querying": Path(".agents/skills/data-querying/SKILL.md"),
    "quantipy.skill.experiment_data": Path(".agents/skills/experiment-data/SKILL.md"),
    "quantipy.agent.backend_python": Path(".codex/agents/backend-python.toml"),
    "quantipy.agent.contrarian": Path(".codex/agents/contrarian.toml"),
    "quantipy.agent.explorer": Path(".codex/agents/explorer.toml"),
    "quantipy.agent.orchestrator": Path(".codex/agents/orchestrator.toml"),
    "quantipy.agent.researcher": Path(".codex/agents/researcher.toml"),
    "quantipy.agent.reviewer": Path(".codex/agents/reviewer.toml"),
    "quantipy.agent.theorist": Path(".codex/agents/theorist.toml"),
}

PHASE_RECEIPTS: dict[Phase, tuple[str, ...]] = {
    Phase.SETUP_CONTEXT: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.experiment_data",
        "quantipy.skill.data_querying",
        "quantipy.agent.explorer",
        "quantipy.agent.researcher",
    ),
    Phase.DEBATE: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backend_python",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_collection",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.backend_python",
        "quantipy.agent.contrarian",
        "quantipy.agent.orchestrator",
        "quantipy.agent.researcher",
        "quantipy.agent.reviewer",
        "quantipy.agent.theorist",
    ),
    Phase.CONSENSUS: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_collection",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.contrarian",
        "quantipy.agent.researcher",
        "quantipy.agent.reviewer",
        "quantipy.agent.theorist",
    ),
    Phase.IMPLEMENTATION: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backend_python",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.backend_python",
        "quantipy.agent.orchestrator",
    ),
    Phase.VERIFICATION: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.reviewer",
    ),
    Phase.REVIEW: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.contrarian",
        "quantipy.agent.reviewer",
    ),
    Phase.FIX_TEST: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backend_python",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.backend_python",
        "quantipy.agent.orchestrator",
        "quantipy.agent.reviewer",
    ),
    Phase.DECISION_LOG: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
    ),
    Phase.REPEAT: (
        "g2.autoresearch_skill",
        "quantipy.agents",
    ),
}

ARTIFACT_CONTRACTS: dict[ArtifactType, dict[str, object]] = {
    ArtifactType.SETUP: {
        "required_fields": [
            "goal",
            "metric_name",
            "metric_direction",
            "target_repo",
            "writable_scope",
            "baseline_summary",
            "hard_constraints",
            "data_sources",
        ]
    },
    ArtifactType.CONTEXT_PACKET: {
        "required_fields": [
            "baseline_metric",
            "current_best_metric",
            "recent_experiment_outcomes",
            "prior_findings",
            "open_proposals",
            "hard_constraints",
            "available_data_sources",
            "loaded_quantipy_sources",
            "research_mode",
            "mode_rationale",
            "burned_theory_families",
        ],
        "field_types": {
            "baseline_metric": "string",
            "current_best_metric": "string",
            "recent_experiment_outcomes": "array[string]",
            "prior_findings": "array[string]",
            "open_proposals": "array[string]",
            "hard_constraints": "array[string]",
            "available_data_sources": "array[string]",
            "loaded_quantipy_sources": "array[string]",
            "research_mode": "enum[alpha_research,data_infra_g0]",
            "mode_rationale": "string",
            "burned_theory_families": "array[string]",
        },
        "shape_constraints": [
            "Use exactly the listed keys and no extra keys",
            "Do not use nested objects in the context_packet artifact",
            "Every array item must be a string",
        ],
    },
    ArtifactType.DEBATE_RESULT: {
        "required_fields": [
            "round_number",
            "submissions[5]",
            "submissions[*].example_tickers",
            "submissions[*].compute_fit",
        ],
    },
    ArtifactType.CONSENSUS_RESULT: {
        "required_fields": [
            "round_number",
            "status",
            "majority_count",
            "majority_agent_ids",
            "dissenting_positions",
            "novelty_score",
            "theory_score",
            "implementation_risk_score",
            "data_adequacy_score",
            "overfit_risk_score",
            "expected_net_sharpe",
            "rejection_reasons",
            "dissent_summary",
            "winner_theory_id|null",
            "implementation_brief|null",
            "universe_plan|null",
        ]
    },
    ArtifactType.IMPLEMENTATION_RESULT: {
        "required_fields": [
            "summary",
            "workspace_path",
            "commit_sha",
            "module_path",
            "notebook_path",
            "tests_added_or_updated",
            "commands_run",
            "experiment_manifest_path",
            "experiment_manifest_sha256",
            "compute_fit",
            "price_hydration_scope_preflight",
        ]
    },
    ArtifactType.VERIFICATION_RESULT: {
        "required_fields": [
            "status",
            "is_walk_forward_sharpe_net",
            "oos_sharpe_net",
            "max_drawdown_pct",
            "win_rate",
            "trade_count",
            "trades_per_day",
            "oos_trading_days",
            "feature_importances_summary",
            "null_test_summary",
            "bug_signals",
            "tests_passed",
            "commands_run",
            "data_coverage",
            "platform_coverage_validation",
            "infra_gate_outcome",
            "infra_rationale",
            "universe_verification_receipt",
            "price_hydration_receipt",
            "quantipy_experiment_evidence",
            "quantipy_execution_not_started",
        ],
        "mode_requirements": {
            ResearchMode.ALPHA_RESEARCH.value: {
                "infra_gate_outcome": None,
                "infra_rationale": None,
            },
            ResearchMode.DATA_INFRA_G0.value: {
                "infra_gate_outcome": "required",
                "infra_rationale": "required",
            },
        },
    },
    ArtifactType.REVIEW_RESULT: {
        "required_fields": [
            "reviewer_agent_id",
            "verdict",
            "recommended_metric_name",
            "recommended_metric_value",
            "critical_issues",
            "noncritical_issues",
            "fix_requests",
            "summary",
        ]
    },
    ArtifactType.FIX_RESULT: {
        "required_fields": [
            "trigger_phase",
            "summary",
            "workspace_path",
            "commit_sha",
            "fixes_applied",
            "tests_rerun",
            "remaining_issues",
            "price_hydration_scope_preflight",
        ]
    },
    ArtifactType.FINAL_DECISION: {
        "required_fields": [
            "experiment_id",
            "decision",
            "recommended_metric_name",
            "recommended_metric_value",
            "reviewer_verdict",
            "rationale",
            "log_summary",
            "continue_loop",
            "memory_write_required",
            "infra_rationale",
        ],
        "mode_requirements": {
            ResearchMode.ALPHA_RESEARCH.value: {"infra_rationale": None},
            ResearchMode.DATA_INFRA_G0.value: {"infra_rationale": "required"},
            "no_consensus": {
                "memory_write_required": False,
                "infra_rationale": None,
            },
        },
    },
    ArtifactType.MEMORY_WRITE: {
        "required_fields": ["memory_verification_receipt"],
        "receipt_type": "memory_verification_receipt",
    },
    ArtifactType.NEXT_ITERATION: {"required_fields": ["start_next_iteration"]},
}


def _canonical_receipt_path(path: Path) -> Path:
    try:
        canonical = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise AutoresearchReceiptError(f"missing required receipt source: {path}") from exc
    if not canonical.is_file():
        raise AutoresearchReceiptError(f"missing required receipt source: {canonical}")
    return canonical


def _load_receipt(receipt_id: str, path: Path) -> SourceReceipt:
    canonical = _canonical_receipt_path(path)
    try:
        content = canonical.read_bytes()
    except OSError as exc:
        raise AutoresearchReceiptError(f"unreadable required receipt source: {canonical}") from exc
    return SourceReceipt(
        receipt_id=receipt_id,
        path=canonical,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def build_receipt_catalog(quantipy_root: Path = DEFAULT_QUANTIPY_ROOT) -> ReceiptCatalog:
    receipts: dict[str, SourceReceipt] = {}
    for receipt_id, path in LOCAL_RECEIPT_PATHS.items():
        receipts[receipt_id] = _load_receipt(receipt_id, G2_OPENCLAW_REPO_ROOT / path)
    for receipt_id, relative_path in QUANTIPY_RECEIPT_PATHS.items():
        receipts[receipt_id] = _load_receipt(receipt_id, quantipy_root / relative_path)
    return ReceiptCatalog(receipts=receipts)


def _load_json(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchConfigError(f"missing config file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchConfigError(f"invalid JSON in config file: {path}") from exc
    return _ensure_mapping(raw, label=str(path))


def _agent_policy_from_json(
    agent_map: Mapping[str, Mapping[str, object]], agent_id: str
) -> StageAgentPolicy:
    try:
        raw = agent_map[agent_id]
    except KeyError as exc:
        raise AutoresearchConfigError(f"missing configured agent: {agent_id}") from exc
    model = _ensure_mapping(raw.get("model"), label=f"{agent_id}.model")
    skills = _require_string_list(raw, "skills")
    return StageAgentPolicy(
        agent_id=agent_id,
        model=_require_str(model, "primary"),
        reasoning=_require_str(raw, "thinkingDefault"),
        skills=skills,
    )


def _codex_agent_model(model: str) -> str:
    prefix = "openai/"
    if not model.startswith(prefix):
        raise AutoresearchConfigError(f"stage model must use OpenAI provider ref: {model}")
    return model.removeprefix(prefix)


def _load_codex_agent_toml(path: Path) -> Mapping[str, object]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchConfigError(f"missing native Codex stage agent file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AutoresearchConfigError(f"invalid native Codex stage agent TOML: {path}") from exc
    return _ensure_mapping(data, label=str(path))


def _validate_codex_native_stage_agents(policy: AutoresearchPolicy) -> None:
    for stage in (
        policy.context_curator,
        *policy.debate_agents,
        policy.consensus,
        policy.implementer,
        policy.reviewer,
        policy.fixer,
    ):
        path = G2_OPENCLAW_REPO_ROOT / ".codex" / "agents" / f"{stage.agent_id}.toml"
        data = _load_codex_agent_toml(path)
        if _require_str(data, "name") != stage.agent_id:
            raise AutoresearchConfigError(
                f"native Codex stage agent {path} must be named {stage.agent_id}"
            )
        if _require_str(data, "model") != _codex_agent_model(stage.model):
            raise AutoresearchConfigError(
                f"native Codex stage agent {stage.agent_id} must use {stage.model}"
            )
        if _require_str(data, "model_reasoning_effort") != stage.reasoning:
            raise AutoresearchConfigError(
                f"native Codex stage agent {stage.agent_id} must use {stage.reasoning} reasoning"
            )
        if "mcp_servers" in data:
            raise AutoresearchConfigError(
                f"native Codex stage agent {stage.agent_id} must not override inherited MCP servers"
            )


def load_autoresearch_policy(
    config_path: Path = DEFAULT_OPENCLAW_CONFIG_PATH,
) -> AutoresearchPolicy:
    config = _load_json(config_path)
    plugins = _ensure_mapping(config.get("plugins"), label="plugins")
    try:
        plugin_allow = _require_string_list(plugins, "allow")
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError("plugins.allow must explicitly include codex") from exc
    if "codex" not in plugin_allow:
        raise AutoresearchConfigError("plugins.allow must explicitly include codex")
    agents = _ensure_mapping(config.get("agents"), label="agents")
    defaults = _ensure_mapping(agents.get("defaults"), label="agents.defaults")
    compaction = _ensure_mapping(defaults.get("compaction"), label="agents.defaults.compaction")
    if _require_str(compaction, "mode") != "default":
        raise AutoresearchConfigError(
            "agents.defaults.compaction.mode must be default for the Codex OAuth route"
        )
    if defaults.get("maxConcurrent") != 2:
        raise AutoresearchConfigError(
            "agents.defaults.maxConcurrent must be 2 to cap the main lane with PM headroom"
        )
    default_subagents = _ensure_mapping(
        defaults.get("subagents"), label="agents.defaults.subagents"
    )
    if default_subagents.get("maxConcurrent") != 1:
        raise AutoresearchConfigError(
            "agents.defaults.subagents.maxConcurrent must be 1 to serialize heavy Codex stages"
        )
    if "maxChildrenPerAgent" in default_subagents:
        raise AutoresearchConfigError(
            "agents.defaults.subagents.maxChildrenPerAgent must not be configured"
        )
    models = _ensure_mapping(config.get("models"), label="models")
    providers = _ensure_mapping(models.get("providers"), label="providers")
    openai_provider = _ensure_mapping(providers.get("openai"), label="providers.openai")
    if _require_str(openai_provider, "api") != "openai-responses":
        raise AutoresearchConfigError("providers.openai.api must be openai-responses")
    agent_runtime = _ensure_mapping(
        openai_provider.get("agentRuntime"), label="providers.openai.agentRuntime"
    )
    if _require_str(agent_runtime, "id") != "codex":
        raise AutoresearchConfigError("providers.openai.agentRuntime.id must be codex")
    openai_models_raw = openai_provider.get("models")
    if not isinstance(openai_models_raw, Sequence) or isinstance(openai_models_raw, str | bytes):
        raise AutoresearchConfigError("providers.openai.models must be a list")
    model_caps: dict[str, bool] = {}
    for item in openai_models_raw:
        data = _ensure_mapping(item, label="provider_model")
        model_caps[_require_str(data, "id")] = _require_bool(data, "reasoning")
    for required_model in ("gpt-5.4", "gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra"):
        if model_caps.get(required_model) is not True:
            raise AutoresearchConfigError(f"openai/{required_model} must exist with reasoning=true")

    agent_list_raw = agents.get("list")
    if not isinstance(agent_list_raw, Sequence) or isinstance(agent_list_raw, str | bytes):
        raise AutoresearchConfigError("agents.list must be a list")
    agent_map: dict[str, Mapping[str, object]] = {}
    for item in agent_list_raw:
        data = _ensure_mapping(item, label="agent")
        agent_map[_require_str(data, "id")] = data

    policy = AutoresearchPolicy(
        pm=_agent_policy_from_json(agent_map, "autoresearch-pm"),
        main_interface=_agent_policy_from_json(agent_map, "main"),
        context_curator=_agent_policy_from_json(agent_map, "context_curator"),
        debate_agents=tuple(
            _agent_policy_from_json(agent_map, agent_id)
            for agent_id in (
                "debater_microstructure",
                "debater_data",
                "debater_skeptic",
                "debater_theory",
                "debater_implementation",
            )
        ),
        consensus=_agent_policy_from_json(agent_map, "consensus_arbiter"),
        implementer=_agent_policy_from_json(agent_map, "implementer"),
        reviewer=_agent_policy_from_json(agent_map, "reviewer"),
        fixer=_agent_policy_from_json(agent_map, "fixer"),
    )
    _validate_policy(policy, agent_map, config)
    return policy


def _validate_policy(
    policy: AutoresearchPolicy,
    agent_map: Mapping[str, Mapping[str, object]],
    config: Mapping[str, object],
) -> None:
    if policy.main_interface.model != "openai/gpt-5.4" or policy.main_interface.reasoning != "high":
        raise AutoresearchConfigError("main must be openai/gpt-5.4 with high reasoning")
    if tuple(policy.main_interface.skills) != ("mempalace-readonly",):
        raise AutoresearchConfigError("main must load exactly mempalace-readonly")
    main_raw = agent_map["main"]
    if main_raw.get("subagents") is not None:
        raise AutoresearchConfigError("main must not declare a subagent allowlist")
    main_tools = _ensure_mapping(main_raw.get("tools"), label="main.tools")
    if main_tools.get("profile") != "minimal":
        raise AutoresearchConfigError("main.tools.profile must be minimal")
    main_allowed_tool_list = _require_string_list(main_tools, "allow")
    if tuple(main_allowed_tool_list) != MAIN_OPENCLAW_TOOL_ALLOW_POLICY:
        raise AutoresearchConfigError(
            "main must allow exactly the direct Codex MCP control/read-only tools"
        )
    try:
        plugins = _ensure_mapping(config.get("plugins"), label="plugins")
        entries = _ensure_mapping(plugins.get("entries"), label="plugins.entries")
        codex = _ensure_mapping(entries.get("codex"), label="plugins.entries.codex")
        plugin_config = _ensure_mapping(codex.get("config"), label="plugins.entries.codex.config")
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError(str(exc)) from exc
    if "nativeToolSurfaceEnabled" in plugin_config:
        raise AutoresearchConfigError(
            "nativeToolSurfaceEnabled is not supported by the current Codex plugin schema"
        )
    if "codexDynamicToolsExclude" in plugin_config:
        raise AutoresearchConfigError(
            "codexDynamicToolsExclude must not be used as a native Codex tool guard"
        )
    main_denied_tool_list = set(_require_string_list(main_tools, "deny"))
    required_main_denies = {
        "exec",
        "sessions_spawn",
        "sessions_yield",
        "sessions_send",
        "sessions_list",
        "sessions_history",
        "agents_list",
    }
    if not required_main_denies <= main_denied_tool_list:
        raise AutoresearchConfigError("main must deny native exec and OpenClaw session/agent tools")
    if policy.pm.model != "openai/gpt-5.6-sol" or policy.pm.reasoning != "high":
        raise AutoresearchConfigError("PM must be openai/gpt-5.6-sol with high reasoning")
    if (
        policy.context_curator.model != "openai/gpt-5.4"
        or policy.context_curator.reasoning != "high"
    ):
        raise AutoresearchConfigError("context_curator must be openai/gpt-5.4 with high reasoning")

    expected_debate_models = {
        "debater_microstructure": "openai/gpt-5.5",
        "debater_data": "openai/gpt-5.6-terra",
        "debater_skeptic": "openai/gpt-5.5",
        "debater_theory": "openai/gpt-5.4",
        "debater_implementation": "openai/gpt-5.4",
    }
    for agent in policy.debate_agents:
        if agent.reasoning != "high":
            raise AutoresearchConfigError(f"{agent.agent_id} must use high reasoning")
        if agent.model != expected_debate_models[agent.agent_id]:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must be {expected_debate_models[agent.agent_id]} "
                "with high reasoning"
            )

    if policy.consensus.model != "openai/gpt-5.6-sol" or policy.consensus.reasoning != "high":
        raise AutoresearchConfigError(
            "consensus_arbiter must be openai/gpt-5.6-sol with high reasoning"
        )
    for agent in (policy.implementer, policy.fixer):
        if agent.model != "openai/gpt-5.4" or agent.reasoning != "high":
            raise AutoresearchConfigError(
                f"{agent.agent_id} must be openai/gpt-5.4 with high reasoning"
            )
    if policy.reviewer.model != "openai/gpt-5.6-sol" or policy.reviewer.reasoning != "high":
        raise AutoresearchConfigError("reviewer must be exactly one openai/gpt-5.6-sol high agent")
    if policy.reviewer.agent_id != "reviewer":
        raise AutoresearchConfigError("reviewer stage must be configured as agent id 'reviewer'")

    if tuple(policy.pm.skills) != ("mempalace-readonly", "autoresearch"):
        raise AutoresearchConfigError("PM must load exactly mempalace-readonly and autoresearch")
    pm_raw = agent_map["autoresearch-pm"]
    pm_tools = _ensure_mapping(pm_raw.get("tools"), label="autoresearch-pm.tools")
    pm_denied_tool_list = _require_string_list(pm_tools, "deny")
    if tuple(pm_denied_tool_list) != PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS:
        raise AutoresearchConfigError(
            "PM must deny OpenClaw/session discovery and delegation tools "
            "for native Codex delegation"
        )
    if pm_raw.get("subagents") is not None:
        raise AutoresearchConfigError("PM must not declare OpenClaw subagents")
    _validate_codex_app_server_sandbox(config)
    _validate_mempalace_server_split(config, policy)
    _validate_codex_native_stage_agents(policy)
    for agent in (
        policy.context_curator,
        *policy.debate_agents,
        policy.consensus,
        policy.implementer,
        policy.reviewer,
        policy.fixer,
    ):
        if tuple(agent.skills) != (
            "mempalace-readonly",
            "quantipy-methodology",
            "quantipy-data-contract",
        ):
            raise AutoresearchConfigError(
                f"{agent.agent_id} must load exactly mempalace-readonly, "
                "quantipy-methodology, and quantipy-data-contract"
            )
        if agent_map[agent.agent_id].get("subagents") is not None:
            raise AutoresearchConfigError(f"{agent.agent_id} must not declare OpenClaw subagents")
        if agent_map[agent.agent_id].get("tools") is not None:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must not carry MemPalace write-tool policy remnants"
            )


def _validate_codex_app_server_sandbox(config: Mapping[str, object]) -> None:
    try:
        plugins = _ensure_mapping(config.get("plugins"), label="plugins")
        entries = _ensure_mapping(plugins.get("entries"), label="plugins.entries")
        codex = _ensure_mapping(entries.get("codex"), label="plugins.entries.codex")
        plugin_config = _ensure_mapping(codex.get("config"), label="plugins.entries.codex.config")
        app_server = _ensure_mapping(
            plugin_config.get("appServer"),
            label="plugins.entries.codex.config.appServer",
        )
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError(str(exc)) from exc
    if app_server.get("sandbox") != "workspace-write":
        raise AutoresearchConfigError("Codex app-server sandbox must be workspace-write")
    if app_server.get("defaultWorkspaceDir") != str(DEFAULT_AUTORESEARCH_MODEL_WORKSPACE_ROOT):
        raise AutoresearchConfigError(
            "Codex app-server defaultWorkspaceDir must be "
            f"{DEFAULT_AUTORESEARCH_MODEL_WORKSPACE_ROOT}"
        )
    if app_server.get("networkProxy") is not None:
        raise AutoresearchConfigError(
            "Codex app-server networkProxy must not be configured; pinned Codex 0.144.3 "
            "rejects the plugin-generated :project_roots permissions profile"
        )


def _validate_mempalace_server_split(
    config: Mapping[str, object],
    policy: AutoresearchPolicy,
) -> None:
    try:
        mcp = _ensure_mapping(config.get("mcp"), label="mcp")
        servers = _ensure_mapping(mcp.get("servers"), label="mcp.servers")
        if set(servers) != {MEMPALACE_READONLY_SERVER_ID, G2_CONTROL_SERVER_ID}:
            raise AutoresearchConfigError(
                "mcp.servers must expose exactly mempalace-readonly and g2-control"
            )
        readonly_server = _ensure_mapping(
            servers.get(MEMPALACE_READONLY_SERVER_ID),
            label=f"mcp.servers.{MEMPALACE_READONLY_SERVER_ID}",
        )
        control_server = _ensure_mapping(
            servers.get(G2_CONTROL_SERVER_ID),
            label=f"mcp.servers.{G2_CONTROL_SERVER_ID}",
        )
        _validate_mempalace_server(
            readonly_server,
            server_id=MEMPALACE_READONLY_SERVER_ID,
            expected_agents=(
                policy.main_interface.agent_id,
                policy.pm.agent_id,
                *policy.all_stage_agent_ids,
            ),
            expected_args_prefix=(MEMPALACE_READONLY_WRAPPER_BASENAME, "--palace"),
        )
        _validate_mempalace_server(
            control_server,
            server_id=G2_CONTROL_SERVER_ID,
            expected_agents=(policy.main_interface.agent_id,),
            expected_args_prefix=("-m", G2_CONTROL_MODULE),
        )
    except AutoresearchValidationError as exc:
        raise AutoresearchConfigError(str(exc)) from exc


def _validate_mempalace_server(
    server: Mapping[str, object],
    *,
    server_id: str,
    expected_agents: tuple[str, ...],
    expected_args_prefix: tuple[str, ...],
) -> None:
    _require_str(server, "command")
    args = _require_string_sequence(server.get("args"), label=f"mcp.servers.{server_id}.args")
    codex = _ensure_mapping(server.get("codex"), label=f"mcp.servers.{server_id}.codex")
    agents = _require_string_list(codex, "agents")
    if tuple(agents) != expected_agents:
        raise AutoresearchConfigError(
            f"mcp.servers.{server_id}.codex.agents must exactly match {expected_agents}"
        )
    if server_id == G2_CONTROL_SERVER_ID:
        if tuple(args) != expected_args_prefix:
            raise AutoresearchConfigError(
                "mcp.servers.g2-control.args must be ['-m', 'gateway.g2_control_mcp_server']"
            )
        if codex.get("defaultToolsApprovalMode") != "approve":
            raise AutoresearchConfigError(
                "mcp.servers.g2-control.codex.defaultToolsApprovalMode must be approve"
            )
        return
    if len(args) != 3 or args[1] != "--palace":
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args must be ['<wrapper>', '--palace', '<path>']"
        )
    readonly_entrypoint = args[0].strip()
    if not readonly_entrypoint:
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args[0] must be a wrapper path"
        )
    if readonly_entrypoint != MEMPALACE_CONFIG_PLACEHOLDER and (
        Path(readonly_entrypoint).name != MEMPALACE_READONLY_WRAPPER_BASENAME
    ):
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args[0] must point to "
            f"{MEMPALACE_READONLY_WRAPPER_BASENAME}"
        )
    if not args[2].strip():
        raise AutoresearchConfigError(
            "mcp.servers.mempalace-readonly.args[2] must be a palace path"
        )


def _validate_alpha_universe_chain(
    state: AutoresearchState,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    consensus = state.latest_consensus
    if consensus is None or consensus.status is not ConsensusStatus.MAJORITY:
        return
    if _is_operator_precondition_consensus(consensus):
        return
    if consensus.universe_plan is None:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH majority consensus requires a frozen universe_plan"
        )
    consensus.universe_plan.validate()
    for verification in state.verification_history:
        if verification.universe_verification_receipt is None:
            if verification.status is VerificationStatus.PASS:
                raise AutoresearchValidationError(
                    "ALPHA_RESEARCH PASS cannot omit universe verification receipts"
                )
            continue
        if verification.price_hydration_receipt is None:
            raise AutoresearchValidationError(
                "verification cannot persist a partial universe receipt chain"
            )
        verification.universe_verification_receipt.validate_against_plan(consensus.universe_plan)
        if validation_context is not None:
            validation_context.validate_universe_receipt(verification.universe_verification_receipt)
        verification.price_hydration_receipt.validate_against_universe(
            verification.universe_verification_receipt
        )


def _validate_consensus_history_universe_plans(state: AutoresearchState) -> None:
    """Require a frozen plan for every persisted non-operator majority."""
    for index, consensus in enumerate(state.consensus_history, start=1):
        if consensus.status is ConsensusStatus.MAJORITY and not _is_operator_precondition_consensus(
            consensus
        ):
            if consensus.universe_plan is None:
                if index == len(
                    state.consensus_history
                ) and _is_data_infra_g0_blocked_no_memory_state(state):
                    continue
                raise AutoresearchValidationError(
                    "non-operator majority consensus at history index "
                    f"{index} requires a frozen universe_plan"
                )
            consensus.universe_plan.validate()


def _revalidate_accepted_member_union_manifests(state: AutoresearchState) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    for verification in state.verification_history:
        receipt = verification.universe_verification_receipt
        if verification.status is VerificationStatus.PASS and receipt is not None:
            receipt.member_union_manifest.validate()
            _verify_member_union_manifest(receipt)


def _validate_no_consensus_completion(state: AutoresearchState) -> None:
    decision = state.final_decision
    if decision is None or decision.decision is not FinalDecision.NO_CONSENSUS:
        return
    expected_rounds = (1, 2)
    debate_rounds = tuple(debate.round_number for debate in state.debate_rounds)
    consensus_rounds = tuple(consensus.round_number for consensus in state.consensus_history)
    consensus_statuses = tuple(consensus.status for consensus in state.consensus_history)
    if (
        state.consensus_retry_count != 1
        or debate_rounds != expected_rounds
        or consensus_rounds != expected_rounds
        or consensus_statuses != (ConsensusStatus.NO_CONSENSUS, ConsensusStatus.NO_CONSENSUS)
    ):
        raise AutoresearchValidationError(
            "NO_CONSENSUS final state requires the mandatory second round after one retry"
        )


def _final_decision_requires_memory_write(
    state: AutoresearchState,
    decision: FinalDecisionArtifact,
) -> bool:
    """Return the sole final-decision class eligible for MemPalace retention."""
    latest_verification = state.latest_verification
    return (
        state.mode is ResearchMode.ALPHA_RESEARCH
        and decision.decision in (*KEEP_DECISIONS, FinalDecision.DISCARD)
        and latest_verification is not None
        and latest_verification.status is VerificationStatus.PASS
        and latest_verification.tests_passed
    )


def _validate_final_decision_memory_requirement(
    state: AutoresearchState,
    decision: FinalDecisionArtifact,
) -> None:
    """Fail closed when a PM-selected memory flag disagrees with retention policy."""
    memory_write_required = _final_decision_requires_memory_write(state, decision)
    if decision.memory_write_required is memory_write_required:
        return
    if memory_write_required:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH completed PASS final decisions require memory_write_required=true"
        )
    raise AutoresearchValidationError(
        f"{decision.decision.value} final decision is not eligible for MemPalace retention; "
        "memory_write_required=false"
    )


def _is_authorized_no_memory_final_decision(state: AutoresearchState) -> bool:
    """Return whether a non-retained final decision completed a valid terminal path."""
    decision = state.final_decision
    if (
        decision is None
        or decision.memory_write_required
        or _final_decision_requires_memory_write(state, decision)
    ):
        return False

    latest_verification = state.latest_verification
    if decision.decision is FinalDecision.NO_CONSENSUS:
        return (
            state.implementation_result is None
            and state.consensus_retry_count == 1
            and tuple(debate.round_number for debate in state.debate_rounds) == (1, 2)
            and tuple(consensus.round_number for consensus in state.consensus_history) == (1, 2)
            and tuple(consensus.status for consensus in state.consensus_history)
            == (ConsensusStatus.NO_CONSENSUS, ConsensusStatus.NO_CONSENSUS)
        )

    if decision.decision is FinalDecision.INFRA_BLOCKED:
        return state.suspended and (
            _is_operator_infrastructure_suspension_state(state)
            or (
                _is_operator_precondition_consensus(state.latest_consensus)
                and state.implementation_result is None
                and latest_verification is None
                and decision.reviewer_verdict is FinalReviewerVerdict.NOT_RUN
                and decision.recommended_metric_value is None
                and bool(decision.infra_rationale)
            )
        )

    if (
        decision.decision is FinalDecision.CRASH
        and latest_verification is not None
        and latest_verification.status is VerificationStatus.TEST_FAILURE
        and state.verification_fix_attempts >= 2
    ):
        return True

    if (
        decision.decision is FinalDecision.DISCARD
        and latest_verification is not None
        and latest_verification.status is VerificationStatus.BUG_SIGNAL
        and state.verification_fix_attempts >= 2
    ):
        return True

    if state.mode is not ResearchMode.DATA_INFRA_G0 or latest_verification is None:
        return False
    if (
        latest_verification.status is not VerificationStatus.PASS
        or not latest_verification.tests_passed
    ):
        return False
    if decision.decision is FinalDecision.INFRA_REPAIRED:
        return latest_verification.infra_gate_outcome is InfraGateOutcome.GATE_PASSED
    return (
        decision.decision is FinalDecision.DISCARD
        and latest_verification.infra_gate_outcome is InfraGateOutcome.REMEDIATION_REQUIRED
    )


def _validate_state(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    if validation_context is not None:
        validation_context.validate_for_state(state)
    if state.iteration < 1:
        raise AutoresearchValidationError("iteration must be >= 1")
    if state.suspended:
        decision = state.final_decision
        if state.phase is not Phase.REPEAT or decision is None:
            raise AutoresearchValidationError(
                "suspended autoresearch state must be in repeat phase with a final decision"
            )
        if decision.decision is not FinalDecision.INFRA_BLOCKED:
            raise AutoresearchValidationError(
                "suspended autoresearch state requires final_decision=INFRA_BLOCKED"
            )
        if not state.suspension_reason or not state.suspension_reason.strip():
            raise AutoresearchValidationError(
                "suspended autoresearch state requires suspension_reason"
            )
        if (
            decision.memory_write_required
            or state.memory_written
            or state.memory_verification_receipt is not None
        ):
            raise AutoresearchValidationError(
                "suspended autoresearch state cannot require or record a memory write"
            )
    elif state.suspension_reason is not None:
        raise AutoresearchValidationError("suspension_reason requires suspended=true")
    if state.consensus_retry_count not in (0, 1):
        raise AutoresearchValidationError("consensus_retry_count must be 0 or 1")
    if state.context_packet is not None and state.setup is None:
        raise AutoresearchValidationError("context_packet requires setup first")
    if state.context_packet is not None and state.mode is None:
        raise AutoresearchValidationError("mode must be explicit after a context_packet exists")
    if state.context_packet is not None and state.mode is not state.context_packet.research_mode:
        raise AutoresearchValidationError("state mode must match context_packet research_mode")
    _validate_external_verification_retry_receipt(state, validation_context)
    if state.debate_rounds and state.context_packet is None:
        raise AutoresearchValidationError("debate history requires a context_packet")
    if state.consensus_history and state.latest_debate is None:
        raise AutoresearchValidationError("consensus history requires a debate_result")
    _validate_consensus_history_universe_plans(state)
    if (
        state.suspended
        and state.mode is ResearchMode.DATA_INFRA_G0
        and state.latest_verification is not None
    ):
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 remediation must end in non-suspending DISCARD"
        )
    _validate_alpha_universe_chain(state)
    if state.memory_written and state.final_decision is None:
        raise AutoresearchValidationError("memory_written cannot be true before final_decision")
    if (
        state.memory_written
        and state.final_decision is not None
        and not state.final_decision.memory_write_required
    ):
        raise AutoresearchValidationError(
            "memory_written is invalid when final_decision.memory_write_required=false"
        )
    if state.memory_written and state.memory_verification_receipt is None:
        raise AutoresearchValidationError("memory_written requires a memory_verification_receipt")
    if not state.memory_written and state.memory_verification_receipt is not None:
        raise AutoresearchValidationError(
            "memory_verification_receipt requires memory_written=true"
        )
    if (
        state.memory_verification_receipt is not None
        and state.final_decision is not None
        and state.memory_verification_receipt.experiment_id != state.final_decision.experiment_id
    ):
        raise AutoresearchValidationError("memory receipt experiment_id must match final_decision")
    if state.final_decision is not None:
        decision = state.final_decision
        _validate_final_decision_memory_requirement(state, decision)
        _validate_no_consensus_completion(state)
        _validate_operator_precondition_infra_blocked_suspension(state)
        is_operator_infrastructure_suspension = _is_operator_infrastructure_suspension_state(state)
        if decision.decision is FinalDecision.NO_CONSENSUS:
            if decision.memory_write_required:
                raise AutoresearchValidationError(
                    "NO_CONSENSUS requires final_decision.memory_write_required=false"
                )
            if state.memory_verification_receipt is not None:
                raise AutoresearchValidationError(
                    "NO_CONSENSUS must not have a memory_verification_receipt"
                )
        if not is_operator_infrastructure_suspension:
            _validate_final_decision_artifact(decision, state, validation_context)
        if not decision.memory_write_required and not _is_authorized_no_memory_final_decision(
            state
        ):
            raise AutoresearchValidationError(
                "final_decision.memory_write_required=false requires an authorized "
                "no-memory terminal path"
            )
    if state.implementation_result and (
        state.latest_consensus is None
        or state.latest_consensus.status is not ConsensusStatus.MAJORITY
    ):
        raise AutoresearchValidationError("implementation_result requires a majority consensus")
    if state.implementation_result:
        _validate_persisted_autoresearch_workspace_path(
            state.implementation_result.workspace_path,
            label="implementation_result workspace_path",
        )
        _validate_implementation_workspace(state, state.implementation_result)
    if state.fix_history and state.implementation_result is None:
        raise AutoresearchValidationError("fix history requires an implementation_result")
    for fix in state.fix_history:
        fix.validate()
        _validate_persisted_autoresearch_workspace_path(
            fix.workspace_path,
            label="fix_history workspace_path",
        )
        if state.implementation_result is not None and (
            fix.workspace_path != state.implementation_result.workspace_path
        ):
            raise AutoresearchValidationError(
                "fix_history workspace_path must exactly match implementation_result workspace_path"
            )
    if state.verification_history and state.implementation_result is None:
        raise AutoresearchValidationError("verification history requires an implementation_result")
    if state.review_history and not state.verification_history:
        raise AutoresearchValidationError("review history requires a verification_result")
    if state.pending_fix_trigger is not None and state.phase is not Phase.FIX_TEST:
        raise AutoresearchValidationError("pending_fix_trigger is only valid during fix_test")
    if state.final_decision is not None and state.phase is not Phase.REPEAT:
        raise AutoresearchValidationError("final_decision requires repeat phase")
    for debate in state.debate_rounds:
        _validate_debate_result(debate, policy, mode=state.mode, context=state.context_packet)
    for verification in state.verification_history:
        verification.validate(
            mode=state.mode,
        )
    for review in state.review_history:
        _validate_review_result(review, policy)
    if state.phase is Phase.DEBATE and state.context_packet is None:
        raise AutoresearchValidationError("debate phase requires a context_packet")
    if state.phase is Phase.CONSENSUS and state.latest_debate is None:
        raise AutoresearchValidationError("consensus phase requires a debate_result")
    if state.phase is Phase.IMPLEMENTATION and (
        state.latest_consensus is None
        or state.latest_consensus.status is not ConsensusStatus.MAJORITY
    ):
        raise AutoresearchValidationError("implementation phase requires a majority consensus")
    if state.phase is Phase.IMPLEMENTATION and _is_operator_precondition_consensus(
        state.latest_consensus
    ):
        raise AutoresearchValidationError(
            "operator-precondition consensus must route to decision_log, not implementation"
        )
    if state.phase is Phase.VERIFICATION and state.implementation_result is None:
        raise AutoresearchValidationError("verification phase requires an implementation_result")
    if state.phase is Phase.REVIEW:
        if not state.verification_history:
            raise AutoresearchValidationError("review phase requires a verification_result")
        if (
            state.latest_verification is None
            or state.latest_verification.status is not VerificationStatus.PASS
        ):
            raise AutoresearchValidationError("review phase requires a passing verification_result")
    if state.phase is Phase.FIX_TEST:
        if state.pending_fix_trigger is None:
            raise AutoresearchValidationError("fix_test phase requires pending_fix_trigger")
        if state.pending_fix_trigger is FixTriggerPhase.VERIFICATION and (
            state.latest_verification is None
            or state.latest_verification.status is VerificationStatus.PASS
        ):
            raise AutoresearchValidationError(
                "verification-triggered fix_test requires a failing verification_result"
            )
        if state.pending_fix_trigger is FixTriggerPhase.REVIEW:
            latest_review = state.latest_review
            if latest_review is None or (
                latest_review.verdict is not ReviewVerdict.FAIL
                and not latest_review.critical_issues
            ):
                raise AutoresearchValidationError(
                    "review-triggered fix_test requires a failing review_result"
                )
    if state.phase is Phase.DECISION_LOG and (
        state.latest_consensus is None
        and state.latest_review is None
        and state.latest_verification is None
    ):
        raise AutoresearchValidationError("decision_log phase requires prior artifacts")
    if state.phase is Phase.REPEAT and state.final_decision is None:
        raise AutoresearchValidationError("repeat phase requires final_decision")


def validate_state(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    _validate_state(state, policy, validation_context)


def _validate_debate_result(
    debate: DebateResultArtifact,
    policy: AutoresearchPolicy,
    *,
    mode: ResearchMode | None = None,
    context: ContextPacketArtifact | None = None,
    target_repo: Path | None = None,
    require_compute_fit: bool = False,
) -> None:
    expected_ids = set(policy.debate_agent_ids)
    actual_ids = {submission.agent_id for submission in debate.submissions}
    if actual_ids != expected_ids:
        raise AutoresearchValidationError(
            "debate_result must contain exactly the configured five debate agents"
        )
    if mode is ResearchMode.ALPHA_RESEARCH and context is not None:
        burned = set(context.burned_theory_families)
        for submission in debate.submissions:
            family = _normalise_identifier(submission.theory_family)
            if family in burned and not submission.materially_new_evidence:
                raise AutoresearchValidationError(
                    "alpha debate theory_family is burned and requires materially_new_evidence"
                )
    for submission in debate.submissions:
        if require_compute_fit and submission.compute_fit is None:
            raise AutoresearchValidationError(
                "new debate submissions must include a compute_fit artifact"
            )
        if submission.compute_fit is not None:
            submission.compute_fit.validate()
            if target_repo is not None:
                _validate_compute_fit_environment(submission.compute_fit, target_repo)


def _validate_review_result(review: ReviewResultArtifact, policy: AutoresearchPolicy) -> None:
    if review.reviewer_agent_id != policy.reviewer.agent_id:
        raise AutoresearchValidationError(
            "review_result must come from the single configured reviewer"
        )


def _validate_implementation_workspace(
    state: AutoresearchState,
    artifact: ImplementationResultArtifact,
    *,
    require_compute_fit: bool = False,
) -> None:
    artifact.validate()
    if require_compute_fit and artifact.compute_fit is None:
        raise AutoresearchValidationError(
            "new implementation_result artifacts must include a compute_fit artifact"
        )
    if artifact.compute_fit is not None:
        artifact.compute_fit.validate()
        if state.setup is not None:
            _validate_compute_fit_environment(
                artifact.compute_fit,
                Path(state.setup.target_repo),
            )
    _validate_persisted_autoresearch_workspace_path(
        artifact.workspace_path,
        label="implementation_result workspace_path",
    )
    if state.setup is None:
        return
    workspace_path = Path(artifact.workspace_path).expanduser().resolve()
    target_repo = Path(state.setup.target_repo).expanduser().resolve()
    if workspace_path == target_repo:
        raise AutoresearchValidationError(
            "implementation_result workspace_path must be an isolated worktree, "
            "not the main target_repo"
        )


def _validate_fix_workspace(state: AutoresearchState, artifact: FixResultArtifact) -> None:
    artifact.validate()
    _validate_price_scope_fix_result_commands(state, artifact)
    _validate_persisted_autoresearch_workspace_path(
        artifact.workspace_path,
        label="fix_result workspace_path",
    )
    if state.implementation_result is None:
        raise AutoresearchValidationError("fix_result requires implementation_result")
    _validate_persisted_autoresearch_workspace_path(
        state.implementation_result.workspace_path,
        label="implementation_result workspace_path",
    )
    if artifact.workspace_path != state.implementation_result.workspace_path:
        raise AutoresearchValidationError(
            "fix_result workspace_path must match implementation_result workspace_path"
        )
    candidate_implementation = replace(
        state.implementation_result,
        commit_sha=artifact.commit_sha,
        price_hydration_scope_preflight=(
            artifact.price_hydration_scope_preflight
            if artifact.price_hydration_scope_preflight is not None
            else state.implementation_result.price_hydration_scope_preflight
        ),
    )
    _validate_implementation_workspace(
        state,
        candidate_implementation,
    )


def _run_git(
    working_directory: Path,
    arguments: Sequence[str],
    *,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            check=False,
            capture_output=True,
            cwd=working_directory,
            text=True,
        )
    except (OSError, RuntimeError) as exc:
        raise AutoresearchValidationError(
            f"Git {operation} could not run in {_render_literal(str(working_directory))}"
        ) from exc
    return result


def _require_git_output(
    working_directory: Path,
    arguments: Sequence[str],
    *,
    operation: str,
) -> str:
    result = _run_git(working_directory, arguments, operation=operation)
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"Git {operation} failed in {_render_literal(str(working_directory))}"
        )
    return result.stdout.strip()


def _require_git_worktree_root(path: Path, *, label: str) -> Path:
    if not path.is_dir():
        raise AutoresearchValidationError(f"{label} {_render_literal(str(path))} does not exist")
    resolved_path = path.resolve()
    top_level = Path(
        _require_git_output(
            resolved_path,
            ("rev-parse", "--show-toplevel"),
            operation=f"worktree check for {label}",
        )
    ).resolve()
    if top_level != resolved_path:
        raise AutoresearchValidationError(
            f"{label} {_render_literal(str(path))} must be the root of a Git worktree"
        )
    return resolved_path


def _require_isolated_git_clone_root(path: Path, *, label: str) -> Path:
    root = _require_git_worktree_root(path, label=label)
    git_metadata = root / ".git"
    try:
        metadata = git_metadata.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(
            f"{label} must contain private .git directory metadata"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AutoresearchValidationError(
            f"{label} must be an isolated clone with a private .git directory"
        )
    _require_private_directory(git_metadata, label=f"{label} .git metadata")
    return root


def _require_artifact_origin_matches_target(
    workspace: Path,
    target_checkout: Path,
    *,
    label: str,
) -> None:
    result = _run_git(
        workspace,
        ("config", "--get", "remote.origin.url"),
        operation=f"origin check for {label}",
    )
    origin = result.stdout.strip()
    if result.returncode != 0 or not origin:
        raise AutoresearchValidationError(
            f"Git ancestry check failed in {_render_literal(str(workspace))}"
        )
    parsed = urlparse(origin)
    if parsed.scheme and parsed.scheme != "file":
        raise AutoresearchValidationError(
            f"{label} remote.origin.url must be the authoritative local target_repo"
        )
    origin_path = Path(unquote(parsed.path if parsed.scheme == "file" else origin)).expanduser()
    try:
        resolved_origin = origin_path.resolve(strict=True)
    except OSError as exc:
        raise AutoresearchValidationError(
            f"{label} remote.origin.url does not resolve to authoritative target_repo"
        ) from exc
    if resolved_origin != target_checkout:
        raise AutoresearchValidationError(
            f"Git ancestry check failed in {_render_literal(str(workspace))}"
        )


def _require_strict_canonical_workspace_path(value: str, *, label: str) -> Path:
    declared_path = Path(value).expanduser()
    try:
        resolved_path = declared_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AutoresearchValidationError(
            f"{label} {_render_literal(value)} does not exist or is not a directory"
        ) from exc
    if not resolved_path.is_dir():
        raise AutoresearchValidationError(
            f"{label} {_render_literal(value)} does not exist or is not a directory"
        )
    if value != str(resolved_path):
        raise AutoresearchValidationError(f"{label} must be its strict canonical resolved path")
    return resolved_path


def _require_autoresearch_worktree_root() -> Path:
    root = _require_strict_canonical_workspace_path(
        str(DEFAULT_AUTORESEARCH_WORKTREE_ROOT),
        label="autoresearch worktree root",
    )
    _require_private_directory(root, label="autoresearch worktree root")
    return root


def _path_under_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def state_has_legacy_autoresearch_workspace(state: AutoresearchState) -> bool:
    """Return whether persisted state still points at the retired linked-worktree root."""
    workspaces: list[str] = []
    if state.implementation_result is not None:
        workspaces.append(state.implementation_result.workspace_path)
    workspaces.extend(fix.workspace_path for fix in state.fix_history)
    for value in workspaces:
        try:
            path = Path(value).expanduser().resolve(strict=False)
        except RuntimeError:
            return True
        if _path_under_root(path, LEGACY_AUTORESEARCH_WORKTREE_ROOT):
            return True
    return False


def _require_workspace_under_autoresearch_worktree_root(
    workspace: Path,
    *,
    label: str,
    worktree_root: Path,
) -> None:
    try:
        workspace.relative_to(worktree_root)
    except ValueError as exc:
        raise AutoresearchValidationError(
            f"{label} must be under the canonical autoresearch worktree root"
        ) from exc


def _resolve_git_commit(worktree: Path, commit_sha: str, *, label: str) -> str:
    result = _run_git(
        worktree,
        ("rev-parse", "--verify", f"{commit_sha}^{{commit}}"),
        operation=f"commit lookup for {label}",
    )
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"{label} {_render_literal(commit_sha)} does not exist in the artifact worktree"
        )
    return result.stdout.strip()


def _require_git_descends_from(
    worktree: Path,
    ancestor: str,
    descendant: str,
    *,
    label: str,
) -> None:
    result = _run_git(
        worktree,
        ("merge-base", "--is-ancestor", ancestor, descendant),
        operation=f"readiness ancestry check for {label}",
    )
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"{label} must descend from the exact readiness-pinned Quantipy commit"
        )


def _require_git_success(
    working_directory: Path,
    arguments: Sequence[str],
    *,
    operation: str,
) -> None:
    result = _run_git(working_directory, arguments, operation=operation)
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"Git {operation} failed in {_render_literal(str(working_directory))}"
        )


def _require_clean_git_worktree(worktree: Path) -> None:
    status = _require_git_output(
        worktree,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        operation="status check",
    )
    if status:
        raise AutoresearchValidationError(
            f"artifact worktree {_render_literal(str(worktree))} must be clean"
        )


def _probe_quantipy_runtime_resolution(runtime_root: Path) -> tuple[Path, Path, str]:
    """Resolve the frozen uv executable and Quantipy import without changing the runtime."""
    command = (
        "uv",
        "--directory",
        str(runtime_root),
        "run",
        "--frozen",
        "--no-sync",
        "python",
        "-c",
        (
            "import json, pathlib, quantipy, sys; "
            "print(json.dumps({'base_interpreter': str(pathlib.Path(sys.executable).resolve()), "
            "'base_interpreter_version': '.'.join(map(str, sys.version_info[:3])), "
            "'import_path': str(pathlib.Path(quantipy.__file__).resolve())}, sort_keys=True))"
        ),
    )
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise AutoresearchValidationError(
            "canonical Quantipy runtime uv resolution failed"
        ) from exc
    if result.returncode != 0:
        raise AutoresearchValidationError("canonical Quantipy runtime uv resolution failed")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AutoresearchValidationError(
            "canonical Quantipy runtime resolution did not produce JSON"
        ) from exc
    if not isinstance(raw, Mapping) or set(raw) != {
        "base_interpreter",
        "base_interpreter_version",
        "import_path",
    }:
        raise AutoresearchValidationError("canonical Quantipy runtime resolution is invalid")
    base_interpreter = _require_canonical_absolute_path(
        _require_str(raw, "base_interpreter"), label="canonical Quantipy runtime base interpreter"
    )
    import_path = _require_canonical_absolute_path(
        _require_str(raw, "import_path"), label="canonical Quantipy runtime import"
    )
    version = _require_str(raw, "base_interpreter_version")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise AutoresearchValidationError(
            "canonical Quantipy runtime base interpreter version is invalid"
        )
    return base_interpreter, import_path, version


def _attest_canonical_quantipy_runtime(
    state: AutoresearchState,
    implementation: ImplementationResultArtifact,
    *,
    readiness_quantipy_commit: str | None = None,
) -> CanonicalQuantipyRuntimeAttestation:
    """Fail closed unless uv's project runtime and source bind one pinned commit."""
    if state.setup is None:
        raise AutoresearchValidationError("canonical Quantipy runtime requires setup target_repo")
    runtime_root = _require_git_worktree_root(
        Path(state.setup.target_repo).expanduser(), label="canonical Quantipy runtime root"
    )
    if runtime_root != _target_repo_root_for_state(state):
        raise AutoresearchValidationError(
            "canonical Quantipy runtime root must equal state target_repo"
        )
    status = _require_git_output(
        runtime_root,
        ("status", "--porcelain=v1", "--untracked-files=no"),
        operation="canonical Quantipy runtime tracked-file status check",
    )
    if status:
        raise AutoresearchValidationError("canonical Quantipy runtime has dirty tracked files")
    runtime_commit = _resolve_git_commit(
        runtime_root, "HEAD", label="canonical Quantipy runtime HEAD"
    )
    implementation_workspace = _require_git_worktree_root(
        Path(implementation.workspace_path), label="implementation_result workspace_path"
    )
    implementation_commit = _resolve_git_commit(
        implementation_workspace,
        implementation.commit_sha,
        label="implementation_result commit_sha",
    )
    readiness_commit = (
        readiness_quantipy_commit
        if readiness_quantipy_commit is not None
        else (state.platform_readiness.quantipy_commit if state.platform_readiness else None)
    )
    if readiness_commit is None:
        raise AutoresearchValidationError(
            "canonical Quantipy runtime requires the readiness-pinned Quantipy commit"
        )
    _require_git_descends_from(
        runtime_root, readiness_commit, runtime_commit, label="canonical Quantipy runtime"
    )
    _require_git_descends_from(
        implementation_workspace,
        readiness_commit,
        implementation_commit,
        label="implementation_result commit_sha",
    )
    snapshots: dict[str, _SecureFileSnapshot] = {}
    for filename, max_bytes in (
        ("pyproject.toml", CANONICAL_QUANTIPY_PYPROJECT_MAX_BYTES),
        ("uv.lock", CANONICAL_QUANTIPY_UV_LOCK_MAX_BYTES),
    ):
        snapshot = _secure_open_snapshot(
            runtime_root / filename,
            label=f"canonical Quantipy runtime {filename}",
            allow_group_write=True,
            max_bytes=max_bytes,
        )
        if snapshot.content != _git_show_committed_bytes(
            runtime_root, runtime_commit, Path(filename)
        ):
            raise AutoresearchValidationError(
                f"canonical Quantipy runtime {filename} must match runtime commit exactly"
            )
        snapshots[filename] = snapshot
    venv_prefix = runtime_root / ".venv"
    _require_runtime_venv_prefix(venv_prefix)
    entrypoint = _secure_open_snapshot(
        venv_prefix / "bin" / "quantipy",
        label="canonical Quantipy runtime .venv quantipy entrypoint",
        allow_group_write=True,
        max_bytes=CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES,
    )
    base_interpreter, import_path, base_interpreter_version = _probe_quantipy_runtime_resolution(
        runtime_root
    )
    import_package = import_path.parent
    if import_package != runtime_root / "src" / "quantipy":
        raise AutoresearchValidationError(
            "canonical Quantipy runtime import must resolve from root/src/quantipy"
        )
    base_snapshot = _secure_open_external_uv_base_interpreter(base_interpreter)
    return CanonicalQuantipyRuntimeAttestation(
        root=str(runtime_root),
        commit_sha=runtime_commit,
        readiness_quantipy_commit=readiness_commit,
        pyproject_sha256=snapshots["pyproject.toml"].sha256,
        uv_lock_sha256=snapshots["uv.lock"].sha256,
        venv_prefix=str(venv_prefix),
        executable_path=str(entrypoint.path),
        executable_sha256=entrypoint.sha256,
        executable_size_bytes=len(entrypoint.content),
        executable_mode=entrypoint.mode,
        executable_owner_uid=entrypoint.owner_uid,
        import_path=str(import_package),
        base_interpreter_path=str(base_snapshot.path),
        base_interpreter_version=base_interpreter_version,
        base_interpreter_sha256=base_snapshot.sha256,
        base_interpreter_size_bytes=len(base_snapshot.content),
        base_interpreter_mode=base_snapshot.mode,
        base_interpreter_owner_uid=base_snapshot.owner_uid,
    )


def _require_canonical_verification_runtime_attestation(
    state: AutoresearchState,
    *,
    validation_context: AutoresearchValidationContext | None,
) -> None:
    """Reattest the runtime sealed for the currently dispatched verification."""
    if state.phase is not Phase.VERIFICATION:
        return
    implementation = state.implementation_result
    attestation = state.canonical_quantipy_runtime_attestation
    if implementation is None or attestation is None:
        raise AutoresearchValidationError(
            "canonical verification dispatch requires a sealed runtime attestation"
        )
    if validation_context is None or validation_context.quantipy_commit is None:
        raise AutoresearchValidationError(
            "canonical verification runtime attestation requires the readiness-pinned "
            "Quantipy commit"
        )
    current = _attest_canonical_quantipy_runtime(
        state,
        implementation,
        readiness_quantipy_commit=validation_context.quantipy_commit,
    )
    if (
        current != attestation
        or current.readiness_quantipy_commit != validation_context.quantipy_commit
    ):
        raise AutoresearchValidationError(
            "canonical verification runtime attestation changed after dispatch"
        )


def seal_canonical_verification_dispatch_state_file(
    state_path: Path,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState:
    """Atomically seal the runtime identity before dispatching a verification agent."""
    resolved_path = state_path.expanduser().resolve(strict=False)
    with _exclusive_state_locks((resolved_path,)):
        state = load_state_file(resolved_path)
        _validate_state(state, policy, validation_context)
        if state.phase is not Phase.VERIFICATION or state.implementation_result is None:
            raise AutoresearchValidationError(
                "canonical verification runtime sealing requires a verification implementation"
            )
        if validation_context is None or validation_context.quantipy_commit is None:
            raise AutoresearchValidationError(
                "canonical verification runtime sealing requires the readiness-pinned "
                "Quantipy commit"
            )
        validate_artifact_workspace(state, state.implementation_result)
        attestation = _attest_canonical_quantipy_runtime(
            state,
            state.implementation_result,
            readiness_quantipy_commit=validation_context.quantipy_commit,
        )
        sealed = replace(state, canonical_quantipy_runtime_attestation=attestation)
        _require_canonical_verification_runtime_attestation(
            sealed,
            validation_context=validation_context,
        )
        _atomic_save_state_file(resolved_path, sealed)
        return sealed


def require_canonical_verification_dispatch_attestation(
    state_path: Path,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
    expected_state_reference_sha256: str | None = None,
) -> AutoresearchState:
    """Read-only production guard for a verification dispatch or result publication."""
    resolved_path = state_path.expanduser().resolve(strict=False)
    with _exclusive_state_locks((resolved_path,)):
        state = load_state_file(resolved_path)
        _validate_state(state, policy, validation_context)
        _require_canonical_verification_runtime_attestation(
            state,
            validation_context=validation_context,
        )
        if state.implementation_result is not None:
            validate_artifact_workspace(state, state.implementation_result)
        if expected_state_reference_sha256 is not None:
            _validate_sha256(
                expected_state_reference_sha256,
                label="expected_state_reference_sha256",
            )
            current_reference = build_authoritative_state_reference(
                state,
                state_path=resolved_path,
            ).sha256()
            if current_reference != expected_state_reference_sha256:
                raise AutoresearchValidationError(
                    "canonical verification state reference changed before dispatch"
                )
        return state


@dataclass(frozen=True, slots=True)
class _SecureFileSnapshot:
    path: Path
    content: bytes
    sha256: str
    mode: int
    owner_uid: int


def _secure_open_external_uv_base_interpreter(path: Path) -> _SecureFileSnapshot:
    """Snapshot uv's external owner-controlled base interpreter exactly once."""
    return _secure_open_snapshot(
        path,
        label="canonical Quantipy runtime external uv base interpreter",
        allow_group_write=True,
        max_bytes=CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES,
    )


def _require_canonical_absolute_path(value: str | Path, *, label: str) -> Path:
    path = Path(value)
    declared = os.fspath(value)
    if not path.is_absolute() or declared != path.as_posix() or str(path) != path.as_posix():
        raise AutoresearchValidationError(f"{label} must be a canonical absolute path")
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise AutoresearchValidationError(f"{label} must be a canonical absolute path")
    return path


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _secure_open_snapshot(
    value: str | Path,
    *,
    label: str,
    trusted_root: Path | None = None,
    private: bool = False,
    allow_group_write: bool = False,
    max_bytes: int = MAX_ARTIFACT_FILE_BYTES,
) -> _SecureFileSnapshot:
    """Open without following links, then hash and parse the same immutable byte snapshot."""
    path = _require_canonical_absolute_path(value, label=label)
    if trusted_root is not None:
        root = _require_canonical_absolute_path(trusted_root, label="trusted Quantipy runs root")
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise AutoresearchValidationError(
                f"{label} must be under the trusted runs root"
            ) from exc
        _require_private_directory(root, label="trusted Quantipy runs root")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open("/", directory_flags)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(path.name, file_flags, dir_fd=directory_fd)
    except OSError as exc:
        raise AutoresearchValidationError(
            f"{label} must be an existing canonical non-symlink regular file"
        ) from exc
    finally:
        os.close(directory_fd)

    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise AutoresearchValidationError(f"{label} must be a regular file")
        if before.st_uid != os.getuid():
            raise AutoresearchValidationError(f"{label} must be owned by the autoresearch user")
        prohibited_write_bits = 0o002 if allow_group_write else 0o022
        if stat.S_IMODE(before.st_mode) & prohibited_write_bits:
            restriction = "world-writable" if allow_group_write else "group- or world-writable"
            raise AutoresearchValidationError(f"{label} must not be {restriction}")
        if private and (stat.S_IMODE(before.st_mode) & 0o077):
            raise AutoresearchValidationError(f"{label} must not grant group or other access")
        if private and before.st_nlink != 1:
            raise AutoresearchValidationError(f"{label} must not be hard-linked")
        if before.st_size > max_bytes:
            raise AutoresearchValidationError(f"{label} exceeds the byte limit")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(file_fd)
        if len(content) > max_bytes:
            raise AutoresearchValidationError(f"{label} exceeds the byte limit")
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise AutoresearchValidationError(f"{label} changed while it was being read")
    finally:
        os.close(file_fd)
    return _SecureFileSnapshot(
        path=path,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        mode=stat.S_IMODE(before.st_mode),
        owner_uid=before.st_uid,
    )


def _require_runtime_venv_prefix(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(
            "canonical Quantipy runtime .venv prefix does not exist"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o002
    ):
        raise AutoresearchValidationError(
            "canonical Quantipy runtime .venv prefix must be owned and not world-writable"
        )


def _require_private_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(f"{label} does not exist") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AutoresearchValidationError(
            f"{label} must be an owned mode-0700 non-symlink directory"
        )


def _require_sealed_quantipy_panel_directory(path: Path) -> None:
    """Require the exact read-only directory mode emitted for completed panels."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AutoresearchValidationError("Quantipy panel directory does not exist") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o500
    ):
        raise AutoresearchValidationError(
            "Quantipy panel directory must be an owned mode-0500 non-symlink directory"
        )


def _require_sealed_quantipy_panel_file(snapshot: _SecureFileSnapshot, *, label: str) -> None:
    """Require the exact file mode emitted with completed panel evidence."""
    if snapshot.owner_uid != os.getuid() or snapshot.mode != 0o400:
        raise AutoresearchValidationError(f"{label} must be an owned mode-0400 sealed file")


def _open_no_follow_directory(path: Path, *, label: str) -> int:
    """Open an existing canonical directory without traversing symlinks."""
    canonical_path = _require_canonical_absolute_path(path, label=label)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(canonical_path.anchor, flags)
    except OSError as exc:
        raise AutoresearchValidationError(
            f"{label} must be an existing canonical non-symlink directory"
        ) from exc
    try:
        for component in canonical_path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as exc:
        os.close(descriptor)
        raise AutoresearchValidationError(
            f"{label} must be an existing canonical non-symlink directory"
        ) from exc
    return descriptor


def _create_or_normalize_private_directory(
    parent_descriptor: int,
    *,
    name: str,
    label: str,
) -> int:
    """Create or normalize one direct user-owned private directory by descriptor."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(f"{label} could not be provisioned") from exc
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise AutoresearchValidationError(
            f"{label} must be an owned non-symlink directory"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise AutoresearchValidationError(f"{label} must be an owned non-symlink directory")
        os.fchmod(descriptor, 0o700)
        secured = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(secured.st_mode)
            or secured.st_uid != os.getuid()
            or stat.S_IMODE(secured.st_mode) != 0o700
        ):
            raise AutoresearchValidationError(
                f"{label} must be an owned mode-0700 non-symlink directory"
            )
    except AutoresearchValidationError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise AutoresearchValidationError(f"{label} could not be secured") from exc
    return descriptor


def _provision_private_quantipy_control_plane_ancestors(root: Path) -> int:
    """Provision the fixed private control-plane ancestors for the runs root."""
    control_plane_root = root.parent.parent
    runs_parent = root.parent
    base_descriptor = _open_no_follow_directory(
        control_plane_root.parent,
        label="trusted Quantipy control-plane base",
    )
    try:
        control_plane_descriptor = _create_or_normalize_private_directory(
            base_descriptor,
            name=control_plane_root.name,
            label="trusted Quantipy control-plane root",
        )
    finally:
        os.close(base_descriptor)
    try:
        return _create_or_normalize_private_directory(
            control_plane_descriptor,
            name=runs_parent.name,
            label="trusted Quantipy runs parent",
        )
    finally:
        os.close(control_plane_descriptor)


def provision_quantipy_experiment_runs_root() -> Path:
    """Create or validate the one fixed private Quantipy experiment receipt root."""
    root = _require_canonical_absolute_path(
        DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        label="trusted Quantipy runs root",
    )
    parent_descriptor = _provision_private_quantipy_control_plane_ancestors(root)
    try:
        try:
            os.mkdir(root.name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise AutoresearchValidationError(
                "trusted Quantipy runs root could not be provisioned"
            ) from exc
        try:
            descriptor = os.open(
                root.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise AutoresearchValidationError(
                "trusted Quantipy runs root must be an owned mode-0700 non-symlink directory"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise AutoresearchValidationError(
                    "trusted Quantipy runs root must be an owned mode-0700 non-symlink directory"
                )
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
    return root


def _require_strict_regular_file(value: str, *, label: str) -> Path:
    return _secure_open_snapshot(value, label=label).path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_mapping(path: Path, *, label: str) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutoresearchValidationError(f"{label} must be readable JSON") from exc
    return _ensure_mapping(raw, label=label)


def _parse_json_snapshot(snapshot: _SecureFileSnapshot, *, label: str) -> Mapping[str, object]:
    try:
        decoded = snapshot.content.decode("utf-8")
        raw = json.loads(
            decoded,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise AutoresearchValidationError(f"{label} must be strict UTF-8 JSON") from exc
    return _ensure_mapping(raw, label=label)


def _strict_json_keys(
    value: object,
    *,
    label: str,
    expected: Sequence[str],
) -> Mapping[str, object]:
    data = _ensure_mapping(value, label=label)
    _require_exact_keys(data, label=label, expected=expected)
    return data


def _strict_json_string(
    value: object,
    *,
    label: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or len(value) < minimum
        or (maximum is not None and len(value) > maximum)
    ):
        bound = f" between {minimum} and {maximum}" if maximum is not None else f" >= {minimum}"
        raise AutoresearchValidationError(f"{label} must be a string of length{bound}")
    return value


def _strict_json_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise AutoresearchValidationError(f"{label} must be a boolean")
    return value


def _strict_json_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AutoresearchValidationError(f"{label} must be an integer >= {minimum}")
    return value


def _strict_json_float(value: object, *, label: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AutoresearchValidationError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise AutoresearchValidationError(f"{label} must be a finite number >= {minimum}")
    return result


def _strict_json_enum(value: object, *, label: str, allowed: frozenset[str]) -> str:
    result = _strict_json_string(value, label=label)
    if result not in allowed:
        raise AutoresearchValidationError(f"{label} is not an allowed value")
    return result


def _strict_json_sha256(value: object, *, label: str) -> str:
    result = _strict_json_string(value, label=label)
    if re.fullmatch(r"[0-9a-f]{64}", result) is None:
        raise AutoresearchValidationError(f"{label} must be a lowercase SHA-256 digest")
    return result


def _strict_json_datetime(value: object, *, label: str, utc_only: bool = False) -> datetime:
    raw = _strict_json_string(value, label=label)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AutoresearchValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutoresearchValidationError(f"{label} must be timezone-aware")
    if utc_only and parsed.utcoffset() != UTC.utcoffset(parsed):
        raise AutoresearchValidationError(f"{label} must be UTC-aware")
    return parsed


def _strict_json_date(value: object, *, label: str) -> date:
    raw = _strict_json_string(value, label=label)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise AutoresearchValidationError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise AutoresearchValidationError(f"{label} must use canonical ISO date spelling")
    return parsed


def _canonical_json_sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_utc_text(value: object, *, label: str) -> str:
    parsed = _strict_json_datetime(value, label=label, utc_only=True)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_quantipy_relative_path(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > QUANTIPY_EXPERIMENT_SOURCE_PATH_MAX_LENGTH
        or "\\" in value
    ):
        raise AutoresearchValidationError(f"{label} must be a canonical portable relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AutoresearchValidationError(f"{label} must be a canonical portable relative path")
    if value != path.as_posix():
        raise AutoresearchValidationError(f"{label} must be a canonical portable relative path")
    return value


def _validate_panel_request(value: object, *, label: str) -> dict[str, object]:
    data = _strict_json_keys(
        value,
        label=label,
        expected=("contract_version", "tickers", "start", "end", "timeframe", "market_hours"),
    )
    if data["contract_version"] != "research-price-panel-v1":
        raise AutoresearchValidationError(f"{label}.contract_version is invalid")
    tickers_raw = data["tickers"]
    if not isinstance(tickers_raw, list) or not tickers_raw:
        raise AutoresearchValidationError(f"{label}.tickers must be a non-empty JSON array")
    tickers = tuple(_strict_json_string(item, label=f"{label}.tickers") for item in tickers_raw)
    if any(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker) is None for ticker in tickers):
        raise AutoresearchValidationError(f"{label}.tickers contains a noncanonical ticker")
    if tickers != tuple(sorted(tickers)) or len(set(tickers)) != len(tickers):
        raise AutoresearchValidationError(f"{label}.tickers must be unique and sorted")
    start = _strict_json_datetime(data["start"], label=f"{label}.start", utc_only=True)
    end = _strict_json_datetime(data["end"], label=f"{label}.end", utc_only=True)
    if start > end:
        raise AutoresearchValidationError(f"{label} start must not be after end")
    timeframe = _strict_json_enum(
        data["timeframe"],
        label=f"{label}.timeframe",
        allowed=frozenset(("1min", "5min", "15min", "30min", "1h", "4h", "1d")),
    )
    market_hours = _strict_json_enum(
        data["market_hours"],
        label=f"{label}.market_hours",
        allowed=frozenset(("all", "regular", "extended")),
    )
    return {
        "contract_version": "research-price-panel-v1",
        "tickers": list(tickers),
        "start": _canonical_utc_text(data["start"], label=f"{label}.start"),
        "end": _canonical_utc_text(data["end"], label=f"{label}.end"),
        "timeframe": timeframe,
        "market_hours": market_hours,
    }


def _validate_panel_receipt(value: object, *, label: str) -> dict[str, object]:
    try:
        return validate_research_panel_receipt(value, label=label)
    except PanelReceiptValidationError as exc:
        raise AutoresearchValidationError(str(exc)) from exc


def _validate_quantipy_v2_manifest(
    manifest_snapshot: _SecureFileSnapshot,
    *,
    workspace: Path,
    commit_sha: str,
    expected_sha256: str,
) -> Mapping[str, object]:
    manifest_path = manifest_snapshot.path
    try:
        relative_manifest = manifest_path.relative_to(workspace)
    except ValueError as exc:
        raise AutoresearchValidationError(
            "implementation_result experiment_manifest_path must be under its workspace"
        ) from exc
    if manifest_snapshot.sha256 != expected_sha256:
        raise AutoresearchValidationError(
            "implementation_result experiment_manifest_sha256 does not match its file"
        )
    committed = _run_git(
        workspace,
        ("show", f"{commit_sha}:{relative_manifest.as_posix()}"),
        operation="experiment manifest commit check",
    )
    if committed.returncode != 0 or committed.stdout.encode("utf-8") != manifest_snapshot.content:
        raise AutoresearchValidationError(
            "implementation_result experiment manifest must be committed unchanged at commit_sha"
        )
    raw_manifest = _parse_json_snapshot(manifest_snapshot, label="Quantipy experiment manifest")
    allowed = frozenset(
        ("schema_version", "experiment_id", "package_path", "notebook_path", "stage_files", "panel")
    )
    required = frozenset(("schema_version", "experiment_id", "package_path", "stage_files"))
    if set(raw_manifest) - allowed or not required <= set(raw_manifest):
        raise AutoresearchValidationError("Quantipy experiment manifest is not the exact v2 shape")
    manifest = dict(raw_manifest)
    if "notebook_path" not in manifest:
        manifest["notebook_path"] = None
    if "panel" not in manifest:
        manifest["panel"] = None
    if manifest.get("schema_version") != QUANTIPY_EXPERIMENT_SCHEMA_VERSION:
        raise AutoresearchValidationError(
            "Quantipy experiment manifest must use quantipy-experiment-v2"
        )
    experiment_id = manifest.get("experiment_id")
    if (
        not isinstance(experiment_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", experiment_id) is None
    ):
        raise AutoresearchValidationError("Quantipy experiment manifest experiment_id is invalid")
    _validate_quantipy_relative_path(manifest.get("package_path"), label="manifest package_path")
    notebook_path = manifest.get("notebook_path")
    if notebook_path is not None:
        _validate_quantipy_relative_path(notebook_path, label="manifest notebook_path")
    stages = manifest.get("stage_files")
    if not isinstance(stages, list) or len(stages) != 4:
        raise AutoresearchValidationError("Quantipy experiment manifest requires four stage_files")
    stage_names: list[str] = []
    stage_paths: list[str] = []
    stage_entrypoints: list[str] = []
    for stage in stages:
        stage_data = _ensure_mapping(stage, label="manifest stage_file")
        _require_exact_keys(
            stage_data,
            label="manifest stage_file",
            expected=("name", "file_path", "entrypoint"),
        )
        name = _require_str(stage_data, "name")
        stage_names.append(name)
        stage_path = _validate_quantipy_relative_path(
            stage_data.get("file_path"), label="manifest stage_file"
        )
        stage_paths.append(stage_path)
        entrypoint = _strict_json_string(
            stage_data["entrypoint"],
            label="manifest stage_file entrypoint",
            maximum=QUANTIPY_EXPERIMENT_SOURCE_PATH_MAX_LENGTH,
        )
        stage_entrypoints.append(entrypoint)
        if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", entrypoint) is None:
            raise AutoresearchValidationError(
                "Quantipy experiment manifest stage entrypoint is invalid"
            )
    if tuple(stage_names) != QUANTIPY_EXPERIMENT_STAGE_ORDER:
        raise AutoresearchValidationError(
            "Quantipy experiment manifest stage_files must be prepare, smoke, feasibility, model"
        )
    if len(set(stage_paths)) != 4 or len(set(stage_entrypoints)) != 4:
        raise AutoresearchValidationError(
            "Quantipy experiment manifest requires unique stage files and entrypoints"
        )
    panel = manifest["panel"]
    if panel is not None:
        panel_data = _strict_json_keys(
            panel, label="manifest panel", expected=("api_url", "request")
        )
        api_url = _strict_json_string(
            panel_data["api_url"],
            label="manifest panel api_url",
            maximum=2048,
        )
        if re.fullmatch(r"https?://[^/]+(?:/.*)?", api_url) is None:
            raise AutoresearchValidationError("manifest panel api_url is invalid")
        manifest["panel"] = {
            "api_url": api_url,
            "request": _validate_panel_request(
                panel_data["request"], label="manifest panel request"
            ),
        }
    _validate_quantipy_committed_sources(
        manifest,
        manifest_path=manifest_path,
        workspace=workspace,
        commit_sha=commit_sha,
    )
    return manifest


def _git_show_committed_bytes(workspace: Path, commit_sha: str, relative_path: Path) -> bytes:
    try:
        result = subprocess.run(
            ("git", "-C", str(workspace), "show", f"{commit_sha}:{relative_path.as_posix()}"),
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise AutoresearchValidationError("Git is unavailable for source binding") from exc
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"experiment source {relative_path.as_posix()} is not committed at commit_sha"
        )
    return result.stdout


def _validate_quantipy_committed_sources(
    manifest: Mapping[str, object],
    *,
    manifest_path: Path,
    workspace: Path,
    commit_sha: str,
) -> None:
    source_root = manifest_path.parent
    package_relative = Path(
        _validate_quantipy_relative_path(manifest["package_path"], label="manifest package_path")
    )
    package_root = source_root / package_relative
    try:
        package_workspace_relative = package_root.relative_to(workspace)
    except ValueError as exc:
        raise AutoresearchValidationError(
            "manifest package_path must remain under the implementation workspace"
        ) from exc
    committed_paths = _git_tree_file_paths(
        workspace,
        commit_sha=commit_sha,
        package_path=package_workspace_relative,
    )
    actual_paths = _secure_package_file_paths(package_root, workspace=workspace)
    if actual_paths != committed_paths:
        extra = sorted(path.as_posix() for path in actual_paths - committed_paths)
        missing = sorted(path.as_posix() for path in committed_paths - actual_paths)
        if extra:
            raise AutoresearchValidationError(
                f"experiment package contains an untracked or ignored package file: {extra[0]}"
            )
        raise AutoresearchValidationError(
            f"experiment package is missing committed source: {missing[0]}"
        )
    stages = manifest["stage_files"]
    assert isinstance(stages, list)
    for stage_raw in stages:
        stage = _ensure_mapping(stage_raw, label="manifest stage_file")
        declared_stage = (
            package_root
            / _validate_quantipy_relative_path(stage["file_path"], label="manifest stage_file")
        ).relative_to(workspace)
        if declared_stage not in actual_paths:
            raise AutoresearchValidationError(
                f"declared experiment stage is not a committed package file: "
                f"{declared_stage.as_posix()}"
            )
    for relative_path in sorted(actual_paths):
        snapshot = _secure_open_snapshot(
            workspace / relative_path,
            label=f"experiment package source {relative_path.as_posix()}",
            max_bytes=QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_BYTES,
        )
        if snapshot.content != _git_show_committed_bytes(workspace, commit_sha, relative_path):
            raise AutoresearchValidationError(
                f"experiment package source {relative_path.as_posix()} "
                "must match commit_sha exactly"
            )

    source_paths: list[Path] = []
    notebook = manifest["notebook_path"]
    if notebook is not None:
        source_paths.append(
            source_root / _validate_quantipy_relative_path(notebook, label="manifest notebook_path")
        )
    for source_path in source_paths:
        try:
            relative_path = source_path.relative_to(workspace)
        except ValueError as exc:
            raise AutoresearchValidationError(
                "manifest notebook_path must remain under the implementation workspace"
            ) from exc
        snapshot = _secure_open_snapshot(
            source_path,
            label=f"experiment notebook {relative_path.as_posix()}",
            max_bytes=QUANTIPY_EXPERIMENT_NOTEBOOK_MAX_BYTES,
        )
        if snapshot.content != _git_show_committed_bytes(workspace, commit_sha, relative_path):
            raise AutoresearchValidationError(
                f"experiment source {relative_path.as_posix()} must match commit_sha exactly"
            )


def _secure_package_file_paths(package_root: Path, *, workspace: Path) -> frozenset[Path]:
    pending = [package_root]
    files: set[Path] = set()
    while pending:
        directory = pending.pop()
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise AutoresearchValidationError(
                "experiment package directory is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise AutoresearchValidationError(
                "experiment package directories must be owned, non-symlink, and non-writable "
                "by group or other"
            )
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise AutoresearchValidationError("experiment package cannot be enumerated") from exc
        for entry in entries:
            path = Path(entry.path)
            package_relative = path.relative_to(package_root)
            try:
                entry_metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise AutoresearchValidationError(
                    "experiment package entry changed during enumeration"
                ) from exc
            if stat.S_ISDIR(entry_metadata.st_mode):
                if entry.name == "__pycache__":
                    continue
                pending.append(path)
            elif stat.S_ISREG(entry_metadata.st_mode):
                if not _is_quantipy_package_source_path(package_relative):
                    continue
                try:
                    files.add(path.relative_to(workspace))
                except ValueError as exc:
                    raise AutoresearchValidationError(
                        "experiment package entry escaped the workspace"
                    ) from exc
            else:
                raise AutoresearchValidationError(
                    "experiment package may contain only regular files and directories"
                )
    return frozenset(files)


def _is_quantipy_package_source_path(path: Path) -> bool:
    return "__pycache__" not in path.parts and path.suffix in {".py", ".pyi"}


def _git_tree_file_paths(
    workspace: Path,
    *,
    commit_sha: str,
    package_path: Path,
) -> frozenset[Path]:
    try:
        result = subprocess.run(
            (
                "git",
                "-C",
                str(workspace),
                "ls-tree",
                "-rz",
                "--name-only",
                commit_sha,
                "--",
                package_path.as_posix(),
            ),
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise AutoresearchValidationError("Git is unavailable for package provenance") from exc
    if result.returncode != 0:
        raise AutoresearchValidationError("experiment package commit tree lookup failed")
    committed_paths: set[Path] = set()
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        workspace_relative = Path(os.fsdecode(raw_path))
        try:
            package_relative = workspace_relative.relative_to(package_path)
        except ValueError as exc:
            raise AutoresearchValidationError(
                "experiment package commit tree lookup escaped the package root"
            ) from exc
        if _is_quantipy_package_source_path(package_relative):
            committed_paths.add(workspace_relative)
    return frozenset(committed_paths)


def _canonical_quantipy_manifest_sha256(manifest: Mapping[str, object]) -> str:
    return _sha256_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


def _validate_quantipy_failure(value: object, *, label: str) -> dict[str, str]:
    data = _strict_json_keys(value, label=label, expected=("category", "message"))
    return {
        "category": _strict_json_enum(
            data["category"],
            label=f"{label}.category",
            allowed=QUANTIPY_EXPERIMENT_FAILURE_CATEGORIES,
        ),
        "message": _strict_json_string(
            data["message"],
            label=f"{label}.message",
            minimum=1,
            maximum=QUANTIPY_EXPERIMENT_FAILURE_MESSAGE_MAX_LENGTH,
        ),
    }


def _quantipy_experiment_source_digest(files: Sequence[Mapping[str, object]]) -> str:
    payload = {
        "domain": QUANTIPY_EXPERIMENT_SOURCE_DIGEST_DOMAIN,
        "files": [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
            }
            for item in sorted(files, key=lambda item: str(item["path"]))
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_quantipy_run_source(value: object, *, label: str) -> dict[str, object]:
    data = _strict_json_keys(
        value,
        label=label,
        expected=("algorithm", "domain", "files", "total_bytes", "sha256"),
    )
    if data["algorithm"] != "sha256":
        raise AutoresearchValidationError(f"{label}.algorithm is invalid")
    if data["domain"] != QUANTIPY_EXPERIMENT_SOURCE_DIGEST_DOMAIN:
        raise AutoresearchValidationError(f"{label}.domain is invalid")
    files_raw = data["files"]
    if (
        not isinstance(files_raw, list)
        or not 1 <= len(files_raw) <= QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_COUNT
    ):
        raise AutoresearchValidationError(
            f"{label}.files must contain 1 to "
            f"{QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_COUNT} source files"
        )
    files: list[dict[str, object]] = []
    for index, raw_file in enumerate(files_raw):
        file_label = f"{label}.files[{index}]"
        source_file = _strict_json_keys(
            raw_file,
            label=file_label,
            expected=("path", "sha256", "size_bytes"),
        )
        path = _validate_quantipy_relative_path(
            source_file["path"],
            label=f"{file_label}.path",
        )
        if Path(path).suffix != ".py":
            raise AutoresearchValidationError(f"{file_label}.path must name Python source")
        size_bytes = _strict_json_int(
            source_file["size_bytes"],
            label=f"{file_label}.size_bytes",
        )
        if size_bytes > QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_BYTES:
            raise AutoresearchValidationError(f"{file_label}.size_bytes exceeds its limit")
        files.append(
            {
                "path": path,
                "sha256": _strict_json_sha256(
                    source_file["sha256"],
                    label=f"{file_label}.sha256",
                ),
                "size_bytes": size_bytes,
            }
        )
    paths = tuple(str(item["path"]) for item in files)
    if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
        raise AutoresearchValidationError(f"{label}.files must be uniquely ordered by path")
    total_bytes = _strict_json_int(data["total_bytes"], label=f"{label}.total_bytes")
    if total_bytes > QUANTIPY_EXPERIMENT_SOURCE_TOTAL_MAX_BYTES or total_bytes != sum(
        cast(int, item["size_bytes"]) for item in files
    ):
        raise AutoresearchValidationError(
            f"{label}.total_bytes does not bind its bounded file inventory"
        )
    digest = _strict_json_sha256(data["sha256"], label=f"{label}.sha256")
    if digest != _quantipy_experiment_source_digest(files):
        raise AutoresearchValidationError(
            f"{label}.sha256 does not match its canonical source inventory"
        )
    return {
        "algorithm": "sha256",
        "domain": QUANTIPY_EXPERIMENT_SOURCE_DIGEST_DOMAIN,
        "files": files,
        "total_bytes": total_bytes,
        "sha256": digest,
    }


def _validate_quantipy_execution_source_against_commit(
    source: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    source_root: Path,
    workspace: Path,
    commit_sha: str,
) -> None:
    files_raw = source["files"]
    assert isinstance(files_raw, list)
    package_relative = Path(str(manifest["package_path"]))
    package_root = source_root / package_relative
    try:
        source_root_relative = source_root.relative_to(workspace)
        package_workspace_relative = package_root.relative_to(workspace)
    except ValueError as exc:
        raise AutoresearchValidationError(
            "Quantipy run source evidence escaped the implementation workspace"
        ) from exc
    committed_source_paths = sorted(
        path
        for path in _git_tree_file_paths(
            workspace,
            commit_sha=commit_sha,
            package_path=package_workspace_relative,
        )
        if path.suffix == ".py"
    )
    expected_files: list[dict[str, object]] = []
    for workspace_relative in committed_source_paths:
        committed_bytes = _git_show_committed_bytes(
            workspace,
            commit_sha,
            workspace_relative,
        )
        try:
            source_relative = workspace_relative.relative_to(source_root_relative)
            source_relative.relative_to(package_relative)
        except ValueError as exc:
            raise AutoresearchValidationError(
                "committed Quantipy package source escaped the manifest source root"
            ) from exc
        expected_files.append(
            {
                "path": source_relative.as_posix(),
                "sha256": hashlib.sha256(committed_bytes).hexdigest(),
                "size_bytes": len(committed_bytes),
            }
        )
    expected_source: dict[str, object] = {
        "algorithm": "sha256",
        "domain": QUANTIPY_EXPERIMENT_SOURCE_DIGEST_DOMAIN,
        "files": expected_files,
        "total_bytes": sum(cast(int, item["size_bytes"]) for item in expected_files),
        "sha256": _quantipy_experiment_source_digest(expected_files),
    }
    if dict(source) != expected_source:
        reported_by_path = {
            str(_ensure_mapping(item, label="Quantipy run.json source file")["path"]): item
            for item in files_raw
        }
        expected_by_path = {str(item["path"]): item for item in expected_files}
        if reported_by_path.keys() == expected_by_path.keys():
            mismatched_path = next(
                path
                for path in sorted(expected_by_path)
                if reported_by_path[path] != expected_by_path[path]
            )
            raise AutoresearchValidationError(
                "Quantipy execution-time source evidence does not match implementation commit: "
                f"{mismatched_path}"
            )
        raise AutoresearchValidationError(
            "Quantipy run source inventory does not exactly match implementation commit"
        )


def _validate_quantipy_run_panel(value: object, *, label: str) -> dict[str, object]:
    data = _strict_json_keys(
        value,
        label=label,
        expected=(
            "panel_path",
            "panel_sha256",
            "receipt_path",
            "receipt_sha256",
            "request_sha256",
            "coverage_sha256",
            "receipt",
        ),
    )
    if data["panel_path"] != "panel/panel.parquet":
        raise AutoresearchValidationError(f"{label}.panel_path is invalid")
    if data["receipt_path"] != "panel/receipt.json":
        raise AutoresearchValidationError(f"{label}.receipt_path is invalid")
    receipt = _validate_panel_receipt(data["receipt"], label=f"{label}.receipt")
    canonical_receipt_bytes = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(canonical_receipt_bytes) > QUANTIPY_PANEL_RECEIPT_MAX_BYTES:
        raise AutoresearchValidationError(f"{label}.receipt exceeds its size limit")
    panel_sha = _strict_json_sha256(data["panel_sha256"], label=f"{label}.panel_sha256")
    receipt_sha = _strict_json_sha256(data["receipt_sha256"], label=f"{label}.receipt_sha256")
    request_sha = _strict_json_sha256(data["request_sha256"], label=f"{label}.request_sha256")
    coverage_sha = _strict_json_sha256(data["coverage_sha256"], label=f"{label}.coverage_sha256")
    if (
        panel_sha != receipt["panel_sha256"]
        or request_sha != receipt["request_sha256"]
        or coverage_sha != receipt["coverage_sha256"]
    ):
        raise AutoresearchValidationError(f"{label} digests do not bind its nested receipt")
    return {
        "panel_path": "panel/panel.parquet",
        "panel_sha256": panel_sha,
        "receipt_path": "panel/receipt.json",
        "receipt_sha256": receipt_sha,
        "request_sha256": request_sha,
        "coverage_sha256": coverage_sha,
        "receipt": receipt,
    }


def _validate_quantipy_run_envelope(
    snapshot: _SecureFileSnapshot,
) -> dict[str, object]:
    run = _strict_json_keys(
        _parse_json_snapshot(snapshot, label="Quantipy run.json"),
        label="Quantipy run.json",
        expected=(
            "run_id",
            "identity",
            "manifest_sha256",
            "source",
            "success",
            "panel_requested",
            "panel",
            "stage_receipts",
            "telemetry",
            "failure",
        ),
    )
    run_id = _strict_json_string(run["run_id"], label="Quantipy run.json run_id")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id) is None:
        raise AutoresearchValidationError("Quantipy run.json run_id is invalid")
    identity = _strict_json_keys(
        run["identity"],
        label="Quantipy run.json identity",
        expected=("experiment_id", "package_path", "notebook_path"),
    )
    identity_experiment_id = _strict_json_string(
        identity["experiment_id"],
        label="Quantipy run.json identity experiment_id",
    )
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", identity_experiment_id) is None:
        raise AutoresearchValidationError("Quantipy run.json identity experiment_id is invalid")
    normalized_identity: dict[str, object] = {
        "experiment_id": identity_experiment_id,
        "package_path": _strict_json_string(
            identity["package_path"],
            label="Quantipy run.json identity package_path",
            minimum=1,
            maximum=QUANTIPY_EXPERIMENT_IDENTITY_PATH_MAX_LENGTH,
        ),
        "notebook_path": None,
    }
    if identity["notebook_path"] is not None:
        normalized_identity["notebook_path"] = _strict_json_string(
            identity["notebook_path"],
            label="Quantipy run.json identity notebook_path",
            minimum=1,
            maximum=QUANTIPY_EXPERIMENT_IDENTITY_PATH_MAX_LENGTH,
        )
    manifest_sha = _strict_json_sha256(
        run["manifest_sha256"], label="Quantipy run.json manifest_sha256"
    )
    source = (
        _validate_quantipy_run_source(
            run["source"],
            label="Quantipy run.json source",
        )
        if run["source"] is not None
        else None
    )
    success = _strict_json_bool(run["success"], label="Quantipy run.json success")
    panel_requested = _strict_json_bool(
        run["panel_requested"], label="Quantipy run.json panel_requested"
    )
    panel = (
        _validate_quantipy_run_panel(run["panel"], label="Quantipy run.json panel")
        if run["panel"] is not None
        else None
    )
    receipts_raw = run["stage_receipts"]
    if not isinstance(receipts_raw, list):
        raise AutoresearchValidationError("Quantipy run.json stage_receipts must be a JSON array")
    normalized_receipts: list[dict[str, object]] = []
    stages: list[str] = []
    for index, receipt_raw in enumerate(receipts_raw):
        label = f"Quantipy run.json stage_receipts[{index}]"
        receipt = _strict_json_keys(
            receipt_raw,
            label=label,
            expected=(
                "stage",
                "status",
                "started_at",
                "completed_at",
                "wall_seconds",
                "result",
                "failure",
            ),
        )
        stage = _strict_json_enum(
            receipt["stage"],
            label=f"{label}.stage",
            allowed=frozenset(QUANTIPY_EXPERIMENT_STAGE_ORDER),
        )
        status_value = _strict_json_enum(
            receipt["status"],
            label=f"{label}.status",
            allowed=frozenset(("completed", "rejected", "failed")),
        )
        started = _strict_json_datetime(receipt["started_at"], label=f"{label}.started_at")
        completed = _strict_json_datetime(receipt["completed_at"], label=f"{label}.completed_at")
        if completed < started:
            raise AutoresearchValidationError(f"{label} completion precedes start")
        wall_seconds = _strict_json_float(receipt["wall_seconds"], label=f"{label}.wall_seconds")
        result: dict[str, object] | None = None
        failure: dict[str, str] | None = None
        if receipt["result"] is not None:
            result_data = _strict_json_keys(
                receipt["result"],
                label=f"{label}.result",
                expected=("stage", "decision", "summary"),
            )
            result = {
                "stage": _strict_json_enum(
                    result_data["stage"],
                    label=f"{label}.result.stage",
                    allowed=frozenset(QUANTIPY_EXPERIMENT_STAGE_ORDER),
                ),
                "decision": _strict_json_enum(
                    result_data["decision"],
                    label=f"{label}.result.decision",
                    allowed=frozenset(("accepted", "rejected")),
                ),
                "summary": _strict_json_string(
                    result_data["summary"],
                    label=f"{label}.result.summary",
                    minimum=1,
                    maximum=QUANTIPY_EXPERIMENT_STAGE_SUMMARY_MAX_LENGTH,
                ),
            }
        if receipt["failure"] is not None:
            failure = _validate_quantipy_failure(receipt["failure"], label=f"{label}.failure")
        if status_value == "completed":
            if result is None or result["decision"] != "accepted" or failure is not None:
                raise AutoresearchValidationError(
                    f"{label} completed status requires accepted result only"
                )
        elif status_value == "rejected":
            if result is None or result["decision"] != "rejected" or failure is not None:
                raise AutoresearchValidationError(
                    f"{label} rejected status requires rejected result only"
                )
            if stage not in {"smoke", "feasibility"}:
                raise AutoresearchValidationError(f"{label} only smoke or feasibility may reject")
        elif result is not None or failure is None:
            raise AutoresearchValidationError(f"{label} failed status requires failure only")
        elif failure["category"] not in {"import", "stage", "filesystem"}:
            raise AutoresearchValidationError(f"{label} entered stage failure category is invalid")
        if result is not None and result["stage"] != stage:
            raise AutoresearchValidationError(f"{label} result stage does not match receipt stage")
        stages.append(stage)
        normalized_receipts.append(
            {
                "stage": stage,
                "status": status_value,
                "started_at": receipt["started_at"],
                "completed_at": receipt["completed_at"],
                "wall_seconds": wall_seconds,
                "result": result,
                "failure": failure,
            }
        )
    if tuple(stages) != QUANTIPY_EXPERIMENT_STAGE_ORDER[: len(stages)]:
        raise AutoresearchValidationError(
            "Quantipy run.json stage receipts are not an ordered prefix"
        )
    if normalized_receipts and any(
        receipt["status"] != "completed" for receipt in normalized_receipts[:-1]
    ):
        raise AutoresearchValidationError(
            "only the final Quantipy entered stage may be non-completed"
        )
    telemetry = _strict_json_keys(
        run["telemetry"],
        label="Quantipy run.json telemetry",
        expected=("scope", "started_at", "completed_at", "wall_seconds"),
    )
    if telemetry["scope"] != "process_wide":
        raise AutoresearchValidationError("Quantipy run.json telemetry scope is invalid")
    telemetry_started = _strict_json_datetime(
        telemetry["started_at"], label="Quantipy run.json telemetry started_at"
    )
    telemetry_completed = _strict_json_datetime(
        telemetry["completed_at"], label="Quantipy run.json telemetry completed_at"
    )
    if telemetry_completed < telemetry_started:
        raise AutoresearchValidationError("Quantipy run.json telemetry completion precedes start")
    telemetry_wall = _strict_json_float(
        telemetry["wall_seconds"], label="Quantipy run.json telemetry wall_seconds"
    )
    previous_completed: datetime | None = None
    for index, receipt in enumerate(normalized_receipts):
        stage_started = _strict_json_datetime(
            receipt["started_at"],
            label=f"Quantipy run.json stage_receipts[{index}].started_at",
        )
        stage_completed = _strict_json_datetime(
            receipt["completed_at"],
            label=f"Quantipy run.json stage_receipts[{index}].completed_at",
        )
        if stage_started < telemetry_started or stage_completed > telemetry_completed:
            raise AutoresearchValidationError(
                "Quantipy stage timing falls outside process-wide telemetry"
            )
        if previous_completed is not None and stage_started < previous_completed:
            raise AutoresearchValidationError("Quantipy stage timing overlaps or moves backward")
        previous_completed = stage_completed
    failure = (
        _validate_quantipy_failure(run["failure"], label="Quantipy run.json failure")
        if run["failure"] is not None
        else None
    )
    if not panel_requested and panel is not None:
        raise AutoresearchValidationError("unrequested Quantipy runs cannot bind panel evidence")
    if not panel_requested and failure is not None and failure["category"] == "panel":
        raise AutoresearchValidationError("unrequested Quantipy runs cannot fail panel preparation")
    if (
        panel_requested
        and panel is None
        and (failure is None or failure["category"] not in {"panel", "preflight", "filesystem"})
    ):
        raise AutoresearchValidationError(
            "requested Quantipy panels require evidence or a valid pre-stage failure"
        )
    if panel is not None and failure is not None and failure["category"] == "panel":
        raise AutoresearchValidationError("panel failures cannot also bind panel evidence")
    if panel is not None and failure is not None and failure["category"] == "preflight":
        raise AutoresearchValidationError("preflight failures cannot bind panel evidence")
    if failure is not None and success:
        raise AutoresearchValidationError("failed Quantipy runs cannot be successful")
    if (
        failure is not None
        and failure["category"] in {"preflight", "panel"}
        and normalized_receipts
    ):
        raise AutoresearchValidationError(
            "preflight and panel failures require zero stage receipts"
        )
    if failure is not None and failure["category"] in {"manifest", "preflight"}:
        if source is not None:
            raise AutoresearchValidationError(
                "manifest and preflight failures cannot bind source evidence"
            )
    elif source is None:
        raise AutoresearchValidationError(
            "entered and post-preflight Quantipy runs require source evidence"
        )
    if (
        failure is not None
        and failure["category"] in {"import", "stage", "filesystem"}
        and normalized_receipts
    ):
        terminal = normalized_receipts[-1]
        terminal_failure = terminal["failure"]
        if terminal["status"] != "failed" or not isinstance(terminal_failure, Mapping):
            raise AutoresearchValidationError(
                "entered run failures require a terminal failed stage"
            )
        if terminal_failure["category"] != failure["category"]:
            raise AutoresearchValidationError("run failure must match terminal stage failure")
    if (
        failure is not None
        and failure["category"] in {"import", "stage"}
        and not normalized_receipts
    ):
        raise AutoresearchValidationError(
            "import and stage run failures require a terminal failed stage"
        )
    if not normalized_receipts and failure is None:
        raise AutoresearchValidationError(
            "Quantipy runs without entered stages require a run-level failure"
        )
    if (
        failure is None
        and len(normalized_receipts) < len(QUANTIPY_EXPERIMENT_STAGE_ORDER)
        and all(receipt["status"] == "completed" for receipt in normalized_receipts)
    ):
        raise AutoresearchValidationError(
            "incomplete Quantipy runs require rejection, stage failure, or run failure"
        )
    calculated_success = (
        failure is None
        and len(normalized_receipts) == 4
        and all(receipt["status"] == "completed" for receipt in normalized_receipts)
    )
    if success != calculated_success:
        raise AutoresearchValidationError(
            "Quantipy run.json success is inconsistent with stage receipts"
        )
    if success and normalized_receipts[-1]["stage"] != "model":
        raise AutoresearchValidationError("successful Quantipy runs must end with model")
    normalized_run: dict[str, object] = {
        "run_id": run_id,
        "identity": normalized_identity,
        "manifest_sha256": manifest_sha,
        "source": source,
        "success": success,
        "panel_requested": panel_requested,
        "panel": panel,
        "stage_receipts": normalized_receipts,
        "telemetry": {
            "scope": "process_wide",
            "started_at": telemetry["started_at"],
            "completed_at": telemetry["completed_at"],
            "wall_seconds": telemetry_wall,
        },
        "failure": failure,
    }
    canonical_size = len(
        json.dumps(
            normalized_run,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if canonical_size >= QUANTIPY_RUN_ENVELOPE_MAX_BYTES:
        raise AutoresearchValidationError(
            "Quantipy run.json canonical envelope exceeds its size limit"
        )
    return normalized_run


def _run_failure_from_mapping(raw: object) -> QuantipyExperimentFailureEvidence | None:
    if raw is None:
        return None
    return QuantipyExperimentFailureEvidence.from_dict(raw)


def _legacy_quantipy_bash_command(
    implementation: ImplementationResultArtifact,
    *,
    run_id: str,
) -> tuple[str, ...]:
    """Return the sealed v3/v4 shell command accepted only as historical evidence."""
    return (
        "bash",
        "-lc",
        " ".join(
            (
                "env",
                "PYTHONDONTWRITEBYTECODE=1",
                "uv",
                "run",
                "quantipy",
                "experiment",
                "run",
                implementation.experiment_manifest_path,
                "--output-root",
                str(DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT),
                "--run-id",
                run_id,
            )
        ),
    )


def _validate_quantipy_detached_run_attestation(
    *,
    state: AutoresearchState,
    implementation: ImplementationResultArtifact,
    evidence: QuantipyExperimentEvidence,
    expected_run_id: str,
    run_snapshot: _SecureFileSnapshot,
    validation_context: AutoresearchValidationContext | None,
    target_root: Path,
) -> None:
    try:
        import gateway.autoresearch_runs as detached_runs

        detached_record = detached_runs.read_run_record(
            run_dir=Path(evidence.detached_run_directory),
            runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "Quantipy detached run record is unavailable or invalid"
        ) from exc
    if detached_record.run_directory != Path(evidence.detached_run_directory):
        raise AutoresearchValidationError("Quantipy detached run directory is not canonical")
    if detached_record.status.manifest_sha256 != evidence.detached_run_manifest_sha256:
        raise AutoresearchValidationError(
            "Quantipy detached run manifest digest does not match evidence"
        )
    detached_manifest = detached_record.manifest
    historical_v2_run_id = _deterministic_quantipy_run_id(
        state.iteration,
        implementation.commit_sha,
        attempt=2,
    )
    contract = (
        _build_historical_v2_quantipy_execution_contract(
            runtime_root=target_root,
            manifest_path=Path(implementation.experiment_manifest_path),
            output_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            run_id=expected_run_id,
        )
        if expected_run_id == historical_v2_run_id
        else build_quantipy_execution_contract(
            runtime_root=target_root,
            manifest_path=Path(implementation.experiment_manifest_path),
            output_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            run_id=expected_run_id,
        )
    )
    if (
        detached_manifest.iteration != state.iteration
        or detached_manifest.phase is not Phase.VERIFICATION
        or detached_manifest.working_directory != str(contract.working_directory)
        or detached_manifest.expected_artifact_path != evidence.run_json_path
        or detached_manifest.command_sha256 != detached_runs.command_sha256(contract.command)
    ):
        raise AutoresearchValidationError(
            "Quantipy detached run manifest does not bind the exact verification command"
        )
    detached_status = detached_record.status
    if evidence.success:
        if (
            detached_status.state is not detached_runs.RunState.SUCCEEDED
            or detached_status.exit_code != 0
            or detached_status.signal_number is not None
            or detached_status.failure_classification is not None
        ):
            raise AutoresearchValidationError(
                "successful Quantipy envelope requires detached success with exit code 0"
            )
    elif (
        detached_status.state is not detached_runs.RunState.FAILED
        or detached_status.exit_code != 1
        or detached_status.signal_number is not None
        or detached_status.failure_classification
        is not detached_runs.RunFailureClassification.PROCESS_ERROR
    ):
        raise AutoresearchValidationError(
            "failed or rejected Quantipy envelope requires detached contract exit code 1"
        )
    capture = detached_status.output_capture
    if capture is None or not capture.stdout.eof_observed or not capture.stderr.eof_observed:
        raise AutoresearchValidationError(
            "Quantipy detached run lacks complete EOF-drained independent output capture"
        )
    artifact_attestation = detached_status.expected_artifact_attestation
    if (
        detached_status.expected_artifact_attestation_status
        is not detached_runs.ExpectedArtifactAttestationStatus.ATTESTED
        or artifact_attestation is None
    ):
        raise AutoresearchValidationError(
            "Quantipy detached run lacks expected artifact attestation"
        )
    if (
        artifact_attestation.path != evidence.run_json_path
        or artifact_attestation.size_bytes != len(run_snapshot.content)
        or artifact_attestation.sha256 != run_snapshot.sha256
    ):
        raise AutoresearchValidationError(
            "Quantipy run.json does not match detached worker artifact attestation"
        )
    recovery = state.platform_runtime_recovery_receipt
    if recovery is None:
        return
    if validation_context is None:
        raise AutoresearchValidationError(
            "canonical detached result validation requires a readiness validation context"
        )
    validation_context.validate_for_state(state)
    if validation_context.quantipy_commit is None:
        raise AutoresearchValidationError(
            "canonical detached result validation requires the readiness-pinned Quantipy commit"
        )
    current_runtime = _attest_canonical_quantipy_runtime(
        state,
        implementation,
        readiness_quantipy_commit=validation_context.quantipy_commit,
    )
    if (
        current_runtime != recovery.runtime
        or current_runtime.root != str(target_root)
        or current_runtime.readiness_quantipy_commit != validation_context.quantipy_commit
    ):
        raise AutoresearchValidationError("canonical detached result runtime attestation changed")


def _validate_quantipy_experiment_evidence(
    state: AutoresearchState,
    artifact: VerificationResultArtifact,
    *,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    evidence = artifact.quantipy_experiment_evidence
    not_started = artifact.quantipy_execution_not_started
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "Quantipy experiment evidence requires implementation_result"
        )
    implementation = state.implementation_result
    workspace = _require_strict_canonical_workspace_path(
        implementation.workspace_path,
        label="implementation_result workspace_path",
    )
    canonical_commit = _resolve_git_commit(
        workspace,
        implementation.commit_sha,
        label="implementation_result commit_sha",
    )
    head_commit = _resolve_git_commit(workspace, "HEAD", label="implementation workspace HEAD")
    if head_commit != canonical_commit:
        raise AutoresearchValidationError(
            "verification requires workspace HEAD to equal implementation commit_sha"
        )
    manifest_snapshot = _secure_open_snapshot(
        implementation.experiment_manifest_path,
        label="implementation_result experiment_manifest_path",
    )
    manifest = _validate_quantipy_v2_manifest(
        manifest_snapshot,
        workspace=workspace,
        commit_sha=canonical_commit,
        expected_sha256=implementation.experiment_manifest_sha256,
    )
    expected_run_id = _expected_quantipy_verification_run_id(state, canonical_commit)
    expected_run_path = DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / expected_run_id / "run.json"
    _require_private_directory(
        DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        label="trusted Quantipy runs root",
    )

    if evidence is None:
        if artifact.status is VerificationStatus.PASS:
            raise AutoresearchValidationError(
                "PASS verification requires Quantipy experiment evidence"
            )
        if not_started is None:
            raise AutoresearchValidationError(
                "non-PASS without runtime evidence requires execution-not-started evidence"
            )
        if (
            not_started.manifest_path != implementation.experiment_manifest_path
            or not_started.manifest_sha256 != implementation.experiment_manifest_sha256
            or not_started.expected_run_id != expected_run_id
            or not_started.expected_run_json_path != str(expected_run_path)
        ):
            raise AutoresearchValidationError(
                "execution-not-started evidence does not bind the implementation and expected run"
            )
        if not_started.command not in artifact.commands_run:
            raise AutoresearchValidationError(
                "execution-not-started command must appear exactly in commands_run"
            )
        if "quantipy experiment" in not_started.command:
            raise AutoresearchValidationError(
                "focused_tests_failed cannot claim a Quantipy experiment command"
            )
        _reserve_quantipy_execution_not_started(
            not_started,
            runs_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        )
        return
    if not_started is not None:
        raise AutoresearchValidationError(
            "runtime evidence and execution-not-started evidence are mutually exclusive"
        )
    if (
        evidence.manifest_path != implementation.experiment_manifest_path
        or evidence.manifest_sha256 != implementation.experiment_manifest_sha256
    ):
        raise AutoresearchValidationError(
            "Quantipy experiment evidence manifest binding does not match implementation_result"
        )
    if evidence.run_id != expected_run_id:
        raise AutoresearchValidationError(
            "Quantipy experiment evidence run_id is not deterministic for this iteration and commit"
        )
    if evidence.run_json_path != str(expected_run_path):
        raise AutoresearchValidationError(
            "Quantipy experiment run_json_path must use the trusted canonical run layout"
        )
    run_snapshot = _secure_open_snapshot(
        evidence.run_json_path,
        label="quantipy_experiment_evidence.run_json_path",
        trusted_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        private=True,
        max_bytes=QUANTIPY_RUN_ENVELOPE_MAX_BYTES - 1,
    )
    _require_private_directory(run_snapshot.path.parent, label="Quantipy run directory")
    if run_snapshot.sha256 != evidence.run_json_sha256:
        raise AutoresearchValidationError(
            "quantipy_experiment_evidence.run_json_sha256 does not match run.json"
        )
    run = _validate_quantipy_run_envelope(run_snapshot)
    if run["run_id"] != evidence.run_id or run["success"] is not evidence.success:
        raise AutoresearchValidationError("Quantipy run.json identity does not match evidence")
    if run["manifest_sha256"] != _canonical_quantipy_manifest_sha256(manifest):
        raise AutoresearchValidationError(
            "Quantipy run.json manifest_sha256 does not match manifest"
        )
    identity = _ensure_mapping(run["identity"], label="Quantipy run.json identity")
    manifest_source_root = manifest_snapshot.path.parent
    package_path = str(manifest_source_root / str(manifest["package_path"]))
    notebook_path = (
        str(manifest_source_root / str(manifest["notebook_path"]))
        if manifest["notebook_path"] is not None
        else None
    )
    if dict(identity) != {
        "experiment_id": manifest["experiment_id"],
        "package_path": package_path,
        "notebook_path": notebook_path,
    }:
        raise AutoresearchValidationError(
            "Quantipy run.json experiment identity does not match manifest"
        )
    run_source = run["source"]
    if run_source is not None:
        _validate_quantipy_execution_source_against_commit(
            _ensure_mapping(run_source, label="Quantipy run.json source"),
            manifest=manifest,
            source_root=manifest_source_root,
            workspace=workspace,
            commit_sha=canonical_commit,
        )
    _validate_quantipy_detached_run_attestation(
        state=state,
        implementation=implementation,
        evidence=evidence,
        expected_run_id=expected_run_id,
        run_snapshot=run_snapshot,
        validation_context=validation_context,
        target_root=_target_repo_root_for_state(state),
    )
    receipts = run["stage_receipts"]
    assert isinstance(receipts, list)
    completed: list[str] = []
    terminal_failure: QuantipyExperimentFailureEvidence | None = None
    terminal_stage: str | None = None
    terminal_status: str | None = None
    for receipt_raw in receipts:
        receipt = _ensure_mapping(receipt_raw, label="Quantipy run.json stage receipt")
        stage = str(receipt["stage"])
        status = str(receipt["status"])
        if status == "completed":
            completed.append(stage)
        elif status == "rejected":
            terminal_stage = stage
            terminal_status = status
        else:
            terminal_failure = _run_failure_from_mapping(receipt["failure"])
            terminal_stage = stage
            terminal_status = status
    if tuple(completed) != evidence.completed_stages:
        raise AutoresearchValidationError(
            "Quantipy experiment evidence completed_stages does not match run.json"
        )
    if (evidence.terminal_stage, evidence.terminal_status) != (terminal_stage, terminal_status):
        raise AutoresearchValidationError(
            "Quantipy experiment terminal stage evidence does not match run.json"
        )
    run_failure = _run_failure_from_mapping(run["failure"])
    actual_failure = run_failure if run_failure is not None else terminal_failure
    if evidence.failure != actual_failure:
        raise AutoresearchValidationError(
            "Quantipy experiment failure evidence does not match run.json"
        )
    is_success = bool(run["success"])
    if evidence.success is not is_success:
        raise AutoresearchValidationError("Quantipy run.json success does not match stage receipts")
    if artifact.status is VerificationStatus.PASS and not is_success:
        raise AutoresearchValidationError("PASS requires a successful completed Quantipy v2 run")
    if artifact.status is VerificationStatus.TEST_FAILURE and is_success:
        raise AutoresearchValidationError(
            "TEST_FAILURE must not report a successful Quantipy v2 run"
        )
    if (
        artifact.status is VerificationStatus.BUG_SIGNAL
        and is_success
        and not artifact.tests_passed
    ):
        raise AutoresearchValidationError(
            "BUG_SIGNAL successful Quantipy v2 run requires tests_passed=true"
        )
    panel_requested = bool(run["panel_requested"])
    if panel_requested is (manifest["panel"] is None):
        raise AutoresearchValidationError(
            "Quantipy run.json panel_requested does not match manifest"
        )
    run_panel = run["panel"]
    if run_panel is None:
        if evidence.panel is not None:
            raise AutoresearchValidationError(
                "Quantipy experiment panel evidence is not present in run.json"
            )
    else:
        run_panel_data = _ensure_mapping(run_panel, label="Quantipy run.json panel")
        if evidence.panel is None or evidence.panel.to_dict() != {
            "panel_path": run_panel_data["panel_path"],
            "panel_sha256": run_panel_data["panel_sha256"],
            "receipt_path": run_panel_data["receipt_path"],
            "receipt_sha256": run_panel_data["receipt_sha256"],
            "request_sha256": run_panel_data["request_sha256"],
            "coverage_sha256": run_panel_data["coverage_sha256"],
        }:
            raise AutoresearchValidationError(
                "Quantipy experiment panel evidence does not match run.json"
            )
        panel_directory = run_snapshot.path.parent / "panel"
        _require_sealed_quantipy_panel_directory(panel_directory)
        panel_snapshot = _secure_open_snapshot(
            run_snapshot.path.parent / evidence.panel.panel_path,
            label="Quantipy panel file",
            trusted_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            private=True,
            max_bytes=1024 * 1024 * 1024,
        )
        receipt_snapshot = _secure_open_snapshot(
            run_snapshot.path.parent / evidence.panel.receipt_path,
            label="Quantipy panel receipt",
            trusted_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            private=True,
            max_bytes=QUANTIPY_PANEL_RECEIPT_MAX_BYTES,
        )
        _require_sealed_quantipy_panel_file(panel_snapshot, label="Quantipy panel file")
        _require_sealed_quantipy_panel_file(receipt_snapshot, label="Quantipy panel receipt")
        if (
            panel_snapshot.sha256 != evidence.panel.panel_sha256
            or receipt_snapshot.sha256 != evidence.panel.receipt_sha256
        ):
            raise AutoresearchValidationError("Quantipy panel evidence digest does not match files")
        persisted_receipt = _validate_panel_receipt(
            _parse_json_snapshot(receipt_snapshot, label="Quantipy panel receipt"),
            label="Quantipy panel receipt",
        )
        if persisted_receipt != run_panel_data["receipt"]:
            raise AutoresearchValidationError(
                "Quantipy panel receipt bytes do not match nested run evidence"
            )
        manifest_panel = _ensure_mapping(manifest["panel"], label="manifest panel")
        if persisted_receipt["request"] != manifest_panel["request"]:
            raise AutoresearchValidationError(
                "Quantipy panel receipt request does not match manifest request"
            )


def _reserve_quantipy_execution_not_started(
    evidence: QuantipyExecutionNotStartedEvidence,
    *,
    runs_root: Path,
) -> None:
    """Reserve the deterministic run directory so a concurrent run cannot start later."""
    _require_private_directory(runs_root, label="trusted Quantipy runs root")
    root_fd = os.open(
        runs_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        try:
            os.mkdir(evidence.expected_run_id, mode=0o700, dir_fd=root_fd)
        except FileExistsError as exc:
            raise AutoresearchValidationError(
                "execution-not-started is false because expected run directory already exists"
            ) from exc
        except OSError as exc:
            raise AutoresearchValidationError(
                "cannot atomically reserve the execution-not-started run directory"
            ) from exc
        run_fd = os.open(
            evidence.expected_run_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        try:
            marker_fd = os.open(
                QUANTIPY_EXECUTION_NOT_STARTED_TOMBSTONE,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=run_fd,
            )
            try:
                payload = json.dumps(
                    {
                        "schema_version": "g2-quantipy-execution-not-started-v1",
                        **evidence.to_dict(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                offset = 0
                while offset < len(payload):
                    written = os.write(marker_fd, payload[offset:])
                    if written <= 0:
                        raise AutoresearchValidationError(
                            "execution-not-started tombstone write was incomplete"
                        )
                    offset += written
                os.fsync(marker_fd)
            finally:
                os.close(marker_fd)
            os.fsync(run_fd)
        finally:
            os.close(run_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _require_ancestor(
    worktree: Path,
    ancestor: str,
    descendant: str,
    *,
    error_message: str,
    missing_is_not_ancestor: bool = False,
) -> None:
    result = _run_git(
        worktree,
        ("merge-base", "--is-ancestor", ancestor, descendant),
        operation="ancestry check",
    )
    if result.returncode == 1 or (missing_is_not_ancestor and result.returncode != 0):
        raise AutoresearchValidationError(error_message)
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"Git ancestry check failed in {_render_literal(str(worktree))}"
        )


def _common_git_base(worktree: Path, first: str, second: str, *, label: str) -> str:
    result = _run_git(
        worktree,
        ("merge-base", first, second),
        operation=f"common ancestry check for {label}",
    )
    base = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{7,64}", base) is None:
        raise AutoresearchValidationError(f"Git common ancestry check failed for {label}")
    _require_ancestor(
        worktree,
        base,
        first,
        error_message=f"readiness base is not an ancestor of {label} runtime commit",
    )
    _require_ancestor(
        worktree,
        base,
        second,
        error_message=f"readiness base is not an ancestor of {label} implementation commit",
    )
    return base


def validate_artifact_workspace(
    state: AutoresearchState,
    artifact: ImplementationResultArtifact | FixResultArtifact,
) -> None:
    """Mechanically validate a committed artifact at the CLI advancement boundary.

    This intentionally performs filesystem and Git checks only at artifact
    advancement; deserializing persisted state remains pure and portable.
    """
    artifact.validate()
    if state.setup is None:
        raise AutoresearchValidationError("artifact workspace validation requires setup")
    workspace = _require_strict_canonical_workspace_path(
        artifact.workspace_path,
        label="artifact workspace_path",
    )
    worktree_root = _require_autoresearch_worktree_root()
    if isinstance(artifact, FixResultArtifact):
        if state.implementation_result is None:
            raise AutoresearchValidationError("fix_result requires implementation_result")
        state.implementation_result.validate()
        implementation_workspace = _require_strict_canonical_workspace_path(
            state.implementation_result.workspace_path,
            label="persisted implementation_result workspace_path",
        )
        _require_workspace_under_autoresearch_worktree_root(
            implementation_workspace,
            label="persisted implementation_result workspace_path",
            worktree_root=worktree_root,
        )
        _require_workspace_under_autoresearch_worktree_root(
            workspace,
            label="fix_result workspace_path",
            worktree_root=worktree_root,
        )
        if artifact.workspace_path != state.implementation_result.workspace_path:
            raise AutoresearchValidationError(
                "fix_result workspace_path must exactly match implementation_result workspace_path"
            )
        if workspace != implementation_workspace:
            raise AutoresearchValidationError(
                "fix_result workspace_path must identify the persisted implementation worktree"
            )
        _validate_fix_workspace(state, artifact)
    else:
        _require_workspace_under_autoresearch_worktree_root(
            workspace,
            label="implementation_result workspace_path",
            worktree_root=worktree_root,
        )
        _validate_implementation_workspace(state, artifact)

    workspace = _require_isolated_git_clone_root(workspace, label="artifact workspace_path")
    target_checkout = _require_git_worktree_root(
        Path(state.setup.target_repo).expanduser(),
        label="authoritative target_repo",
    )
    if workspace == target_checkout:
        raise AutoresearchValidationError(
            "artifact workspace_path must be distinct from authoritative target_repo"
        )
    _require_artifact_origin_matches_target(
        workspace,
        target_checkout,
        label="artifact workspace_path",
    )
    artifact_commit = _resolve_git_commit(
        workspace,
        artifact.commit_sha,
        label="artifact commit_sha",
    )
    worktree_head = _resolve_git_commit(workspace, "HEAD", label="worktree HEAD")
    if artifact_commit != worktree_head:
        raise AutoresearchValidationError("artifact commit_sha must equal worktree HEAD")
    _require_clean_git_worktree(workspace)

    if isinstance(artifact, FixResultArtifact):
        assert state.implementation_result is not None
        _require_ancestor(
            workspace,
            state.implementation_result.commit_sha,
            artifact_commit,
            error_message="prior implementation commit_sha is not an ancestor of final fix commit",
            missing_is_not_ancestor=True,
        )
        authoritative_head = _resolve_git_commit(
            target_checkout,
            "HEAD",
            label="authoritative target_repo HEAD",
        )
        _require_ancestor(
            workspace,
            authoritative_head,
            artifact_commit,
            error_message=("authoritative target_repo HEAD is not an ancestor of final fix commit"),
            missing_is_not_ancestor=True,
        )
    else:
        authoritative_head = _resolve_git_commit(
            target_checkout,
            "HEAD",
            label="authoritative target_repo HEAD",
        )
        _require_ancestor(
            workspace,
            authoritative_head,
            artifact_commit,
            error_message=(
                "authoritative target_repo HEAD is not an ancestor of implementation commit"
            ),
            missing_is_not_ancestor=True,
        )
        manifest_snapshot = _secure_open_snapshot(
            artifact.experiment_manifest_path,
            label="implementation_result experiment_manifest_path",
        )
        _validate_quantipy_v2_manifest(
            manifest_snapshot,
            workspace=workspace,
            commit_sha=artifact_commit,
            expected_sha256=artifact.experiment_manifest_sha256,
        )
    _require_private_directory(workspace, label="artifact workspace_path")


def _json_block(payload: Mapping[str, object], *, compact: bool = False) -> str:
    if compact:
        return _compact_json_block(payload)
    return json.dumps(payload, indent=2, sort_keys=True)


def _compact_json_block(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


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


def _extract_first_float(text: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if match is None:
        return None
    return float(match.group(0))


def _baseline_metric(state: AutoresearchState) -> float | None:
    if state.context_packet is not None:
        baseline = _extract_first_float(state.context_packet.baseline_metric)
        if baseline is not None:
            return baseline
    if state.setup is not None:
        return _extract_first_float(state.setup.baseline_summary)
    return None


def _validate_final_decision_artifact(
    artifact: FinalDecisionArtifact,
    state: AutoresearchState,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    _validate_final_decision_memory_requirement(state, artifact)
    if artifact.recommended_metric_name == OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires the dedicated operator transition"
        )

    latest_review = state.latest_review
    latest_verification = state.latest_verification
    latest_consensus = state.latest_consensus
    expected_reviewer_verdict = (
        FinalReviewerVerdict(latest_review.verdict.value)
        if latest_review is not None
        else FinalReviewerVerdict.NOT_RUN
    )
    if artifact.reviewer_verdict != expected_reviewer_verdict:
        raise AutoresearchValidationError(
            "final_decision reviewer_verdict must match latest review"
        )

    if (
        latest_consensus is not None
        and latest_consensus.status is ConsensusStatus.NO_CONSENSUS
        and state.implementation_result is None
    ):
        if artifact.decision is not FinalDecision.NO_CONSENSUS:
            raise AutoresearchValidationError(
                "final_decision must be NO_CONSENSUS when consensus never reached a majority"
            )
        if artifact.memory_write_required:
            raise AutoresearchValidationError(
                "NO_CONSENSUS requires final_decision.memory_write_required=false"
            )
        if artifact.infra_rationale is not None:
            raise AutoresearchValidationError(
                "NO_CONSENSUS final_decision cannot contain infra_rationale"
            )
        return

    if (
        _is_operator_precondition_consensus(latest_consensus)
        and state.implementation_result is None
        and latest_verification is None
    ):
        if artifact.decision is not FinalDecision.INFRA_BLOCKED:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires final_decision=INFRA_BLOCKED"
            )
        if artifact.memory_write_required:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires memory_write_required=false"
            )
        if artifact.reviewer_verdict is not FinalReviewerVerdict.NOT_RUN:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires reviewer_verdict=NOT_RUN"
            )
        if artifact.recommended_metric_value is not None:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires recommended_metric_value=null"
            )
        if not artifact.infra_rationale:
            raise AutoresearchValidationError(
                "operator-precondition consensus requires infra_rationale"
            )
        return

    if (
        latest_verification is not None
        and latest_verification.status is VerificationStatus.TEST_FAILURE
        and state.verification_fix_attempts >= 2
    ):
        if artifact.decision is not FinalDecision.CRASH:
            raise AutoresearchValidationError(
                "test failures after retries require final_decision=CRASH"
            )
        return

    if (
        latest_verification is not None
        and latest_verification.status is VerificationStatus.BUG_SIGNAL
        and state.verification_fix_attempts >= 2
    ):
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "bug signals after retries require final_decision=DISCARD"
            )
        if (
            state.mode is ResearchMode.DATA_INFRA_G0
            and _is_fail_closed_g0_platform_contract_bug_signal(latest_verification)
            and artifact.memory_write_required
        ):
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 platform_coverage_contract_mismatch BUG_SIGNAL discard "
                "requires memory_write_required=false"
            )
        return

    if state.mode is ResearchMode.DATA_INFRA_G0:
        if not artifact.infra_rationale:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision requires infra_rationale"
            )
        if latest_verification is None or latest_verification.infra_gate_outcome is None:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision requires an infrastructure verification gate"
            )
        if (
            latest_verification.status is not VerificationStatus.PASS
            or not latest_verification.tests_passed
        ):
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 infrastructure final decisions require a successful completed "
                "verification assessment with status=PASS and tests_passed=true"
            )
        expected = (
            FinalDecision.INFRA_REPAIRED
            if latest_verification.infra_gate_outcome is InfraGateOutcome.GATE_PASSED
            else FinalDecision.DISCARD
        )
        if artifact.decision is not expected:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision must be INFRA_REPAIRED for GATE_PASSED "
                "or non-suspending DISCARD for REMEDIATION_REQUIRED"
            )
        receipt = latest_verification.platform_coverage_validation
        preflight = (
            state.implementation_result.price_hydration_scope_preflight
            if state.implementation_result is not None
            else None
        )
        receipt_is_trusted = (
            receipt is not None
            and receipt.matches_shared_contract
            and receipt.status is PlatformCoverageStatus.COMPLETE
            and preflight is not None
            and latest_verification.universe_verification_receipt is not None
            and latest_verification.price_hydration_receipt is not None
            and _platform_receipt_has_expected_runner_provenance(
                receipt,
                preflight=preflight,
                universe=latest_verification.universe_verification_receipt,
                hydration=latest_verification.price_hydration_receipt,
                requested_sessions=_requested_sessions_for_preflight(
                    preflight,
                    validation_context,
                ),
            )
        )
        if expected is FinalDecision.INFRA_REPAIRED and not receipt_is_trusted:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 INFRA_REPAIRED requires a COMPLETE receipt cross-checked "
                "against runner-owned preflight identity and counts"
            )
        if expected is FinalDecision.DISCARD and artifact.memory_write_required:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 remediation DISCARD requires memory_write_required=false"
            )
        return

    if artifact.decision is FinalDecision.INFRA_BLOCKED:
        raise AutoresearchValidationError(
            "INFRA_BLOCKED requires the explicit operator-owned readiness suspension "
            "transition and cannot be emitted by a stage artifact"
        )

    if artifact.infra_rationale:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH final_decision cannot contain infra_rationale"
        )

    if latest_review is not None and (
        latest_review.verdict is ReviewVerdict.FAIL or latest_review.critical_issues
    ):
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "critical review issues require final_decision=DISCARD"
            )
        return

    if (
        latest_verification is not None
        and latest_verification.max_drawdown_pct is not None
        and latest_verification.max_drawdown_pct >= 30.0
    ):
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "max_drawdown_pct >= 30 requires final_decision=DISCARD"
            )
        return

    metric_value = artifact.recommended_metric_value
    if metric_value is None:
        raise AutoresearchValidationError(
            "final_decision requires recommended_metric_value for completed experiments"
        )

    if metric_value <= -0.5:
        if artifact.decision is not FinalDecision.DISCARD:
            raise AutoresearchValidationError(
                "decision Sharpe <= -0.5 requires final_decision=DISCARD"
            )
        return

    if (
        metric_value > 1.0
        and latest_review is not None
        and latest_review.verdict is ReviewVerdict.PASS
    ):
        if artifact.decision is not FinalDecision.STRONG_KEEP:
            raise AutoresearchValidationError(
                "decision Sharpe > 1.0 with reviewer PASS requires final_decision=STRONG KEEP"
            )
        return

    if metric_value > 0.5:
        if artifact.decision not in (
            FinalDecision.SIGNIFICANT_KEEP,
            FinalDecision.STRONG_KEEP,
        ):
            raise AutoresearchValidationError(
                "decision Sharpe > 0.5 requires SIGNIFICANT KEEP or STRONG KEEP"
            )
        return

    baseline_metric = _baseline_metric(state)
    if baseline_metric is None:
        if artifact.decision is FinalDecision.KEEP:
            raise AutoresearchValidationError(
                "plain KEEP requires a numeric baseline to prove improvement"
            )
        if artifact.decision in KEEP_DECISIONS:
            raise AutoresearchValidationError(
                "KEEP-family decisions without a numeric baseline require Sharpe > 0.5"
            )
        return

    if metric_value > baseline_metric:
        if artifact.decision not in KEEP_DECISIONS:
            raise AutoresearchValidationError(
                "decision Sharpe above baseline requires a KEEP-family final_decision"
            )
        return

    if artifact.decision is not FinalDecision.DISCARD:
        raise AutoresearchValidationError(
            "non-improving Sharpe must end with final_decision=DISCARD"
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
            "loading the required instructions."
        ),
        Phase.DEBATE: (
            "Run the fixed five-agent debate. "
            "Each configured debate agent returns one theory, one vote, "
            "and explicit objections. Do not substitute agents or models."
        ),
        Phase.CONSENSUS: (
            "Decide whether the latest debate has a 3-of-5 majority; return MAJORITY or "
            "NO_CONSENSUS only. The first NO_CONSENSUS gets exactly one retry. For every "
            "non-operator-precondition MAJORITY in both ALPHA_RESEARCH and DATA_INFRA_G0, "
            "freeze one compact universe_plan with profile "
            "identity/digests, sorted unique explicit selection dates, and "
            "next-session-or-later execution policy."
        ),
        Phase.IMPLEMENTATION: (
            "Implementation is allowed only after a majority consensus. "
            "Use the final implementation brief exactly "
            "as approved. No implementation without consensus majority. For "
            "ALPHA_RESEARCH, derive and prewarm the platform data plan before creating "
            "or running the committed quantipy-experiment-v2 package. The implementation "
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
            "in implementation_result. If within_budget is false, do not run any "
            "qp.prices(), hydrate, full backtest, or notebook command that would load "
            "the price panel; commit the scaffold, focused tests, notebook shell, and "
            "over-budget preflight so verification can emit the structured feasibility "
            "BUG_SIGNAL without spending the hydrate cost. Commit one canonical "
            "quantipy-experiment-v2 manifest under the workspace with exactly prepare, smoke, "
            "feasibility, and model stage_files; record its canonical absolute path and "
            "SHA-256 in implementation_result. A notebook alone is not implementation evidence."
        ),
        Phase.VERIFICATION: (
            "Verify the produced experiment deterministically. "
            "Use implementation_result.workspace_path and "
            "implementation_result.commit_sha as the source under test. "
            "Run focused tests, then the one canonical detached uv --directory runtime command "
            "under the fixed private runs root with deterministic "
            "run_id before evaluating metrics. "
            "Reject impossible metrics, failing tests, or incomplete required metrics."
        ),
        Phase.REVIEW: (
            "Run exactly one configured reviewer. "
            "The reviewer must return PASS, CONDITIONAL PASS, or FAIL "
            "with concrete fix requests."
        ),
        Phase.FIX_TEST: (
            "Apply a narrow fix against the latest verification or review failure. "
            "After a fix, the next step is always verification. Any notebook, "
            "hydrate, backtest, or similarly long test command MUST be launched "
            "through /home/dev/repos/g2_openclaw/scripts/run-long-task.sh with a "
            "unique absolute --run-dir and bounded polling; direct foreground "
            "execution is invalid. If the launcher cannot be used, fail closed "
            "and report the infrastructure blocker without emitting a fix_result."
        ),
        Phase.DECISION_LOG: (
            "Decide and log the completed iteration. "
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
        f"{compute_fit_contract}"
        f"{workspace_contract}"
        f"{verification_handoff_contract}"
        f"{mempalace_fact_instruction}"
        f"{operator_precondition_instruction}"
        f"{context_source_instruction}"
        f"ARTIFACT_CONTRACT={contract}\n"
    )


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
            output_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
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
    return (
        "Verification handoff contract:\n"
        "- Every verification attempt must terminate by writing a structured JSON "
        "verification_result artifact to an absolute path under the PM workspace, "
        "validating that same absolute path with jq/wc, and advancing it with "
        "`cd /home/dev/repos/g2_openclaw && uv run gateway-cli autoresearch-advance "
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
        worktree_root = _render_literal(str(DEFAULT_AUTORESEARCH_WORKTREE_ROOT))
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
        "is invalid; if the launcher cannot be used, fail closed and report the "
        "infrastructure blocker without emitting a fix_result.\n"
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
    compute_snapshot = collect_compute_capability_snapshot(target_repo)
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
    state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
) -> NextAction:
    try:
        validation_context = AutoresearchValidationContext.from_readiness(readiness)
        validation_context.validate_for_state(state)
        _validate_state(state, policy, validation_context)
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    if state.suspended:
        raise AutoresearchValidationError(
            "autoresearch is suspended on an infrastructure blocker; "
            "run autoresearch-resume after platform readiness changes"
        )
    if state.phase is not Phase.REVIEW:
        _revalidate_accepted_member_union_manifests(state)
    _validate_alpha_verification_price_preflight(state)
    target = _select_phase_target(state, policy)
    required_receipts = receipts.require(PHASE_RECEIPTS[state.phase])
    instruction_source_manifest = build_instruction_source_manifest(
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
    if prompt_bytes > MAX_NEXT_ACTION_PROMPT_BYTES:
        raise AutoresearchValidationError(
            "autoresearch prompt exceeds hard byte budget: "
            f"{prompt_bytes} > {MAX_NEXT_ACTION_PROMPT_BYTES} bytes for phase "
            f"{state.phase.value}; compact accepted artifact/state fields before dispatch"
        )
    if prompt_bytes > NEXT_ACTION_PROMPT_TARGET_BYTES:
        raise AutoresearchValidationError(
            "autoresearch prompt exceeds operational byte target: "
            f"{prompt_bytes} > {NEXT_ACTION_PROMPT_TARGET_BYTES} bytes for phase "
            f"{state.phase.value}; preserve the {MAX_NEXT_ACTION_PROMPT_BYTES}-byte hard "
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
        _revalidate_accepted_member_union_manifests(state)
    return action


def _clear_consumed_platform_runtime_receipts(state: AutoresearchState) -> AutoresearchState:
    """Remove active v5 authorization material once its result is in history."""
    receipt = state.external_verification_retry_receipt
    if receipt is None:
        return replace(state, canonical_quantipy_runtime_attestation=None)
    if receipt.retry_attempt != 5:
        return replace(state, canonical_quantipy_runtime_attestation=None)
    if state.platform_runtime_recovery_receipt is None:
        raise AutoresearchValidationError("v5 verification requires its runtime recovery receipt")
    if state.latest_verification is None:
        raise AutoresearchValidationError("v5 receipt cannot be consumed without a result")
    return replace(
        state,
        external_verification_retry_receipt=None,
        interrupted_verification_history=(),
        platform_runtime_recovery_receipt=None,
        canonical_quantipy_runtime_attestation=None,
    )


def advance_state(
    state: AutoresearchState,
    artifact: SetupContextArtifact
    | ContextPacketArtifact
    | DebateResultArtifact
    | ConsensusResultArtifact
    | ImplementationResultArtifact
    | VerificationResultArtifact
    | ReviewResultArtifact
    | FixResultArtifact
    | FinalDecisionArtifact,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
    *,
    state_path: Path | None = None,
) -> AutoresearchState:
    if state_path is not None:
        state = _validate_persisted_state_matches(state, state_path=state_path)
    _validate_state(state, policy, validation_context)
    if state.mode in (ResearchMode.ALPHA_RESEARCH, ResearchMode.DATA_INFRA_G0) and (
        state.phase is Phase.VERIFICATION
        or (state.mode is ResearchMode.DATA_INFRA_G0 and state.phase is Phase.DECISION_LOG)
    ):
        if validation_context is None:
            raise AutoresearchValidationError(
                f"{state.mode.name} artifact advancement requires a strict readiness "
                "validation context"
            )
        validation_context.validate_for_state(state)

    if state.phase is Phase.SETUP_CONTEXT:
        if isinstance(artifact, SetupContextArtifact):
            if state.setup is not None:
                raise AutoresearchValidationError("setup artifact already exists")
            return replace(state, setup=artifact)
        if isinstance(artifact, ContextPacketArtifact):
            if state.setup is None:
                raise AutoresearchValidationError("context packet requires setup first")
            return replace(
                state,
                context_packet=artifact,
                mode=artifact.research_mode,
                phase=Phase.DEBATE,
            )
        raise AutoresearchValidationError(
            "setup_context phase accepts setup or context_packet artifacts only"
        )

    if state.phase is Phase.DEBATE:
        if not isinstance(artifact, DebateResultArtifact):
            raise AutoresearchValidationError("debate phase accepts debate_result only")
        _validate_debate_result(
            artifact,
            policy,
            mode=state.mode,
            context=state.context_packet,
            target_repo=Path(state.setup.target_repo) if state.setup is not None else None,
            require_compute_fit=True,
        )
        expected_round = len(state.debate_rounds) + 1
        if artifact.round_number != expected_round:
            raise AutoresearchValidationError(
                f"debate round must be {expected_round}, got {artifact.round_number}"
            )
        return replace(
            state,
            debate_rounds=(*state.debate_rounds, artifact),
            phase=Phase.CONSENSUS,
        )

    if state.phase is Phase.CONSENSUS:
        if not isinstance(artifact, ConsensusResultArtifact):
            raise AutoresearchValidationError("consensus phase accepts consensus_result only")
        latest_debate = state.latest_debate
        if latest_debate is None:
            raise AutoresearchValidationError("consensus requires a debate_result first")
        if artifact.round_number != latest_debate.round_number:
            raise AutoresearchValidationError(
                "consensus round_number must match the latest debate round"
            )
        next_consensus_history = (*state.consensus_history, artifact)
        if artifact.status is ConsensusStatus.MAJORITY:
            if _is_operator_precondition_consensus(artifact):
                return replace(
                    state,
                    consensus_history=next_consensus_history,
                    phase=Phase.DECISION_LOG,
                )
            if artifact.universe_plan is None:
                raise AutoresearchValidationError(
                    "non-operator majority consensus requires a frozen universe_plan"
                )
            artifact.universe_plan.validate()
            return replace(
                state,
                consensus_history=next_consensus_history,
                phase=Phase.IMPLEMENTATION,
            )
        if state.consensus_retry_count == 0:
            return replace(
                state,
                consensus_history=next_consensus_history,
                consensus_retry_count=1,
                phase=Phase.DEBATE,
            )
        return replace(
            state,
            consensus_history=next_consensus_history,
            phase=Phase.DECISION_LOG,
        )

    if state.phase is Phase.IMPLEMENTATION:
        if not isinstance(artifact, ImplementationResultArtifact):
            raise AutoresearchValidationError(
                "implementation phase accepts implementation_result only"
            )
        if (
            state.latest_consensus is None
            or state.latest_consensus.status is not ConsensusStatus.MAJORITY
        ):
            raise AutoresearchValidationError(
                "cannot advance implementation without consensus majority"
            )
        if state_path is not None:
            validate_artifact_workspace(state, artifact)
        _validate_implementation_workspace(state, artifact, require_compute_fit=True)
        _validate_alpha_implementation_price_preflight(state, artifact)
        next_state = replace(state, implementation_result=artifact, phase=Phase.VERIFICATION)
        _validate_alpha_universe_chain(next_state)
        return next_state

    if state.phase is Phase.VERIFICATION:
        if not isinstance(artifact, VerificationResultArtifact):
            raise AutoresearchValidationError("verification phase accepts verification_result only")
        if state.implementation_result is None:
            raise AutoresearchValidationError("verification requires implementation_result")
        artifact.validate(mode=state.mode)
        _validate_alpha_price_scope_verification(state, artifact)
        _require_g0_platform_provenance(state, artifact, validation_context)
        if state_path is not None:
            _validate_quantipy_experiment_evidence(
                state,
                artifact,
                validation_context=validation_context,
            )
        next_verification_history = (*state.verification_history, artifact)
        consumed_runtime_recovery = _clear_consumed_platform_runtime_receipts(
            replace(state, verification_history=next_verification_history)
        )
        if artifact.status is VerificationStatus.PASS:
            next_state = replace(
                consumed_runtime_recovery,
                pending_fix_trigger=None,
                phase=Phase.REVIEW,
            )
            _validate_alpha_universe_chain(next_state, validation_context)
            return next_state
        if (
            artifact.status in (VerificationStatus.TEST_FAILURE, VerificationStatus.BUG_SIGNAL)
            and state.verification_fix_attempts >= 2
        ):
            next_state = replace(
                consumed_runtime_recovery,
                pending_fix_trigger=None,
                phase=Phase.DECISION_LOG,
            )
            _validate_alpha_universe_chain(next_state, validation_context)
            return next_state
        next_state = replace(
            consumed_runtime_recovery,
            pending_fix_trigger=FixTriggerPhase.VERIFICATION,
            phase=Phase.FIX_TEST,
        )
        _validate_alpha_universe_chain(next_state, validation_context)
        return next_state

    if state.phase is Phase.REVIEW:
        if not isinstance(artifact, ReviewResultArtifact):
            raise AutoresearchValidationError("review phase accepts review_result only")
        _validate_review_result(artifact, policy)
        next_review_history = (*state.review_history, artifact)
        if artifact.verdict is ReviewVerdict.FAIL or artifact.critical_issues:
            return replace(
                state,
                review_history=next_review_history,
                pending_fix_trigger=FixTriggerPhase.REVIEW,
                phase=Phase.FIX_TEST,
            )
        return replace(
            state,
            review_history=next_review_history,
            pending_fix_trigger=None,
            phase=Phase.DECISION_LOG,
        )

    if state.phase is Phase.FIX_TEST:
        if not isinstance(artifact, FixResultArtifact):
            raise AutoresearchValidationError("fix_test phase accepts fix_result only")
        if state.pending_fix_trigger is None:
            raise AutoresearchValidationError("fix_test phase requires pending_fix_trigger")
        if artifact.trigger_phase is not state.pending_fix_trigger:
            raise AutoresearchValidationError(
                "fix_result trigger_phase must match the pending fix source"
            )
        if artifact.trigger_phase is FixTriggerPhase.VERIFICATION:
            next_attempts = state.verification_fix_attempts + 1
        else:
            next_attempts = state.verification_fix_attempts
        if state_path is not None:
            validate_artifact_workspace(state, artifact)
        _validate_fix_workspace(state, artifact)
        assert state.implementation_result is not None
        next_implementation = replace(
            state.implementation_result,
            commit_sha=artifact.commit_sha,
            price_hydration_scope_preflight=(
                artifact.price_hydration_scope_preflight
                if artifact.price_hydration_scope_preflight is not None
                else state.implementation_result.price_hydration_scope_preflight
            ),
        )
        return replace(
            state,
            implementation_result=next_implementation,
            fix_history=(*state.fix_history, artifact),
            external_verification_retry_receipt=None,
            interrupted_verification_history=(),
            platform_runtime_recovery_receipt=None,
            verification_fix_attempts=next_attempts,
            pending_fix_trigger=None,
            phase=Phase.VERIFICATION,
        )

    if state.phase is Phase.DECISION_LOG:
        if not isinstance(artifact, FinalDecisionArtifact):
            raise AutoresearchValidationError("decision_log phase accepts final_decision only")
        if (
            state.latest_consensus is None
            and state.latest_review is None
            and state.latest_verification is None
        ):
            raise AutoresearchValidationError("final_decision requires prior artifacts")
        _validate_final_decision_artifact(artifact, state, validation_context)
        if artifact.decision is FinalDecision.INFRA_BLOCKED:
            next_state = replace(
                state,
                final_decision=artifact,
                phase=Phase.REPEAT,
                suspended=True,
                suspension_reason=artifact.infra_rationale,
            )
        else:
            next_state = replace(state, final_decision=artifact, phase=Phase.REPEAT)
        _validate_state(next_state, policy, validation_context)
        return next_state

    raise AutoresearchValidationError(
        "repeat phase does not accept artifacts; mark memory or start next iteration"
    )


def can_write_memory(state: AutoresearchState) -> bool:
    return (
        state.phase is Phase.REPEAT
        and state.final_decision is not None
        and state.final_decision.memory_write_required
        and _final_decision_requires_memory_write(state, state.final_decision)
    )


def _is_explicit_no_memory_transition(state: AutoresearchState) -> bool:
    decision = state.final_decision
    return (
        state.phase is Phase.REPEAT
        and decision is not None
        and _is_authorized_no_memory_final_decision(state)
        and not state.memory_written
        and state.memory_verification_receipt is None
    )


def _validate_operator_precondition_infra_blocked_suspension(state: AutoresearchState) -> None:
    decision = state.final_decision
    if (
        decision is not None
        and decision.decision is FinalDecision.INFRA_BLOCKED
        and _is_operator_precondition_consensus(state.latest_consensus)
        and not state.suspended
    ):
        raise AutoresearchValidationError(
            "operator-precondition INFRA_BLOCKED state must be suspended"
        )


def _is_operator_infrastructure_suspension_state(state: AutoresearchState) -> bool:
    decision = state.final_decision
    return (
        state.phase is Phase.REPEAT
        and state.mode is ResearchMode.ALPHA_RESEARCH
        and state.suspended
        and state.setup is not None
        and state.context_packet is not None
        and state.context_packet.research_mode is ResearchMode.ALPHA_RESEARCH
        and state.platform_readiness is not None
        and decision is not None
        and decision.experiment_id == _canonical_iteration_experiment_id(state.iteration)
        and decision.decision is FinalDecision.INFRA_BLOCKED
        and decision.recommended_metric_name == OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME
        and decision.recommended_metric_value is None
        and decision.rationale == OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE
        and decision.log_summary == OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY
        and decision.continue_loop
        and not decision.memory_write_required
        and decision.infra_rationale == state.suspension_reason
        and state.suspension_reason is not None
        and state.suspension_reason == state.suspension_reason.strip()
        and not state.memory_written
        and state.memory_verification_receipt is None
    )


def _is_data_infra_g0_blocked_no_memory_state(state: AutoresearchState) -> bool:
    decision = state.final_decision
    latest_verification = state.latest_verification
    return (
        state.phase is Phase.REPEAT
        and state.mode is ResearchMode.DATA_INFRA_G0
        and state.suspended
        and decision is not None
        and decision.decision is FinalDecision.INFRA_BLOCKED
        and bool(decision.infra_rationale)
        and not decision.memory_write_required
        and not state.memory_written
        and state.memory_verification_receipt is None
        and state.implementation_result is not None
        and latest_verification is not None
        and latest_verification.status is VerificationStatus.PASS
        and latest_verification.tests_passed
        and latest_verification.infra_gate_outcome is InfraGateOutcome.REMEDIATION_REQUIRED
    )


def _default_mempalace_kg_path() -> Path:
    palace_root = Path(
        os.environ.get("MEMPALACE_PALACE", str(Path.home() / ".mempalace/palace"))
    ).expanduser()
    return palace_root / "knowledge_graph.sqlite3"


def _standard_metric_object(decision: FinalDecisionArtifact) -> str:
    if decision.recommended_metric_value is None:
        return standardize_mempalace_kg_object(decision.recommended_metric_name)
    metric = f"{decision.recommended_metric_name}_{decision.recommended_metric_value:g}"
    return standardize_mempalace_kg_object(metric)


def _standard_data_window_object(
    coverage: DynamicUniverseCoverageReceipt | AggregateCoverageReceipt,
) -> str:
    """Return the normalized common data/OOS window token required in MemPalace."""
    if isinstance(coverage, DynamicUniverseCoverageReceipt):
        return standardize_mempalace_kg_object(
            f"{coverage.experiment_start}_to_{coverage.experiment_end}_oos_"
            f"{coverage.oos_start}_to_{coverage.oos_end}"
        )
    return standardize_mempalace_kg_object(
        f"{coverage.actual_common_start}_to_{coverage.actual_common_end}_oos_"
        f"{coverage.oos_start}_to_{coverage.oos_end}"
    )


def standardized_mempalace_kg_facts(state: AutoresearchState) -> dict[str, str]:
    """Return the exact standardized KG facts required for a final decision."""
    if state.final_decision is None or state.mode is None:
        raise AutoresearchValidationError(
            "standardized MemPalace facts require final_decision and mode"
        )
    if not _final_decision_requires_memory_write(state, state.final_decision):
        raise AutoresearchValidationError(
            "standardized MemPalace facts are allowed only for retention-eligible final decisions"
        )
    verification = state.latest_verification
    if verification is None:
        raise AutoresearchValidationError(
            "standardized MemPalace facts require the final decision's verification_result"
        )
    decision = state.final_decision
    data_window = (
        standardize_mempalace_kg_object("unavailable")
        if verification.data_coverage is None
        else _standard_data_window_object(verification.data_coverage)
    )
    facts = {
        "decision": standardize_mempalace_kg_object(decision.decision.value),
        "research_mode": standardize_mempalace_kg_object(state.mode.value),
        "data_window": data_window,
        "reviewer_verdict": standardize_mempalace_kg_object(decision.reviewer_verdict.value),
    }
    if state.mode is ResearchMode.ALPHA_RESEARCH:
        facts["alpha_decision_metric"] = _standard_metric_object(decision)
        facts["keeper_rationale" if decision.decision in KEEP_DECISIONS else "failed_due_to"] = (
            standardize_mempalace_kg_object(decision.rationale)
        )
        return facts
    if verification.infra_gate_outcome is None or decision.infra_rationale is None:
        raise AutoresearchValidationError(
            "DATA_INFRA_G0 standardized MemPalace facts require gate outcome and infra_rationale"
        )
    facts["infra_gate_outcome"] = standardize_mempalace_kg_object(
        verification.infra_gate_outcome.value
    )
    facts["infra_rationale"] = standardize_mempalace_kg_object(decision.infra_rationale)
    return facts


def build_final_memory_write_request(state: AutoresearchState) -> FinalMemoryWriteRequest:
    """Derive the only MemPalace write payload from a validated final state."""
    if not can_write_memory(state) or state.final_decision is None or state.mode is None:
        raise AutoresearchValidationError(
            "final MemPalace persistence requires a retention-eligible repeat decision"
        )
    verification = state.latest_verification
    if verification is None:
        raise AutoresearchValidationError(
            "final MemPalace persistence requires the final verification result"
        )
    drawer_content = _compact_json_block(
        {
            "experiment_id": state.final_decision.experiment_id,
            "final_decision": state.final_decision.to_dict(),
            "research_mode": state.mode.value,
            "schema": "g2-openclaw.autoresearch.final-memory.v1",
            "verification_result": verification.to_dict(),
        }
    )
    return FinalMemoryWriteRequest(
        experiment_id=state.final_decision.experiment_id,
        drawer_content=drawer_content,
        facts=standardized_mempalace_kg_facts(state),
    )


def finalize_repeat_memory(
    state: AutoresearchState,
    *,
    writer: FinalMemoryWriter | None = None,
) -> AutoresearchState:
    """Write, verify, and mark the state-owned final decision exactly once."""
    request = build_final_memory_write_request(state)
    finalizer = writer or SubprocessFinalMemoryWriter.from_environment(
        repository_root=G2_OPENCLAW_REPO_ROOT
    )
    try:
        kg_path = finalizer.write(request)
    except MempalaceFinalizationError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    receipt = verify_mempalace_final_decision(state, kg_path)
    return mark_memory_written(state, receipt)


def finalize_repeat_memory_state_file(
    state_path: Path,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
    writer: FinalMemoryWriter | None = None,
) -> AutoresearchState:
    """Finalize and atomically mark the current repeat state under its state lock."""
    resolved_state_path = state_path.expanduser().resolve(strict=False)
    with _exclusive_state_lock(resolved_state_path):
        state = load_state_file(resolved_state_path)
        _validate_state(state, policy, validation_context)
        finalized = finalize_repeat_memory(state, writer=writer)
        _validate_state(finalized, policy, validation_context)
        _atomic_save_state_file(resolved_state_path, finalized)
        return finalized


def _committed_finalization_journal_drawer_id(
    journal: object,
    *,
    expected_request_sha256: str,
) -> str:
    _validate_sha256(expected_request_sha256, label="expected_request_sha256")
    if not isinstance(journal, Mapping):
        raise AutoresearchValidationError("MemPalace finalization journal must be an object")
    if set(journal) != {"status", "request_sha256", "drawer_id"}:
        raise AutoresearchValidationError(
            "MemPalace committed finalization journal schema is invalid"
        )
    if journal.get("status") != "committed":
        raise AutoresearchValidationError("MemPalace finalization journal is not committed")
    request_sha256 = journal.get("request_sha256")
    if not isinstance(request_sha256, str):
        raise AutoresearchValidationError(
            "MemPalace finalization journal request_sha256 is invalid"
        )
    try:
        _validate_sha256(request_sha256, label="request_sha256")
    except AutoresearchValidationError as exc:
        raise AutoresearchValidationError(
            "MemPalace finalization journal request_sha256 is invalid"
        ) from exc
    if request_sha256 != expected_request_sha256:
        raise AutoresearchValidationError("MemPalace finalization journal does not match state")
    drawer_id = journal.get("drawer_id")
    if not isinstance(drawer_id, str) or not drawer_id.strip():
        raise AutoresearchValidationError(
            "MemPalace finalization journal lacks canonical drawer ID"
        )
    return drawer_id


def verify_mempalace_final_decision(
    state: AutoresearchState,
    kg_path: Path | None = None,
) -> MemoryVerificationReceipt:
    """Read and attest KG facts; this function never mutates MemPalace."""
    if state.final_decision is None or state.mode is None:
        raise AutoresearchValidationError("MemPalace verification requires final_decision and mode")
    if not _final_decision_requires_memory_write(state, state.final_decision):
        raise AutoresearchValidationError(
            "MemPalace verification is prohibited for a non-retention final decision"
        )
    if not state.final_decision.memory_write_required:
        raise AutoresearchValidationError(
            "MemPalace verification is not required for this final decision"
        )
    path = (kg_path if kg_path is not None else _default_mempalace_kg_path()).expanduser()
    if not path.is_file():
        raise AutoresearchValidationError(f"MemPalace KG does not exist: {path}")
    decision = state.final_decision
    expected_objects = standardized_mempalace_kg_facts(state)
    request = build_final_memory_write_request(state)
    journal_path = finalization_journal_path(path.parent, decision.experiment_id)
    try:
        journal: object = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutoresearchValidationError("MemPalace finalization journal is unavailable") from exc
    expected_journal_digest = _sha256_text(
        json.dumps(request.to_dict(), separators=(",", ":"), sort_keys=True)
    )
    expected_drawer_id = _committed_finalization_journal_drawer_id(
        journal,
        expected_request_sha256=expected_journal_digest,
    )
    required = set(expected_objects)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(triples)").fetchall()}
        required_columns = {
            "id",
            "subject",
            "predicate",
            "object",
            "valid_from",
            "valid_to",
            "source_file",
            "source_drawer_id",
        }
        if not required_columns <= columns:
            raise AutoresearchValidationError("MemPalace KG triples schema is incomplete")
        rows = connection.execute(
            """
            SELECT id, subject, predicate, object, valid_from, valid_to,
                   source_file, source_drawer_id
            FROM triples WHERE subject = ? AND valid_to IS NULL
            """,
            (decision.experiment_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise AutoresearchValidationError(f"cannot read MemPalace KG: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()

    facts: dict[str, list[tuple[str, str, str, str, str, str, str, str]]] = {}
    for row in rows:
        normalized_predicate = _normalise_predicate(str(row[2]))
        if normalized_predicate not in required:
            continue
        source_file = str(row[6] or "").strip()
        source_drawer_id = str(row[7] or "").strip()
        if source_file != FINAL_MEMORY_SOURCE_FILE or source_drawer_id != expected_drawer_id:
            raise AutoresearchValidationError(
                "MemPalace standardized facts require exact canonical finalizer provenance"
            )
        if not str(row[3]).strip():
            raise AutoresearchValidationError(
                "MemPalace standardized facts require non-empty objects"
            )
        normalized_row = (
            "" if row[0] is None else str(row[0]),
            "" if row[1] is None else str(row[1]),
            "" if row[2] is None else str(row[2]),
            "" if row[3] is None else str(row[3]),
            "" if row[4] is None else str(row[4]),
            "" if row[5] is None else str(row[5]),
            "" if row[6] is None else str(row[6]),
            "" if row[7] is None else str(row[7]),
        )
        facts.setdefault(normalized_predicate, []).append(normalized_row)
    missing = sorted(required - facts.keys())
    if missing:
        raise AutoresearchValidationError(
            "MemPalace required standardized facts are missing: " + ", ".join(missing)
        )

    for predicate, expected_object in expected_objects.items():
        if any(row[3] != expected_object for row in facts[predicate]):
            raise AutoresearchValidationError(
                f"MemPalace {predicate} fact does not match final decision artifact"
            )
    stable_rows = sorted(row for predicate_rows in facts.values() for row in predicate_rows)
    digest = _sha256_text(json.dumps(stable_rows, separators=(",", ":"), ensure_ascii=True))
    return MemoryVerificationReceipt(
        experiment_id=decision.experiment_id,
        kg_path=str(path),
        predicates=tuple(sorted(facts)),
        verified_rows_digest=digest,
    )


def mark_memory_written(
    state: AutoresearchState,
    receipt: MemoryVerificationReceipt,
) -> AutoresearchState:
    if not can_write_memory(state):
        raise AutoresearchValidationError(
            "memory write is allowed only after final decision that requires memory"
        )
    if state.final_decision is None or receipt.experiment_id != state.final_decision.experiment_id:
        raise AutoresearchValidationError("memory receipt must match final decision experiment_id")
    return replace(state, memory_written=True, memory_verification_receipt=receipt)


def _canonical_iteration_experiment_id(iteration: int) -> str:
    if iteration < 1:
        raise AutoresearchValidationError("iteration must be >= 1")
    return f"iteration-{iteration}"


def suspend_for_infrastructure(state: AutoresearchState, reason: str) -> AutoresearchState:
    """Durably suspend an active alpha iteration for operator-owned infra repair."""
    if not reason or not reason.strip():
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires a non-empty reason"
        )
    if reason != reason.strip():
        raise AutoresearchValidationError(
            "operator infrastructure suspension reason must not have leading or trailing whitespace"
        )
    if state.suspended:
        raise AutoresearchValidationError("autoresearch state is already suspended")
    if state.phase is Phase.REPEAT or state.final_decision is not None:
        raise AutoresearchValidationError(
            "autoresearch state is already finalized or in repeat phase"
        )
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires an active ALPHA_RESEARCH iteration"
        )
    if state.phase not in OPERATOR_INFRASTRUCTURE_SUSPENSION_ACTIVE_PHASES:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires a coherent active ALPHA_RESEARCH phase"
        )
    if state.setup is None or state.context_packet is None or state.platform_readiness is None:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires setup, context packet, and "
            "pinned platform readiness"
        )
    if state.context_packet.research_mode is not ResearchMode.ALPHA_RESEARCH:
        raise AutoresearchValidationError(
            "operator infrastructure suspension requires an ALPHA_RESEARCH context packet"
        )

    decision = FinalDecisionArtifact(
        experiment_id=_canonical_iteration_experiment_id(state.iteration),
        decision=FinalDecision.INFRA_BLOCKED,
        recommended_metric_name=OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME,
        recommended_metric_value=None,
        reviewer_verdict=(
            FinalReviewerVerdict(state.latest_review.verdict.value)
            if state.latest_review is not None
            else FinalReviewerVerdict.NOT_RUN
        ),
        rationale=OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE,
        log_summary=OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY,
        continue_loop=True,
        memory_write_required=False,
        infra_rationale=reason,
    )
    return replace(
        state,
        phase=Phase.REPEAT,
        pending_fix_trigger=None,
        final_decision=decision,
        memory_written=False,
        memory_verification_receipt=None,
        suspended=True,
        suspension_reason=reason,
    )


def start_next_iteration(
    state: AutoresearchState,
    *,
    readiness: PlatformReadinessManifest | None = None,
) -> AutoresearchState:
    """Begin a completed iteration's successor with a newly validated READY receipt."""
    if state.setup is None or state.final_decision is None:
        raise AutoresearchValidationError("next iteration requires completed current iteration")
    _validate_operator_precondition_infra_blocked_suspension(state)
    if state.suspended:
        raise AutoresearchValidationError(
            "suspended INFRA_BLOCKED state requires explicit autoresearch-resume"
        )
    if state.phase is not Phase.REPEAT:
        raise AutoresearchValidationError("start_next_iteration requires a completed repeat phase")
    if readiness is None:
        raise AutoresearchValidationError(
            "start_next_iteration requires an explicit platform readiness manifest"
        )
    if state.platform_readiness is None:
        raise AutoresearchValidationError(
            "autoresearch state has no pinned platform readiness receipt; "
            "run autoresearch-pin-readiness explicitly before dispatch"
        )
    if not state.memory_written and not _is_explicit_no_memory_transition(state):
        raise AutoresearchValidationError(
            "cannot start next iteration before a verified MemPalace write or a "
            "policy-approved no-memory final decision"
        )
    try:
        readiness_identity = readiness.require_ready()
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    return AutoresearchState(
        phase=Phase.SETUP_CONTEXT,
        iteration=state.iteration + 1,
        setup=state.setup,
        platform_readiness=readiness_identity,
    )


def pin_platform_readiness(
    state: AutoresearchState,
    readiness: PlatformReadinessManifest,
) -> AutoresearchState:
    """Initialize readiness or repin an active state to the same contract IDs."""
    try:
        identity = readiness.require_ready()
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    if state.suspended:
        raise AutoresearchValidationError(
            "suspended readiness must continue through autoresearch-resume"
        )
    pinned = state.platform_readiness
    if pinned is not None and (
        pinned.manifest_id != identity.manifest_id or pinned.snapshot_id != identity.snapshot_id
    ):
        raise AutoresearchValidationError(
            "state readiness manifest_id or snapshot_id changed; same-ID repin required"
        )
    return replace(state, platform_readiness=identity)


def resume_suspended_iteration(
    state: AutoresearchState,
    readiness: PlatformReadinessManifest,
) -> AutoresearchState:
    """Explicitly recheck readiness and resume after a durable infrastructure pause."""
    if not state.suspended or state.final_decision is None:
        raise AutoresearchValidationError("autoresearch state is not suspended")
    if state.final_decision.decision is not FinalDecision.INFRA_BLOCKED:
        raise AutoresearchValidationError("only an INFRA_BLOCKED state can be resumed explicitly")
    if state.setup is None:
        raise AutoresearchValidationError("suspended state is missing setup context")
    try:
        identity = readiness.require_ready()
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    if (
        state.final_decision.recommended_metric_name
        == OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME
    ):
        if not _is_operator_infrastructure_suspension_state(state):
            raise AutoresearchValidationError(
                "operator infrastructure suspension state has an invalid contract"
            )
        if state.platform_readiness is None:
            raise AutoresearchValidationError(
                "operator infrastructure suspension has no pinned readiness identity to replace"
            )
        if state.platform_readiness == identity:
            raise AutoresearchValidationError(
                "autoresearch-resume requires a changed READY platform readiness manifest"
            )
    return AutoresearchState(
        phase=Phase.SETUP_CONTEXT,
        iteration=state.iteration + 1,
        setup=state.setup,
        platform_readiness=identity,
    )


def _rewrite_workspace_prefix(value: str, *, old_root: Path, new_root: Path) -> str:
    path = Path(value).expanduser().resolve(strict=False)
    try:
        relative = path.relative_to(old_root)
    except ValueError as exc:
        raise AutoresearchValidationError(
            "legacy workspace migration can rewrite only paths under the retired worktree root"
        ) from exc
    return str(new_root / relative)


def _clone_legacy_workspace_for_state(
    *,
    legacy_workspace: Path,
    authoritative_checkout: Path,
    destination: Path,
    commit_sha: str,
) -> None:
    def validate_destination() -> None:
        workspace = _require_isolated_git_clone_root(
            destination,
            label="legacy workspace migration destination",
        )
        _require_artifact_origin_matches_target(
            workspace,
            authoritative_checkout,
            label="legacy workspace migration destination",
        )
        head = _resolve_git_commit(workspace, "HEAD", label="migrated workspace HEAD")
        if head != _resolve_git_commit(
            workspace,
            commit_sha,
            label="migrated workspace commit_sha",
        ):
            raise AutoresearchValidationError(
                "legacy workspace migration destination exists with a different HEAD"
            )
        authoritative_head = _resolve_git_commit(
            authoritative_checkout,
            "HEAD",
            label="authoritative target_repo HEAD",
        )
        _require_ancestor(
            workspace,
            authoritative_head,
            head,
            error_message=(
                "authoritative target_repo HEAD is not an ancestor of migrated workspace commit"
            ),
            missing_is_not_ancestor=True,
        )
        _require_clean_git_worktree(workspace)

    if destination.exists():
        validate_destination()
        return
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    _require_private_directory(destination.parent, label="legacy workspace migration root")
    _require_git_success(
        authoritative_checkout.parent,
        (
            "clone",
            "--no-hardlinks",
            "--no-local",
            str(authoritative_checkout),
            str(destination),
        ),
        operation="clone legacy autoresearch workspace",
    )
    destination.chmod(0o700)
    (destination / ".git").chmod(0o700)
    _require_artifact_origin_matches_target(
        destination,
        authoritative_checkout,
        label="legacy workspace migration destination",
    )
    _require_git_success(
        destination,
        ("fetch", "origin", commit_sha),
        operation="fetch migrated autoresearch workspace commit",
    )
    _require_git_success(
        destination,
        ("checkout", "--detach", commit_sha),
        operation="checkout migrated autoresearch workspace commit",
    )
    _remove_group_world_write_bits(destination)
    destination.chmod(0o700)
    (destination / ".git").chmod(0o700)
    validate_destination()


def _remove_group_world_write_bits(path: Path) -> None:
    for current_root, directory_names, file_names in os.walk(path):
        root_path = Path(current_root)
        if root_path.name == ".git":
            directory_names[:] = []
            continue
        root_mode = stat.S_IMODE(root_path.lstat().st_mode)
        root_path.chmod(root_mode & ~0o022)
        for file_name in file_names:
            file_path = root_path / file_name
            file_metadata = file_path.lstat()
            if stat.S_ISLNK(file_metadata.st_mode):
                continue
            file_mode = stat.S_IMODE(file_metadata.st_mode)
            file_path.chmod(file_mode & ~0o022)


def _require_legacy_linked_worktree_from_authoritative_checkout(
    legacy_workspace: Path,
    authoritative_checkout: Path,
) -> None:
    git_file = legacy_workspace / ".git"
    try:
        git_metadata = git_file.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(
            "legacy workspace migration requires linked worktree .git metadata"
        ) from exc
    if stat.S_ISLNK(git_metadata.st_mode) or not stat.S_ISREG(git_metadata.st_mode):
        raise AutoresearchValidationError(
            "legacy workspace migration requires source linked worktree metadata"
        )
    common_dir = Path(
        _require_git_output(
            legacy_workspace,
            ("rev-parse", "--path-format=absolute", "--git-common-dir"),
            operation="legacy linked worktree common git dir check",
        )
    ).resolve(strict=True)
    authoritative_git_dir = Path(
        _require_git_output(
            authoritative_checkout,
            ("rev-parse", "--path-format=absolute", "--git-common-dir"),
            operation="authoritative target_repo common git dir check",
        )
    ).resolve(strict=True)
    if common_dir != authoritative_git_dir:
        raise AutoresearchValidationError(
            "legacy workspace migration source must share authoritative target_repo Git metadata"
        )


def migrate_legacy_autoresearch_workspace_state_file(
    state_path: Path,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState:
    """Move a pre-isolated-clone active state onto the controller-owned workspace root.

    This is intentionally not exposed as a model artifact transition. It exists so a
    live state carrying the retired linked-worktree path can resume without allowing
    another model write to that legacy workspace.
    """
    resolved_state_path = state_path.expanduser().resolve(strict=False)
    with _exclusive_state_lock(resolved_state_path):
        state = load_state_file(resolved_state_path)
        if not state_has_legacy_autoresearch_workspace(state):
            return state
        implementation = state.implementation_result
        if implementation is None:
            raise AutoresearchValidationError(
                "legacy workspace migration requires implementation_result"
            )
        old_root = LEGACY_AUTORESEARCH_WORKTREE_ROOT.resolve(strict=False)
        old_workspace = Path(implementation.workspace_path).expanduser().resolve(strict=True)
        if not _path_under_root(old_workspace, old_root):
            raise AutoresearchValidationError(
                "legacy workspace migration requires implementation_result under retired root"
            )
        legacy_root_metadata = old_root.lstat()
        if old_root.is_symlink() or not stat.S_ISDIR(legacy_root_metadata.st_mode):
            raise AutoresearchValidationError("legacy workspace root must be a plain directory")
        old_workspace = _require_git_worktree_root(
            old_workspace,
            label="legacy implementation_result workspace_path",
        )
        if state.setup is None:
            raise AutoresearchValidationError(
                "legacy workspace migration requires setup target_repo"
            )
        authoritative_checkout = _require_git_worktree_root(
            Path(state.setup.target_repo).expanduser(),
            label="authoritative target_repo",
        )
        _require_legacy_linked_worktree_from_authoritative_checkout(
            old_workspace,
            authoritative_checkout,
        )
        _require_clean_git_worktree(old_workspace)
        implementation_commit = _resolve_git_commit(
            old_workspace,
            implementation.commit_sha,
            label="legacy implementation_result commit_sha",
        )
        _resolve_git_commit(
            authoritative_checkout,
            implementation_commit,
            label="authoritative target_repo object database implementation commit",
        )
        if _resolve_git_commit(old_workspace, "HEAD", label="legacy workspace HEAD") != (
            implementation_commit
        ):
            raise AutoresearchValidationError(
                "legacy workspace migration requires implementation commit at HEAD"
            )
        new_root = DEFAULT_AUTORESEARCH_WORKTREE_ROOT.resolve(strict=False)
        new_workspace = new_root / old_workspace.relative_to(old_root)
        _clone_legacy_workspace_for_state(
            legacy_workspace=old_workspace,
            authoritative_checkout=authoritative_checkout,
            destination=new_workspace,
            commit_sha=implementation_commit,
        )
        migrated_implementation = replace(
            implementation,
            workspace_path=str(new_workspace),
            experiment_manifest_path=_rewrite_workspace_prefix(
                implementation.experiment_manifest_path,
                old_root=old_root,
                new_root=new_root,
            ),
        )
        migrated_fixes = tuple(
            replace(
                fix,
                workspace_path=_rewrite_workspace_prefix(
                    fix.workspace_path,
                    old_root=old_root,
                    new_root=new_root,
                ),
            )
            for fix in state.fix_history
        )
        migrated = replace(
            state,
            implementation_result=migrated_implementation,
            fix_history=migrated_fixes,
        )
        validate_artifact_workspace(
            replace(migrated, implementation_result=None, fix_history=()),
            migrated_implementation,
        )
        for fix in migrated.fix_history:
            validate_artifact_workspace(migrated, fix)
        _validate_state(migrated, policy, validation_context)
        _atomic_save_state_file(resolved_state_path, migrated)
        return migrated


def load_artifact_file(
    path: Path,
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    *,
    instruction_manifest_sha256: str,
    validation_context: AutoresearchValidationContext | None = None,
    state_reference_sha256: str | None = None,
    state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
) -> (
    SetupContextArtifact
    | ContextPacketArtifact
    | DebateResultArtifact
    | ConsensusResultArtifact
    | ImplementationResultArtifact
    | VerificationResultArtifact
    | ReviewResultArtifact
    | FixResultArtifact
    | FinalDecisionArtifact
):
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise AutoresearchValidationError(f"missing artifact file: {path}") from exc
    if len(raw_bytes) > MAX_ARTIFACT_FILE_BYTES:
        raise AutoresearchValidationError(
            "artifact file exceeds hard byte budget: "
            f"{len(raw_bytes)} > {MAX_ARTIFACT_FILE_BYTES} bytes; compact the "
            "phase artifact and write the strict manifest/state-reference envelope"
        )
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise AutoresearchValidationError(f"artifact JSON is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchValidationError(f"invalid artifact JSON: {path}") from exc

    _validate_sha256(instruction_manifest_sha256, label="instruction_manifest_sha256")
    if state_reference_sha256 is not None:
        _validate_sha256(state_reference_sha256, label="state_reference_sha256")
    data = _ensure_mapping(raw, label="artifact_file")
    _require_exact_keys(
        data,
        label="artifact_file",
        expected=("instruction_manifest_sha256", "state_reference_sha256", "artifact"),
    )
    envelope_digest = _require_sha256(data, "instruction_manifest_sha256")
    if envelope_digest != instruction_manifest_sha256:
        raise AutoresearchValidationError(
            "artifact instruction_manifest_sha256 does not match dispatched manifest"
        )
    state = _validate_persisted_state_matches(state, state_path=state_path)
    expected_state_reference_sha256 = build_authoritative_state_reference(
        state,
        state_path=state_path,
    ).sha256()
    envelope_state_reference_sha256 = _require_sha256(data, "state_reference_sha256")
    if (
        state_reference_sha256 is not None
        and envelope_state_reference_sha256 != state_reference_sha256
    ):
        raise AutoresearchValidationError(
            "artifact state_reference_sha256 does not match dispatched state reference"
        )
    if envelope_state_reference_sha256 != expected_state_reference_sha256:
        raise AutoresearchValidationError(
            "artifact state_reference_sha256 does not match the current authoritative state"
        )
    artifact_raw = data["artifact"]

    _validate_state(state, policy, validation_context)
    target = _select_phase_target(state, policy)
    if target.artifact_type is ArtifactType.SETUP:
        return SetupContextArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.CONTEXT_PACKET:
        return ContextPacketArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.DEBATE_RESULT:
        return DebateResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.CONSENSUS_RESULT:
        return ConsensusResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.IMPLEMENTATION_RESULT:
        return ImplementationResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.VERIFICATION_RESULT:
        return VerificationResultArtifact.from_dict(artifact_raw, mode=state.mode)
    if target.artifact_type is ArtifactType.REVIEW_RESULT:
        return ReviewResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.FIX_RESULT:
        return FixResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.FINAL_DECISION:
        return FinalDecisionArtifact.from_dict(artifact_raw)
    raise AutoresearchValidationError(
        f"{state.phase.value} does not accept artifact files; use state mutation commands instead"
    )


def load_state_file(path: Path) -> AutoresearchState:
    return AutoresearchState.from_dict(_load_state_raw(path))


def _load_state_raw(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchValidationError(f"missing state file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchValidationError(f"invalid state JSON: {path}") from exc
    return _ensure_mapping(raw, label="autoresearch_state")


def retry_external_verification_state_file(
    state_path: Path,
    probe: ResearchPanelProbeReceipt,
    *,
    operator_reason: str,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
) -> AutoresearchState:
    """Atomically authorize one bounded operator retry of the current panel failure."""
    resolved_path = state_path.expanduser().resolve(strict=False)
    with _exclusive_state_locks((resolved_path,)):
        raw = _load_state_raw(resolved_path)
        schema_version = _require_int(raw, "schema_version")
        if schema_version != AUTORESEARCH_STATE_SCHEMA_VERSION:
            raise AutoresearchValidationError(
                "external verification retry accepts only the compatible schema-v4 state"
            )
        state = AutoresearchState.from_dict(raw)
        _validate_state(state, policy, validation_context)
        state = _materialize_attested_pending_retry_failure(
            state,
            policy=policy,
            validation_context=validation_context,
        )
        prior_verification = state.latest_verification
        if prior_verification is None:
            raise AutoresearchValidationError(
                "external verification retry requires a preserved verification artifact"
            )
        _validate_quantipy_experiment_evidence(
            state,
            prior_verification,
            validation_context=validation_context,
        )
        receipt = ExternalVerificationRetryReceipt.for_state(state, probe, operator_reason)
        retried = retry_external_verification(state, receipt)
        _validate_state(retried, policy, validation_context)
        _atomic_save_state_file(resolved_path, retried)
        return retried


def _default_systemd_is_active(unit: str) -> bool:
    def run_command(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutoresearchValidationError(
                f"cannot inspect interrupted detached systemd unit {unit}: {exc}"
            ) from exc

    try:
        return systemd_unit_is_active(unit, run_command=run_command)
    except SystemdUnitStateError as exc:
        raise AutoresearchValidationError(
            f"cannot prove interrupted detached systemd unit is inactive: {unit}"
        ) from exc


def _detached_pid_is_alive(pid: int | None, *, proc_root: Path) -> bool:
    if pid is None:
        return False
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AutoresearchValidationError(
            f"cannot inspect interrupted detached process {pid}: {exc}"
        ) from exc
    closing = raw.rfind(")")
    fields = raw[closing + 1 :].split() if closing >= 0 else []
    if not fields:
        raise AutoresearchValidationError(
            f"malformed process stat for interrupted detached process {pid}"
        )
    return fields[0] != "Z"


@dataclass(frozen=True, slots=True)
class _SealedInterruptedRunAttestation:
    """Immutable detached evidence required before interrupted-v3 publication."""

    manifest_sha256: str
    status_sha256: str
    stdout_sha256: str
    stderr_sha256: str


def _attest_sealed_interrupted_run(record: RunRecord) -> _SealedInterruptedRunAttestation:
    capture = record.status.output_capture
    if capture is None:
        raise AutoresearchValidationError(
            "interrupted verification recovery requires sealed worker output capture"
        )
    return _SealedInterruptedRunAttestation(
        manifest_sha256=record.status.manifest_sha256,
        status_sha256=_canonical_json_digest(record.status.to_dict()),
        stdout_sha256=capture.stdout.sha256,
        stderr_sha256=capture.stderr.sha256,
    )


def _reattest_sealed_interrupted_run(
    record: RunRecord,
    *,
    runs_root: Path,
    expected: _SealedInterruptedRunAttestation,
) -> None:
    """Re-read the sealed record and capture tails before state publication."""
    import gateway.autoresearch_runs as detached_runs

    try:
        current = detached_runs.read_run_record(
            run_dir=record.run_directory,
            runs_root=runs_root,
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "interrupted verification recovery cannot re-attest the sealed detached v3 record"
        ) from exc
    if _attest_sealed_interrupted_run(current) != expected:
        raise AutoresearchValidationError(
            "interrupted verification recovery sealed detached v3 evidence changed"
        )


def _require_absent_interrupted_run_artifact(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise AutoresearchValidationError(
            "interrupted verification recovery requires the v3 run.json to be absent"
        )


def _find_exact_platform_v4_detached_run(
    *,
    runs_root: Path,
    iteration: int,
    directory_name: str,
    task_label: str,
    state_reference_sha256: str,
) -> RunRecord:
    """Select exactly one sealed v4 record; duplicates are a fail-closed ambiguity."""
    import gateway.autoresearch_runs as detached_runs

    expected_directory = runs_root / directory_name
    try:
        record = detached_runs.read_run_record(run_dir=expected_directory, runs_root=runs_root)
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "platform runtime recovery expected detached v4 record is unavailable or invalid"
        ) from exc
    manifest = record.manifest
    if (
        manifest.phase is not Phase.VERIFICATION
        or manifest.iteration != iteration
        or manifest.attempt != 4
        or manifest.task_label != task_label
        or manifest.state_reference_sha256 != state_reference_sha256
    ):
        raise AutoresearchValidationError(
            "platform runtime recovery expected detached v4 manifest identity is invalid"
        )
    duplicate_count = 0
    for directory, _children, files in os.walk(runs_root, followlinks=False):
        if "manifest.json" not in files:
            continue
        candidate = Path(directory)
        try:
            candidate_manifest = detached_runs.read_run_manifest(
                run_dir=candidate, runs_root=runs_root
            )
        except (OSError, ValueError):
            continue
        if (
            candidate_manifest.phase is Phase.VERIFICATION
            and candidate_manifest.iteration == iteration
            and candidate_manifest.attempt == 4
            and candidate_manifest.task_label == task_label
            and candidate_manifest.state_reference_sha256 == state_reference_sha256
        ):
            duplicate_count += 1
    if duplicate_count != 1:
        raise AutoresearchValidationError(
            "platform runtime recovery found duplicate expected detached v4 identities"
        )
    return record


def _require_absent_platform_v5_identity(
    *,
    run_id: str,
    iteration: int,
    implementation_commit: str,
) -> None:
    """Refuse recovery if any v5 artifact or detached identity already exists."""
    artifact_directory = DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / run_id
    try:
        artifact_directory.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            "platform runtime recovery cannot inspect the v5 artifact directory"
        ) from exc
    else:
        raise AutoresearchValidationError(
            "platform runtime recovery requires the v5 artifact directory to be absent"
        )
    import gateway.autoresearch_runs as detached_runs

    runs_root = detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT
    directory = runs_root / (f"i{iteration}-verification-r1-a5-{implementation_commit[:12]}-v5")
    try:
        directory.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            "platform runtime recovery cannot inspect the v5 detached identity"
        ) from exc
    else:
        raise AutoresearchValidationError(
            "platform runtime recovery requires the v5 detached identity to be absent"
        )
    expected_artifact = str(artifact_directory / "run.json")
    for raw_directory, _children, files in os.walk(runs_root, followlinks=False):
        if "manifest.json" not in files:
            continue
        candidate = Path(raw_directory)
        try:
            manifest = detached_runs.read_run_manifest(run_dir=candidate, runs_root=runs_root)
        except (OSError, ValueError):
            continue
        if (
            manifest.iteration == iteration
            and manifest.phase is Phase.VERIFICATION
            and manifest.attempt == 5
            and manifest.expected_artifact_path == expected_artifact
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery found a duplicate detached v5 identity"
            )


def _require_unchanged_platform_runtime_recovery_state(
    state_path: Path,
    expected: AutoresearchState,
) -> None:
    """Reject an out-of-lock state-file write before v5 authorization publishes."""
    if load_state_file(state_path) != expected:
        raise AutoresearchValidationError(
            "platform runtime recovery state changed before publication"
        )


def _platform_runtime_recovery_identity_contexts(
    state: AutoresearchState,
    validation_context: AutoresearchValidationContext | None,
) -> tuple[AutoresearchValidationContext, ReadinessIdentity]:
    """Authorize only the historical three-field identity's exact v4→v5 upgrade."""
    if validation_context is None:
        raise AutoresearchValidationError(
            "platform runtime recovery requires the exact readiness-pinned Quantipy commit"
        )
    historical_identity = state.platform_readiness
    current_identity = validation_context.readiness_identity
    if (
        historical_identity is None
        or current_identity is None
        or current_identity.quantipy_commit is None
        or validation_context.quantipy_commit != current_identity.quantipy_commit
    ):
        raise AutoresearchValidationError(
            "platform runtime recovery requires the exact readiness-pinned Quantipy commit"
        )
    if historical_identity == current_identity:
        return validation_context, current_identity
    if historical_identity.quantipy_commit is not None or historical_identity != replace(
        current_identity, quantipy_commit=None
    ):
        raise AutoresearchValidationError(
            "platform runtime recovery readiness identity may differ only by the current "
            "nonnull Quantipy commit"
        )
    return (
        replace(validation_context, readiness_identity=historical_identity),
        current_identity,
    )


def recover_platform_runtime_state_file(
    state_path: Path,
    *,
    probe: ResearchPanelProbeReceipt,
    operator_reason: str,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None = None,
    systemd_is_active: Callable[[str], bool] | None = None,
    proc_root: Path = Path("/proc"),
) -> AutoresearchState:
    """Materialize only the sealed v4 panel-receipt failure and authorize canonical v5."""
    publication_path = state_path.expanduser().resolve(strict=False)
    authoritative_path = DEFAULT_AUTORESEARCH_STATE_PATH.expanduser().resolve(strict=False)
    unit_is_active = systemd_is_active or _default_systemd_is_active
    with _exclusive_state_locks((authoritative_path, publication_path)):
        try:
            authoritative_bytes = authoritative_path.read_bytes()
            publication_bytes = publication_path.read_bytes()
        except OSError as exc:
            raise AutoresearchValidationError(
                "platform runtime recovery cannot read the authoritative state copy"
            ) from exc
        if publication_bytes != authoritative_bytes:
            raise AutoresearchValidationError(
                "platform runtime recovery output must be a byte-exact copy of the "
                "authoritative state"
            )
        state = load_state_file(authoritative_path)
        historical_validation_context, current_readiness_identity = (
            _platform_runtime_recovery_identity_contexts(state, validation_context)
        )
        _validate_state(state, policy, historical_validation_context)
        receipt = state.external_verification_retry_receipt
        implementation = state.implementation_result
        if (
            state.phase is not Phase.VERIFICATION
            or state.mode is not ResearchMode.ALPHA_RESEARCH
            or receipt is None
            or receipt.schema_version != INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
            or receipt.retry_attempt != 4
            or implementation is None
            or len(state.interrupted_verification_history) != 1
            or len(state.verification_history) != 2
            or state.platform_runtime_recovery_receipt is not None
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery accepts only the exact pending v4 topology"
            )
        if validation_context is None or validation_context.quantipy_commit is None:
            raise AutoresearchValidationError(
                "platform runtime recovery requires the exact readiness-pinned Quantipy commit"
            )
        if not operator_reason or operator_reason.strip() != operator_reason:
            raise AutoresearchValidationError(
                "platform runtime recovery requires a trimmed operator reason"
            )
        expected_v4_run_id = _deterministic_quantipy_run_id(
            state.iteration, implementation.commit_sha, attempt=4
        )
        expected_v5_run_id = _deterministic_quantipy_run_id(
            state.iteration, implementation.commit_sha, attempt=5
        )
        _require_absent_platform_v5_identity(
            run_id=expected_v5_run_id,
            iteration=state.iteration,
            implementation_commit=implementation.commit_sha,
        )
        if receipt.expected_run_id != expected_v4_run_id:
            raise AutoresearchValidationError(
                "platform runtime recovery v4 retry receipt identity is stale"
            )
        state_reference_sha256 = build_authoritative_state_reference(
            state, state_path=authoritative_path
        ).sha256()
        task_label = (
            f"autoresearch-i{state.iteration}-verification-r1-a4-"
            f"{implementation.commit_sha[:12]}-v4"
        )
        directory_name = (
            f"i{state.iteration}-verification-r1-a4-{implementation.commit_sha[:12]}-v4"
        )
        import gateway.autoresearch_runs as detached_runs

        record = _find_exact_platform_v4_detached_run(
            runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
            iteration=state.iteration,
            directory_name=directory_name,
            task_label=task_label,
            state_reference_sha256=state_reference_sha256,
        )
        expected_run_path = DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / expected_v4_run_id / "run.json"
        expected_command = _legacy_quantipy_bash_command(implementation, run_id=expected_v4_run_id)
        manifest = record.manifest
        status = record.status
        if (
            manifest.working_directory != implementation.workspace_path
            or manifest.expected_artifact_path != str(expected_run_path)
            or manifest.command_sha256 != detached_runs.command_sha256(expected_command)
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery detached manifest is not the exact historical v4 command"
            )
        capture = status.output_capture
        if (
            status.state is not detached_runs.RunState.FAILED
            or status.exit_code != 1
            or status.signal_number is not None
            or status.failure_classification
            is not detached_runs.RunFailureClassification.PROCESS_ERROR
            or status.systemd_unit is None
            or OPENCLAW_LONG_TASK_UNIT_RE.fullmatch(status.systemd_unit) is None
            or capture is None
            or any(
                stream.truncated
                or not stream.eof_observed
                or stream.bytes_observed != stream.bytes_stored
                for stream in (capture.stdout, capture.stderr)
            )
            or status.expected_artifact_attestation_status
            is not detached_runs.ExpectedArtifactAttestationStatus.ATTESTED
            or status.expected_artifact_attestation is None
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery requires sealed v4 process_error/exit-1 "
                "EOF artifact evidence"
            )
        run_snapshot = _secure_open_snapshot(
            expected_run_path,
            label="sealed v4 Quantipy run.json",
            trusted_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            private=True,
            max_bytes=QUANTIPY_RUN_ENVELOPE_MAX_BYTES - 1,
        )
        if (
            status.expected_artifact_attestation.path != str(expected_run_path)
            or status.expected_artifact_attestation.size_bytes != len(run_snapshot.content)
            or status.expected_artifact_attestation.sha256 != run_snapshot.sha256
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery v4 run.json does not match detached artifact attestation"
            )
        run = _validate_quantipy_run_envelope(run_snapshot)
        failure = _run_failure_from_mapping(run["failure"])
        if (
            run["run_id"] != expected_v4_run_id
            or run["success"] is not False
            or run["panel_requested"] is not True
            or run["panel"] is not None
            or run["stage_receipts"]
            or failure is None
            or failure.category != "panel"
            or failure.message != "ExperimentPanelError: Research panel receipt is invalid."
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery requires the exact v4 panel receipt failure"
            )
        workspace = _require_strict_canonical_workspace_path(
            implementation.workspace_path, label="implementation_result workspace_path"
        )
        _require_clean_git_worktree(workspace)
        commit = _resolve_git_commit(
            workspace, implementation.commit_sha, label="implementation_result commit_sha"
        )
        if (
            _resolve_git_commit(workspace, "HEAD", label="implementation_result workspace HEAD")
            != commit
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery implementation worktree HEAD must equal its commit"
            )
        manifest_snapshot = _secure_open_snapshot(
            implementation.experiment_manifest_path,
            label="implementation_result experiment_manifest_path",
        )
        experiment_manifest = _validate_quantipy_v2_manifest(
            manifest_snapshot,
            workspace=workspace,
            commit_sha=commit,
            expected_sha256=implementation.experiment_manifest_sha256,
        )
        if run["manifest_sha256"] != _canonical_quantipy_manifest_sha256(experiment_manifest):
            raise AutoresearchValidationError(
                "platform runtime recovery v4 run.json manifest does not match immutable source"
            )
        source = run["source"]
        if source is None:
            raise AutoresearchValidationError(
                "platform runtime recovery v4 run.json requires source inventory"
            )
        _validate_quantipy_execution_source_against_commit(
            _ensure_mapping(source, label="sealed v4 Quantipy run.json source"),
            manifest=experiment_manifest,
            source_root=manifest_snapshot.path.parent,
            workspace=workspace,
            commit_sha=commit,
        )
        prior = state.latest_verification
        assert prior is not None
        v4_evidence = QuantipyExperimentEvidence(
            manifest_path=implementation.experiment_manifest_path,
            manifest_sha256=implementation.experiment_manifest_sha256,
            detached_run_directory=str(record.run_directory),
            detached_run_manifest_sha256=status.manifest_sha256,
            run_id=expected_v4_run_id,
            run_json_path=str(expected_run_path),
            run_json_sha256=run_snapshot.sha256,
            success=False,
            completed_stages=(),
            terminal_stage=None,
            terminal_status=None,
            failure=failure,
            panel=None,
        )
        v4 = replace(
            prior,
            status=VerificationStatus.TEST_FAILURE,
            is_walk_forward_sharpe_net=None,
            oos_sharpe_net=None,
            max_drawdown_pct=None,
            win_rate=None,
            trade_count=None,
            trades_per_day=None,
            oos_trading_days=None,
            feature_importances_summary="runner-owned sealed v4 platform runtime evidence",
            null_test_summary="ExperimentPanelError: Research panel receipt is invalid.",
            bug_signals=(),
            tests_passed=False,
            commands_run=(),
            data_coverage=None,
            platform_coverage_validation=None,
            infra_gate_outcome=None,
            infra_rationale=None,
            universe_verification_receipt=None,
            price_hydration_receipt=None,
            quantipy_experiment_evidence=v4_evidence,
            quantipy_execution_not_started=None,
        )
        v4.validate(mode=state.mode)
        history = (*state.verification_history, v4)
        history_sha256 = tuple(_canonical_json_digest(item.to_dict()) for item in history)
        interruption = state.interrupted_verification_history[0]
        _require_absent_interrupted_run_artifact(
            DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / interruption.expected_run_id / "run.json"
        )
        runtime = _attest_canonical_quantipy_runtime(
            state,
            implementation,
            readiness_quantipy_commit=validation_context.quantipy_commit,
        )
        v5 = ExternalVerificationRetryReceipt(
            expected_run_id=_deterministic_quantipy_run_id(
                state.iteration, implementation.commit_sha, attempt=5
            ),
            prior_verification_sha256=history_sha256[-1],
            probe=probe,
            retry_attempt=5,
            implementation_commit=implementation.commit_sha,
            manifest_sha256=implementation.experiment_manifest_sha256,
            readiness_manifest_id=receipt.readiness_manifest_id,
            readiness_snapshot_id=receipt.readiness_snapshot_id,
            operator_reason=operator_reason,
            verification_history_sha256=history_sha256,
            interruption_history_sha256=(_canonical_json_digest(interruption.to_dict()),),
            schema_version=INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        )
        v5_contract = build_quantipy_execution_contract(
            runtime_root=Path(runtime.root),
            manifest_path=Path(implementation.experiment_manifest_path),
            output_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            run_id=v5.expected_run_id,
        )
        recovery = PlatformRuntimeRecoveryReceipt(
            expected_run_id=v5.expected_run_id,
            implementation_commit=implementation.commit_sha,
            implementation_manifest_sha256=implementation.experiment_manifest_sha256,
            verification_history_sha256=history_sha256,
            interruption_sha256=_canonical_json_digest(interruption.to_dict()),
            prior_retry_receipt_sha256=_canonical_json_digest(
                interruption.prior_retry_receipt.to_dict()
            ),
            v4_verification_sha256=history_sha256[-1],
            v4_detached_run_manifest_sha256=status.manifest_sha256,
            v4_detached_run_status_sha256=_canonical_json_digest(status.to_dict()),
            old_worktree_runtime_commit=commit,
            runtime=runtime,
            execution_command_sha256=hashlib.sha256(
                "\0".join(v5_contract.command).encode("utf-8")
            ).hexdigest(),
            probe=probe,
            operator_reason=operator_reason,
        )
        recovered = replace(
            state,
            platform_readiness=current_readiness_identity,
            verification_history=history,
            external_verification_retry_receipt=v5,
            platform_runtime_recovery_receipt=recovery,
            canonical_quantipy_runtime_attestation=runtime,
            phase=Phase.VERIFICATION,
            pending_fix_trigger=None,
        )
        _validate_state(recovered, policy, validation_context)
        if status.systemd_unit is not None and unit_is_active(status.systemd_unit):
            raise AutoresearchValidationError(
                "platform runtime recovery refuses an active detached systemd unit"
            )
        if _detached_pid_is_alive(status.pid, proc_root=proc_root):
            raise AutoresearchValidationError(
                "platform runtime recovery refuses a live detached process"
            )
        # Re-read every mutable external fact immediately before the atomic publication.
        current = detached_runs.read_run_record(
            run_dir=record.run_directory, runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT
        )
        if (
            _canonical_json_digest(current.status.to_dict())
            != recovery.v4_detached_run_status_sha256
            or current.status.manifest_sha256 != recovery.v4_detached_run_manifest_sha256
            or _secure_open_snapshot(
                expected_run_path,
                label="sealed v4 Quantipy run.json",
                trusted_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
                private=True,
                max_bytes=QUANTIPY_RUN_ENVELOPE_MAX_BYTES - 1,
            ).sha256
            != v4_evidence.run_json_sha256
            or _attest_canonical_quantipy_runtime(
                state,
                implementation,
                readiness_quantipy_commit=validation_context.quantipy_commit,
            )
            != runtime
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery evidence changed before state publication"
            )
        if (
            status.systemd_unit is None
            or unit_is_active(status.systemd_unit)
            or _detached_pid_is_alive(status.pid, proc_root=proc_root)
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery detached process became active before state publication"
            )
        _require_absent_interrupted_run_artifact(
            DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / interruption.expected_run_id / "run.json"
        )
        _require_absent_platform_v5_identity(
            run_id=expected_v5_run_id,
            iteration=state.iteration,
            implementation_commit=implementation.commit_sha,
        )
        _require_clean_git_worktree(workspace)
        if (
            _resolve_git_commit(workspace, "HEAD", label="implementation_result workspace HEAD")
            != commit
        ):
            raise AutoresearchValidationError(
                "platform runtime recovery implementation worktree changed before publication"
            )
        current_manifest_snapshot = _secure_open_snapshot(
            implementation.experiment_manifest_path,
            label="implementation_result experiment_manifest_path",
        )
        current_manifest = _validate_quantipy_v2_manifest(
            current_manifest_snapshot,
            workspace=workspace,
            commit_sha=commit,
            expected_sha256=implementation.experiment_manifest_sha256,
        )
        if current_manifest != experiment_manifest:
            raise AutoresearchValidationError(
                "platform runtime recovery implementation manifest changed before publication"
            )
        _validate_quantipy_execution_source_against_commit(
            _ensure_mapping(source, label="sealed v4 Quantipy run.json source"),
            manifest=current_manifest,
            source_root=current_manifest_snapshot.path.parent,
            workspace=workspace,
            commit_sha=commit,
        )
        _require_absent_platform_v5_identity(
            run_id=expected_v5_run_id,
            iteration=state.iteration,
            implementation_commit=implementation.commit_sha,
        )
        try:
            if authoritative_path.read_bytes() != authoritative_bytes:
                raise AutoresearchValidationError(
                    "platform runtime recovery authoritative state changed before publication"
                )
            if publication_path.read_bytes() != publication_bytes:
                raise AutoresearchValidationError(
                    "platform runtime recovery output state changed before publication"
                )
        except OSError as exc:
            raise AutoresearchValidationError(
                "platform runtime recovery cannot re-read the output state copy"
            ) from exc
        _require_unchanged_platform_runtime_recovery_state(authoritative_path, state)
        _atomic_save_state_file(publication_path, recovered)
        return recovered


def recover_interrupted_verification_state_file(
    state_path: Path,
    *,
    operator_reason: str,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    validation_context: AutoresearchValidationContext | None = None,
    systemd_is_active: Callable[[str], bool] | None = None,
    proc_root: Path = Path("/proc"),
) -> AutoresearchState:
    """Record the one operator-stopped v3 attempt and authorize deterministic v4.

    This deliberately does not materialize a VerificationResultArtifact: no
    Quantipy envelope was produced for the stopped process.
    """
    resolved_path = state_path.expanduser().resolve(strict=False)
    unit_is_active = systemd_is_active or _default_systemd_is_active
    with _exclusive_state_locks((resolved_path,)):
        state = load_state_file(resolved_path)
        _validate_state(state, policy, validation_context)
        receipt = state.external_verification_retry_receipt
        implementation = state.implementation_result
        if (
            state.phase is not Phase.VERIFICATION
            or state.mode is not ResearchMode.ALPHA_RESEARCH
            or receipt is None
            or receipt.schema_version != EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION
            or receipt.retry_attempt != 3
            or implementation is None
            or state.interrupted_verification_history
            or len(state.verification_history) != 2
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery accepts only the exact pending v3 topology"
            )
        if not operator_reason or operator_reason.strip() != operator_reason:
            raise AutoresearchValidationError(
                "interrupted verification recovery requires a trimmed operator reason"
            )
        expected_run_id = _deterministic_quantipy_run_id(
            state.iteration, implementation.commit_sha, attempt=3
        )
        if receipt.expected_run_id != expected_run_id:
            raise AutoresearchValidationError(
                "interrupted verification recovery retry receipt identity is stale"
            )
        expected_state_reference = build_authoritative_state_reference(
            state, state_path=resolved_path
        ).sha256()
        expected_run_path = DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / expected_run_id / "run.json"
        expected_task_label = (
            f"autoresearch-i{state.iteration}-verification-r1-a3-"
            f"{implementation.commit_sha[:12]}-v3"
        )
        expected_directory_name = (
            f"i{state.iteration}-verification-r1-a3-{implementation.commit_sha[:12]}-v3"
        )
        try:
            import gateway.autoresearch_runs as detached_runs

            record = _find_exact_interrupted_detached_run(
                runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
                iteration=state.iteration,
                directory_name=expected_directory_name,
                task_label=expected_task_label,
                state_reference_sha256=expected_state_reference,
            )
        except (OSError, ValueError) as exc:
            raise AutoresearchValidationError(
                "interrupted verification recovery cannot inspect detached run records"
            ) from exc
        expected_instruction_digest = record.manifest.instruction_manifest_sha256
        expected_command = _legacy_quantipy_bash_command(
            implementation,
            run_id=expected_run_id,
        )
        if (
            record.manifest.phase is not Phase.VERIFICATION
            or record.manifest.attempt != 3
            or record.manifest.task_label != expected_task_label
            or record.manifest.working_directory != implementation.workspace_path
            or record.manifest.expected_artifact_path != str(expected_run_path)
            or record.manifest.command_sha256 != detached_runs.command_sha256(expected_command)
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery detached manifest is not the exact v3 command"
            )
        status = record.status
        if (
            status.state is not detached_runs.RunState.FAILED
            or status.exit_code != 143
            or status.signal_number is not None
            or status.failure_classification
            is not detached_runs.RunFailureClassification.OPERATOR_STOPPED
            or status.systemd_unit is None
            or OPENCLAW_LONG_TASK_UNIT_RE.fullmatch(status.systemd_unit) is None
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery detached status is not terminal "
                "operator_stopped/143"
            )
        capture = status.output_capture
        if capture is None or any(
            stream.truncated
            or not stream.eof_observed
            or stream.bytes_observed != stream.bytes_stored
            for stream in (capture.stdout, capture.stderr)
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery requires complete sealed worker output capture"
            )
        if (
            status.expected_artifact_attestation_status
            is not detached_runs.ExpectedArtifactAttestationStatus.FAILED
            or status.expected_artifact_attestation_error
            is not detached_runs.ExpectedArtifactAttestationError.MISSING
            or status.expected_artifact_attestation is not None
        ):
            raise AutoresearchValidationError(
                "interrupted verification recovery requires a missing v3 artifact attestation"
            )
        sealed_attestation = _attest_sealed_interrupted_run(record)
        history_sha256 = tuple(
            _canonical_json_digest(artifact.to_dict()) for artifact in state.verification_history
        )
        interruption = InterruptedVerificationAttemptReceipt(
            expected_run_id=expected_run_id,
            interrupted_attempt=3,
            implementation_commit=implementation.commit_sha,
            implementation_manifest_sha256=implementation.experiment_manifest_sha256,
            detached_run_directory=str(record.run_directory),
            detached_run_manifest_sha256=status.manifest_sha256,
            detached_run_status_sha256=_canonical_json_digest(status.to_dict()),
            state_sha256=_canonical_json_digest(state.to_dict()),
            state_reference_sha256=expected_state_reference,
            instruction_manifest_sha256=expected_instruction_digest,
            prior_retry_receipt_sha256=_canonical_json_digest(receipt.to_dict()),
            prior_retry_receipt=receipt,
            verification_history_sha256=history_sha256,
            operator_reason=operator_reason,
        )
        next_receipt = ExternalVerificationRetryReceipt(
            expected_run_id=_deterministic_quantipy_run_id(
                state.iteration, implementation.commit_sha, attempt=4
            ),
            prior_verification_sha256=history_sha256[-1],
            probe=receipt.probe,
            retry_attempt=4,
            implementation_commit=implementation.commit_sha,
            manifest_sha256=implementation.experiment_manifest_sha256,
            readiness_manifest_id=receipt.readiness_manifest_id,
            readiness_snapshot_id=receipt.readiness_snapshot_id,
            operator_reason=receipt.operator_reason,
            verification_history_sha256=history_sha256,
            interruption_history_sha256=(_canonical_json_digest(interruption.to_dict()),),
            schema_version=INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,
        )
        recovered = replace(
            state,
            external_verification_retry_receipt=next_receipt,
            interrupted_verification_history=(interruption,),
        )
        _validate_state(recovered, policy, validation_context)
        if unit_is_active(status.systemd_unit):
            raise AutoresearchValidationError(
                "interrupted verification recovery refuses an active detached systemd unit"
            )
        if _detached_pid_is_alive(status.pid, proc_root=proc_root):
            raise AutoresearchValidationError(
                "interrupted verification recovery refuses a live detached process"
            )
        _reattest_sealed_interrupted_run(
            record,
            runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
            expected=sealed_attestation,
        )
        _require_absent_interrupted_run_artifact(expected_run_path)
        _atomic_save_state_file(resolved_path, recovered)
        return recovered


def _find_exact_interrupted_detached_run(
    *,
    runs_root: Path,
    iteration: int,
    directory_name: str,
    task_label: str,
    state_reference_sha256: str,
) -> RunRecord:
    """Select the sole state-bound v3 manifest before reading any run status."""
    import gateway.autoresearch_runs as detached_runs

    try:
        metadata = runs_root.lstat()
    except FileNotFoundError as exc:
        raise AutoresearchValidationError("detached runs root is unavailable") from exc
    if runs_root.is_symlink() or not runs_root.is_dir() or not metadata:
        raise AutoresearchValidationError("detached runs root is not a safe directory")

    expected_directory = runs_root / directory_name
    try:
        expected_manifest = detached_runs.read_run_manifest(
            run_dir=expected_directory,
            runs_root=runs_root,
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "interrupted verification recovery expected detached v3 manifest is unavailable "
            "or invalid"
        ) from exc
    if (
        expected_manifest.phase is not Phase.VERIFICATION
        or expected_manifest.iteration != iteration
        or expected_manifest.attempt != 3
        or expected_manifest.task_label != task_label
        or expected_manifest.state_reference_sha256 != state_reference_sha256
    ):
        raise AutoresearchValidationError(
            "interrupted verification recovery expected detached v3 manifest identity is invalid"
        )

    duplicate_directories: list[Path] = []
    for directory, child_directories, files in os.walk(runs_root, followlinks=False):
        child_directories.sort()
        files.sort()
        parent = Path(directory)
        if parent == expected_directory or "manifest.json" not in files:
            continue
        try:
            manifest = detached_runs.read_run_manifest(run_dir=parent, runs_root=runs_root)
        except (OSError, ValueError):
            continue
        if (
            manifest.phase is Phase.VERIFICATION
            and manifest.iteration == iteration
            and manifest.attempt == 3
            and manifest.task_label == task_label
            and manifest.state_reference_sha256 == state_reference_sha256
        ):
            duplicate_directories.append(parent)
    if duplicate_directories:
        raise AutoresearchValidationError(
            "interrupted verification recovery found duplicate expected detached v3 identities"
        )
    try:
        return detached_runs.read_run_record(run_dir=expected_directory, runs_root=runs_root)
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "interrupted verification recovery expected detached v3 status is unavailable "
            "or invalid"
        ) from exc


def _materialize_attested_pending_retry_failure(
    state: AutoresearchState,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState:
    """Advance only the preserved v2 artifact that predates state-file handoff."""
    receipt = state.external_verification_retry_receipt
    if receipt is None or state.phase is not Phase.VERIFICATION:
        return state
    if receipt.retry_attempt != 2 or len(state.verification_history) != 1:
        raise AutoresearchValidationError(
            "external verification retry state-file recovery only accepts the preserved v2 attempt"
        )
    previous = state.latest_verification
    if previous is None or previous.status is not VerificationStatus.TEST_FAILURE:
        raise AutoresearchValidationError(
            "external verification retry requires the preserved initial verification failure"
        )
    implementation = state.implementation_result
    if implementation is None:
        raise AutoresearchValidationError("external verification retry requires implementation")
    expected_run_id = _deterministic_quantipy_run_id(
        state.iteration,
        implementation.commit_sha,
        attempt=receipt.retry_attempt,
    )
    if receipt.expected_run_id != expected_run_id:
        raise AutoresearchValidationError(
            "external verification retry receipt run ID does not bind the implementation"
        )
    legacy_run_identity = f"{implementation.commit_sha[:12]}-v{receipt.retry_attempt}"
    if expected_run_id != f"autoresearch-i{state.iteration}-{legacy_run_identity}":
        raise AutoresearchValidationError(
            "external verification retry receipt run ID has an invalid legacy identity"
        )
    try:
        import gateway.autoresearch_runs as detached_runs

        detached_run_directory = detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT / (
            f"i{state.iteration}-verification-r1-a{receipt.retry_attempt}-{legacy_run_identity}"
        )
        detached_record = detached_runs.read_run_record(
            run_dir=detached_run_directory,
            runs_root=detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT,
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "preserved v2 detached Quantipy run record is unavailable or invalid"
        ) from exc
    expected_run_path = DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / receipt.expected_run_id / "run.json"
    if detached_record.manifest.expected_artifact_path != str(expected_run_path):
        raise AutoresearchValidationError(
            "preserved v2 detached run does not attest the deterministic run artifact"
        )
    run_snapshot = _secure_open_snapshot(
        expected_run_path,
        label="preserved v2 Quantipy run.json",
        trusted_root=DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        private=True,
        max_bytes=QUANTIPY_RUN_ENVELOPE_MAX_BYTES - 1,
    )
    run = _validate_quantipy_run_envelope(run_snapshot)
    failure = _run_failure_from_mapping(run["failure"])
    if (
        run["run_id"] != receipt.expected_run_id
        or run["success"] is not False
        or run["panel_requested"] is not True
        or run["panel"] is not None
        or run["stage_receipts"]
        or failure is None
        or failure.category != "panel"
        or not _is_manifest_bound_legacy_local_research_panel_http_413(state, failure.message)
    ):
        raise AutoresearchValidationError(
            "preserved v2 artifact is not the attested local research-panel HTTP 413 failure"
        )
    evidence = QuantipyExperimentEvidence(
        manifest_path=implementation.experiment_manifest_path,
        manifest_sha256=implementation.experiment_manifest_sha256,
        detached_run_directory=str(detached_record.run_directory),
        detached_run_manifest_sha256=detached_record.status.manifest_sha256,
        run_id=receipt.expected_run_id,
        run_json_path=str(expected_run_path),
        run_json_sha256=run_snapshot.sha256,
        success=False,
        completed_stages=(),
        terminal_stage=None,
        terminal_status=None,
        failure=failure,
        panel=None,
    )
    materialized = replace(
        previous,
        status=VerificationStatus.TEST_FAILURE,
        tests_passed=False,
        quantipy_experiment_evidence=evidence,
        quantipy_execution_not_started=None,
    )
    materialized.validate(mode=state.mode)
    _validate_alpha_price_scope_verification(state, materialized)
    _validate_quantipy_experiment_evidence(
        state,
        materialized,
        validation_context=validation_context,
    )
    intermediate = replace(
        state,
        verification_history=(*state.verification_history, materialized),
        pending_fix_trigger=FixTriggerPhase.VERIFICATION,
        phase=Phase.FIX_TEST,
    )
    _validate_state(intermediate, policy, validation_context)
    return intermediate


def initialize_state(readiness: PlatformReadinessManifest) -> AutoresearchState:
    """Create a pristine v4 campaign state pinned to authoritative readiness."""
    try:
        identity = readiness.require_ready()
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    return AutoresearchState(platform_readiness=identity)


def save_state_file(path: Path, state: AutoresearchState) -> None:
    resolved_path = path.expanduser().resolve(strict=False)
    with _exclusive_state_locks((resolved_path,)):
        _atomic_save_state_file(resolved_path, state)


def advance_infrastructure_verification_failure(
    *,
    state_path: Path,
    state_reference_sha256: str,
    instruction_manifest_sha256: str,
    artifact: VerificationResultArtifact,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState:
    """Atomically advance only a manifest-bound infrastructure verification failure."""
    _validate_sha256(state_reference_sha256, label="state_reference_sha256")
    _validate_sha256(instruction_manifest_sha256, label="instruction_manifest_sha256")
    resolved_path = state_path.expanduser().resolve(strict=False)
    with _exclusive_state_locks((resolved_path,)):
        state = load_state_file(resolved_path)
        current_reference = build_authoritative_state_reference(
            state,
            state_path=resolved_path,
        ).sha256()
        if current_reference != state_reference_sha256:
            raise AutoresearchValidationError(
                "infrastructure verification failure state reference is stale"
            )
        current_instruction_manifest = expected_instruction_manifest_sha256(
            state,
            policy,
            receipts,
            state_path=resolved_path,
        )
        if current_instruction_manifest != instruction_manifest_sha256:
            raise AutoresearchValidationError(
                "infrastructure verification failure instruction manifest is stale"
            )
        if state.phase is not Phase.VERIFICATION:
            raise AutoresearchValidationError(
                "infrastructure verification failure requires verification phase"
            )
        if state.mode is not ResearchMode.ALPHA_RESEARCH:
            raise AutoresearchValidationError(
                "infrastructure verification failure is valid only for ALPHA_RESEARCH"
            )
        if artifact.status is not VerificationStatus.TEST_FAILURE:
            raise AutoresearchValidationError(
                "infrastructure verification failure requires TEST_FAILURE status"
            )
        advanced = advance_state(
            state,
            artifact,
            policy,
            validation_context=validation_context,
            state_path=resolved_path,
        )
        _atomic_save_state_file(resolved_path, advanced)
        return advanced


def _canonical_state_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    canonical_paths = {path.expanduser().resolve(strict=False) for path in paths}
    lock_namespace = _prepare_lock_namespace()
    for path in canonical_paths:
        if path == lock_namespace or lock_namespace in path.parents:
            raise AutoresearchValidationError(
                f"autoresearch state path cannot be inside lock namespace: {path}"
            )
    return tuple(sorted(canonical_paths, key=os.fspath))


def _prepare_lock_namespace() -> Path:
    namespace_path = AUTORESEARCH_LOCK_NAMESPACE.expanduser().absolute()
    try:
        namespace_path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            f"unable to create autoresearch lock namespace: {namespace_path}"
        ) from exc
    try:
        namespace_stat = namespace_path.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(
            f"unable to inspect autoresearch lock namespace: {namespace_path}"
        ) from exc
    if not stat.S_ISDIR(namespace_stat.st_mode):
        raise AutoresearchValidationError(
            f"autoresearch lock namespace is not a directory: {namespace_path}"
        )
    if namespace_stat.st_uid != os.getuid():
        raise AutoresearchValidationError(
            f"autoresearch lock namespace has wrong owner: {namespace_path}"
        )
    if stat.S_IMODE(namespace_stat.st_mode) != 0o700:
        raise AutoresearchValidationError(
            f"autoresearch lock namespace permissions must be 0700: {namespace_path}"
        )
    return namespace_path.resolve(strict=True)


def _state_lock_path(state_path: Path) -> Path:
    canonical_state_path = state_path.expanduser().resolve(strict=False)
    lock_namespace = AUTORESEARCH_LOCK_NAMESPACE.expanduser().absolute()
    state_path_digest = hashlib.sha256(
        "\n".join((AUTORESEARCH_STATE_LOCK_DIGEST_DOMAIN, os.fspath(canonical_state_path))).encode(
            "utf-8"
        )
    ).hexdigest()
    return lock_namespace / f"{state_path_digest}.lock"


def _open_state_lock_file(lock_path: Path) -> int:
    create_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        lock_fd = os.open(lock_path, create_flags, 0o600)
    except FileExistsError:
        try:
            return os.open(
                lock_path,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise AutoresearchValidationError(
                f"unable to open autoresearch state lock: {lock_path}"
            ) from exc
    except OSError as exc:
        raise AutoresearchValidationError(
            f"unable to open autoresearch state lock: {lock_path}"
        ) from exc
    try:
        os.fchmod(lock_fd, 0o600)
    except OSError as exc:
        os.close(lock_fd)
        raise AutoresearchValidationError(
            f"unable to secure autoresearch state lock: {lock_path}"
        ) from exc
    return lock_fd


@contextmanager
def _exclusive_state_locks(paths: Sequence[Path]) -> Iterator[None]:
    """Lock canonical state paths once in deterministic order."""
    with ExitStack() as stack:
        for path in _canonical_state_paths(paths):
            stack.enter_context(_exclusive_state_lock(path))
        yield


@contextmanager
def _exclusive_state_lock(state_path: Path) -> Iterator[None]:
    """Serialize access to one canonical state path across CLI processes."""
    resolved_state_path = state_path.expanduser().resolve(strict=False)
    _prepare_lock_namespace()
    lock_path = _state_lock_path(resolved_state_path)
    lock_fd = _open_state_lock_file(lock_path)
    try:
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise AutoresearchValidationError(
                f"autoresearch state lock is not a regular file: {lock_path}"
            )
        if lock_stat.st_uid != os.getuid():
            raise AutoresearchValidationError(
                f"autoresearch state lock has wrong owner: {lock_path}"
            )
        if stat.S_IMODE(lock_stat.st_mode) != 0o600:
            raise AutoresearchValidationError(
                f"autoresearch state lock permissions must be 0600: {lock_path}"
            )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except AutoresearchValidationError:
        os.close(lock_fd)
        raise
    except OSError as exc:
        os.close(lock_fd)
        raise AutoresearchValidationError(
            f"unable to lock autoresearch state: {resolved_state_path}"
        ) from exc
    try:
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def persist_derived_state(
    source_path: Path,
    output_path: Path,
    source_state: AutoresearchState,
    derived_state: AutoresearchState,
    *,
    policy: AutoresearchPolicy | None = None,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    """Atomically publish a derived state only while its source remains authorized.

    The source path, rather than a prior derived output, is always compared while
    both canonical paths are locked. When the paths are equal, the verified source
    is atomically replaced. When they differ, the source is left untouched and only
    the derived output is atomically replaced.
    """
    resolved_source_path = source_path.expanduser().resolve(strict=False)
    resolved_output_path = output_path.expanduser().resolve(strict=False)
    expected_reference = build_authoritative_state_reference(
        source_state,
        state_path=resolved_source_path,
    )
    candidate_policy = policy or load_autoresearch_policy(DEFAULT_OPENCLAW_CONFIG_PATH)
    _validate_state(derived_state, candidate_policy, validation_context)
    with _exclusive_state_locks((resolved_source_path, resolved_output_path)):
        persisted_state = load_state_file(resolved_source_path)
        persisted_reference = build_authoritative_state_reference(
            persisted_state,
            state_path=resolved_source_path,
        )
        if persisted_reference != expected_reference:
            raise AutoresearchValidationError(
                "persisted state does not match the supplied authoritative state"
            )
        _validate_state(derived_state, candidate_policy, validation_context)
        _atomic_save_state_file(resolved_output_path, derived_state)


@dataclass(frozen=True, slots=True)
class _ArtifactAdvancePublicationGuard:
    source_path: Path
    artifact_path: Path
    instruction_manifest_sha256: str
    policy: AutoresearchPolicy
    validation_context: AutoresearchValidationContext | None
    state_reference_sha256: str | None
    expected_source_reference: AuthoritativeStateReference
    expected_candidate: AutoresearchState


def _revalidate_artifact_advance_for_atomic_publication(
    guard: _ArtifactAdvancePublicationGuard,
) -> None:
    """Re-derive every mutable input after temp-file fsync and before replace."""
    locked_state = load_state_file(guard.source_path)
    _require_canonical_verification_runtime_attestation(
        locked_state,
        validation_context=guard.validation_context,
    )
    locked_reference = build_authoritative_state_reference(
        locked_state,
        state_path=guard.source_path,
    )
    if locked_reference != guard.expected_source_reference:
        raise AutoresearchValidationError(
            "persisted state does not match the supplied authoritative state"
        )
    locked_artifact = load_artifact_file(
        guard.artifact_path,
        locked_state,
        guard.policy,
        instruction_manifest_sha256=guard.instruction_manifest_sha256,
        validation_context=guard.validation_context,
        state_reference_sha256=guard.state_reference_sha256,
        state_path=guard.source_path,
    )
    if isinstance(locked_artifact, ImplementationResultArtifact | FixResultArtifact):
        validate_artifact_workspace(locked_state, locked_artifact)
    locked_candidate = advance_state(
        locked_state,
        locked_artifact,
        guard.policy,
        validation_context=guard.validation_context,
        state_path=guard.source_path,
    )
    if locked_candidate != guard.expected_candidate:
        raise AutoresearchValidationError("artifact changed before state publication")


def advance_artifact_state_file(
    *,
    state_path: Path,
    output_path: Path,
    artifact_path: Path,
    instruction_manifest_sha256: str,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
    state_reference_sha256: str | None = None,
) -> AutoresearchState:
    """Advance a dispatched artifact twice, publishing only the locked re-derivation.

    Artifact bytes and the detached runtime evidence are mutable external inputs.
    The first derivation gives a useful fail-fast result; the second occurs while
    the state-file publication lock is held and must produce the identical state.
    """
    resolved_state_path = state_path.expanduser().resolve(strict=False)
    resolved_output_path = output_path.expanduser().resolve(strict=False)
    resolved_artifact_path = artifact_path.expanduser().resolve(strict=False)
    source_state = load_state_file(resolved_state_path)
    _require_canonical_verification_runtime_attestation(
        source_state,
        validation_context=validation_context,
    )
    expected_reference = build_authoritative_state_reference(
        source_state,
        state_path=resolved_state_path,
    )
    artifact = load_artifact_file(
        resolved_artifact_path,
        source_state,
        policy,
        instruction_manifest_sha256=instruction_manifest_sha256,
        validation_context=validation_context,
        state_reference_sha256=state_reference_sha256,
        state_path=resolved_state_path,
    )
    if isinstance(artifact, ImplementationResultArtifact | FixResultArtifact):
        validate_artifact_workspace(source_state, artifact)
    candidate = advance_state(
        source_state,
        artifact,
        policy,
        validation_context=validation_context,
        state_path=resolved_state_path,
    )
    with _exclusive_state_locks((resolved_state_path, resolved_output_path)):
        publication_guard = _ArtifactAdvancePublicationGuard(
            source_path=resolved_state_path,
            artifact_path=resolved_artifact_path,
            instruction_manifest_sha256=instruction_manifest_sha256,
            policy=policy,
            validation_context=validation_context,
            state_reference_sha256=state_reference_sha256,
            expected_source_reference=expected_reference,
            expected_candidate=candidate,
        )
        _atomic_save_state_file(
            resolved_output_path,
            candidate,
            publication_guard=publication_guard,
        )
        return candidate


def submit_stage_artifact_file(
    *,
    state_path: Path,
    artifact_path: Path,
    inbox_path: Path = DEFAULT_AUTORESEARCH_STAGE_INBOX,
    instruction_manifest_sha256: str,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
    state_reference_sha256: str | None = None,
) -> Path:
    """Validate a model-produced artifact and publish only its envelope to the inbox.

    This is the model-writable boundary. It never writes authoritative state; the
    supervisor/controller later re-derives the same transition under state locks.
    """
    resolved_state_path = state_path.expanduser().resolve(strict=False)
    resolved_artifact_path = artifact_path.expanduser().resolve(strict=False)
    configured_inbox_path = inbox_path.expanduser()
    source_state = load_state_file(resolved_state_path)
    _require_canonical_verification_runtime_attestation(
        source_state,
        validation_context=validation_context,
    )
    expected_reference = build_authoritative_state_reference(
        source_state,
        state_path=resolved_state_path,
    )
    artifact = load_artifact_file(
        resolved_artifact_path,
        source_state,
        policy,
        instruction_manifest_sha256=instruction_manifest_sha256,
        validation_context=validation_context,
        state_reference_sha256=state_reference_sha256,
        state_path=resolved_state_path,
    )
    if isinstance(artifact, ImplementationResultArtifact | FixResultArtifact):
        validate_artifact_workspace(source_state, artifact)
    advance_state(
        source_state,
        artifact,
        policy,
        validation_context=validation_context,
        state_path=resolved_state_path,
    )
    envelope = resolved_artifact_path.read_bytes()
    if len(envelope) > MAX_STAGE_SUBMISSION_BYTES:
        raise AutoresearchValidationError(
            "stage submission exceeds hard byte budget: "
            f"{len(envelope)} > {MAX_STAGE_SUBMISSION_BYTES} bytes"
        )
    artifact_sha256 = hashlib.sha256(envelope).hexdigest()
    filename = (
        f"{source_state.iteration:04d}-{source_state.phase.value}-"
        f"{expected_reference.sha256()[:16]}-{artifact_sha256[:16]}.json"
    )
    output_path = configured_inbox_path / filename
    try:
        configured_inbox_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        inbox_fd = os.open(
            configured_inbox_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except FileExistsError:
        raise
    except OSError as exc:
        raise AutoresearchValidationError(f"failed to open stage submission inbox: {exc}") from exc
    try:
        _validate_stage_inbox_directory_fd(inbox_fd, label="stage submission inbox")
        try:
            output_fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=inbox_fd,
            )
        except FileExistsError:
            try:
                existing_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=inbox_fd)
            except OSError as exc:
                raise AutoresearchValidationError(
                    f"failed to inspect existing stage submission artifact: {exc}"
                ) from exc
            try:
                existing = os.read(existing_fd, MAX_STAGE_SUBMISSION_BYTES + 1)
            finally:
                os.close(existing_fd)
            if hashlib.sha256(existing).hexdigest() != artifact_sha256:
                raise AutoresearchValidationError(
                    "stage submission filename collision with different artifact"
                ) from None
        except OSError as exc:
            raise AutoresearchValidationError(
                f"failed to write stage submission inbox artifact: {exc}"
            ) from exc
        else:
            try:
                written = 0
                while written < len(envelope):
                    written += os.write(output_fd, envelope[written:])
                os.fsync(output_fd)
            finally:
                os.close(output_fd)
    finally:
        os.close(inbox_fd)
    return output_path


def _validate_stage_inbox_directory_fd(fd: int, *, label: str) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise AutoresearchValidationError(f"{label} must be a plain directory")
    if metadata.st_uid != os.getuid():
        raise AutoresearchValidationError(f"{label} must be owned by the current user")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AutoresearchValidationError(f"{label} must not be group/world writable")
    return metadata


def _open_stage_inbox_child_directory(inbox_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=inbox_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            f"cannot create stage submission {name} directory: {exc}"
        ) from exc
    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=inbox_fd,
        )
    except OSError as exc:
        raise AutoresearchValidationError(
            f"stage submission {name} path must be a plain directory: {exc}"
        ) from exc
    try:
        _validate_stage_inbox_directory_fd(child_fd, label=f"stage submission {name} directory")
    except Exception:
        os.close(child_fd)
        raise
    return child_fd


def _move_stage_inbox_entry_no_replace(
    name: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    for attempt in range(128):
        target = name
        if attempt:
            digest = hashlib.sha256(f"{name}\n{time.time_ns()}\n{attempt}".encode()).hexdigest()
            target = f"{name}.{digest[:16]}"
        try:
            os.link(
                name,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise AutoresearchValidationError(
                f"cannot quarantine stage submission artifact: {exc}"
            ) from exc
        os.unlink(name, dir_fd=src_dir_fd)
        return
    raise AutoresearchValidationError(
        "cannot quarantine stage submission artifact without overwrite"
    )


def consume_stage_submission_inbox(
    *,
    state_path: Path,
    output_path: Path,
    inbox_path: Path,
    openclaw_config: Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    quantipy_root: Path = DEFAULT_QUANTIPY_ROOT,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState | None:
    """Consume at most one validated stage submission from a model-writable inbox."""
    try:
        inbox_fd = os.open(
            inbox_path.expanduser(),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AutoresearchValidationError(f"cannot open stage submission inbox: {exc}") from exc
    snapshot_path: Path | None = None
    accepted_fd: int | None = None
    rejected_fd: int | None = None
    try:
        _validate_stage_inbox_directory_fd(inbox_fd, label="stage submission inbox")
        accepted_fd = _open_stage_inbox_child_directory(inbox_fd, "accepted")
        rejected_fd = _open_stage_inbox_child_directory(inbox_fd, "rejected")
        assert accepted_fd is not None
        assert rejected_fd is not None

        candidates: list[str] = []
        for name in sorted(os.listdir(inbox_fd)):
            if not name.endswith(".json"):
                continue
            entry = os.stat(name, dir_fd=inbox_fd, follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode):
                raise AutoresearchValidationError(
                    "stage submission candidate must be a non-symlink regular file"
                )
            candidates.append(name)

        if not candidates:
            return None
        policy = load_autoresearch_policy(openclaw_config)

        def quarantine(name: str, destination_fd: int) -> None:
            _move_stage_inbox_entry_no_replace(
                name,
                src_dir_fd=inbox_fd,
                dst_dir_fd=destination_fd,
            )

        for artifact_name in candidates:
            try:
                artifact_stat = os.stat(artifact_name, dir_fd=inbox_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AutoresearchValidationError(
                    f"cannot inspect stage submission artifact: {exc}"
                ) from exc
            if not stat.S_ISREG(artifact_stat.st_mode):
                raise AutoresearchValidationError(
                    "stage submission artifact must be a non-symlink regular file"
                )
            try:
                artifact_fd = os.open(artifact_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=inbox_fd)
            except OSError as exc:
                raise AutoresearchValidationError(
                    f"cannot open stage submission artifact: {exc}"
                ) from exc
            try:
                opened_artifact = os.fstat(artifact_fd)
                if (
                    opened_artifact.st_dev,
                    opened_artifact.st_ino,
                    opened_artifact.st_size,
                ) != (
                    artifact_stat.st_dev,
                    artifact_stat.st_ino,
                    artifact_stat.st_size,
                ):
                    raise AutoresearchValidationError(
                        "stage submission artifact changed during inspection"
                    )
                if opened_artifact.st_nlink != 1:
                    quarantine(artifact_name, rejected_fd)
                    continue
                if opened_artifact.st_size > MAX_STAGE_SUBMISSION_BYTES:
                    quarantine(artifact_name, rejected_fd)
                    continue
                envelope = os.read(artifact_fd, MAX_STAGE_SUBMISSION_BYTES + 1)
                if len(envelope) != opened_artifact.st_size:
                    raise AutoresearchValidationError(
                        "stage submission artifact changed while reading"
                    )
            finally:
                os.close(artifact_fd)
            snapshot_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=Path(state_path).expanduser().resolve(strict=False).parent,
                    prefix=".stage-submission.",
                    suffix=".json",
                    delete=False,
                ) as snapshot:
                    snapshot_path = Path(snapshot.name)
                    snapshot.write(envelope)
                    snapshot.flush()
                    os.fsync(snapshot.fileno())
                snapshot_path.chmod(0o600)
                artifact_path = snapshot_path
                state = load_state_file(state_path)
                receipts = build_receipt_catalog(quantipy_root)
                instruction_manifest_sha256 = expected_instruction_manifest_sha256(
                    state,
                    policy,
                    receipts,
                    state_path=state_path,
                )
                try:
                    advanced = advance_artifact_state_file(
                        state_path=state_path,
                        output_path=output_path,
                        artifact_path=artifact_path,
                        instruction_manifest_sha256=instruction_manifest_sha256,
                        policy=policy,
                        validation_context=validation_context,
                    )
                except AutoresearchValidationError:
                    quarantine(artifact_name, rejected_fd)
                    continue
                quarantine(artifact_name, accepted_fd)
                return advanced
            finally:
                if snapshot_path is not None:
                    with suppress(FileNotFoundError):
                        snapshot_path.unlink()
    finally:
        if accepted_fd is not None:
            os.close(accepted_fd)
        if rejected_fd is not None:
            os.close(rejected_fd)
        os.close(inbox_fd)
    return None


def persist_next_iteration_state(
    source_path: Path,
    output_path: Path,
    source_state: AutoresearchState,
    derived_state: AutoresearchState,
    *,
    instruction_manifest_sha256: str,
    policy: AutoresearchPolicy,
    receipt_catalog_factory: Callable[[], ReceiptCatalog],
) -> None:
    """Publish a next-iteration state only after persisting its decision receipt."""
    resolved_source_path = source_path.expanduser().resolve(strict=False)
    resolved_output_path = output_path.expanduser().resolve(strict=False)
    expected_reference = build_authoritative_state_reference(
        source_state,
        state_path=resolved_source_path,
    )
    with _exclusive_state_locks((resolved_source_path, resolved_output_path)):
        persisted_state = load_state_file(resolved_source_path)
        persisted_reference = build_authoritative_state_reference(
            persisted_state,
            state_path=resolved_source_path,
        )
        if persisted_reference != expected_reference:
            raise AutoresearchValidationError(
                "persisted state does not match the supplied authoritative state"
            )
        current_instruction_manifest_sha256 = expected_instruction_manifest_sha256(
            persisted_state,
            policy,
            receipt_catalog_factory(),
            state_path=resolved_source_path,
        )
        if current_instruction_manifest_sha256 != instruction_manifest_sha256:
            raise AutoresearchValidationError("decision receipt instruction manifest is stale")
        from gateway.autoresearch_decision_receipts import persist_decision_receipt

        persist_decision_receipt(
            persisted_state,
            state_path=source_path,
            instruction_manifest_sha256=instruction_manifest_sha256,
        )
        _atomic_save_state_file(resolved_output_path, derived_state)


def _atomic_save_state_file(
    path: Path,
    state: AutoresearchState,
    *,
    publication_guard: _ArtifactAdvancePublicationGuard | None = None,
) -> None:
    if publication_guard is not None and state != publication_guard.expected_candidate:
        raise AutoresearchValidationError(
            "atomic artifact publication candidate does not match its guard"
        )
    serialized_state = json.dumps(state.to_dict(), indent=2, sort_keys=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(serialized_state)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if publication_guard is not None:
            _revalidate_artifact_advance_for_atomic_publication(publication_guard)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
