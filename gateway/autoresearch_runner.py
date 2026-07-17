"""Deterministic control-plane for the Quantipy autoresearch loop.

This module owns the fixed phase graph, stage-agent policy validation,
skill/source receipts, artifact validation, and next-action selection.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from bisect import bisect_right
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from ctypes.util import find_library
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from gateway.autoresearch_readiness import (
    PlatformReadinessManifest,
    ReadinessIdentity,
    load_xnys_calendar_evidence,
    validate_state_readiness,
)

DEFAULT_OPENCLAW_CONFIG_PATH = Path("gateway/openclaw_config/openclaw.json")
DEFAULT_QUANTIPY_ROOT = Path("/home/dev/repos/quantipy")
DEFAULT_AUTORESEARCH_WORKTREE_ROOT = Path("/home/dev/.openclaw/autoresearch/worktrees")
DEFAULT_AUTORESEARCH_STATE_PATH = Path("/home/dev/.openclaw/autoresearch/quantipy-state.json")
AUTORESEARCH_LOCK_NAMESPACE = Path("/tmp") / f"g2-openclaw-autoresearch-locks-{os.getuid()}"
G2_OPENCLAW_REPO_ROOT = Path(__file__).resolve().parent.parent
AUTORESEARCH_STATE_SCHEMA_VERSION = 2
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
MAX_ARTIFACT_FILE_BYTES = 24 * 1024
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


class AutoresearchError(ValueError):
    """Base error for deterministic autoresearch control-plane failures."""


class AutoresearchConfigError(AutoresearchError):
    """Raised when the OpenClaw stage-agent config deviates from policy."""


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
MEMPALACE_FULL_SERVER_ID = "mempalace"
MEMPALACE_READONLY_SERVER_ID = "mempalace-readonly"
MEMPALACE_READONLY_WRAPPER_BASENAME = "mempalace-readonly-server.py"
MEMPALACE_KG_OBJECT_MAX_LENGTH = 128
MEMPALACE_KG_OBJECT_SHA256_LENGTH = 64
# OpenClaw policy checks compare internal MCP tool.name ids such as
# "mempalace__mempalace_search". Codex-facing docs and traces show dotted
# display ids such as "mempalace.mempalace_add_drawer" or
# "mempalace-readonly.mempalace_search" after adaptation.
MEMPALACE_POLICY_TOOL_PREFIX = "mempalace__"
MEMPALACE_FULL_SERVER_DISPLAY_NAMESPACE = MEMPALACE_FULL_SERVER_ID
MEMPALACE_READONLY_DISPLAY_NAMESPACE = MEMPALACE_READONLY_SERVER_ID
MEMPALACE_MUTATION_TOOLS = (
    "mempalace_add_drawer",
    "mempalace_check_duplicate",
    "mempalace_checkpoint",
    "mempalace_create_tunnel",
    "mempalace_delete_by_source",
    "mempalace_delete_drawer",
    "mempalace_delete_hallway",
    "mempalace_delete_tunnel",
    "mempalace_diary_write",
    "mempalace_hook_settings",
    "mempalace_kg_add",
    "mempalace_kg_invalidate",
    "mempalace_mine",
    "mempalace_reconnect",
    "mempalace_sync",
    "mempalace_update_drawer",
)
MEMPALACE_OBSOLETE_MUTATION_TOOL_ID_PREFIXES = (
    "",
    "mcp__mempalace__",
    f"{MEMPALACE_FULL_SERVER_DISPLAY_NAMESPACE}.",
)
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


def _compile_mempalace_policy_tool_ids(
    tool_names: Sequence[str],
) -> tuple[str, ...]:
    return tuple(f"{MEMPALACE_POLICY_TOOL_PREFIX}{tool_name}" for tool_name in tool_names)


def _compile_mempalace_codex_display_tool_ids(
    tool_names: Sequence[str],
    *,
    namespace: str,
) -> tuple[str, ...]:
    return tuple(f"{namespace}.{tool_name}" for tool_name in tool_names)


def _compile_mempalace_alias_tool_ids(
    tool_names: Sequence[str],
    *,
    prefixes: Sequence[str] = MEMPALACE_OBSOLETE_MUTATION_TOOL_ID_PREFIXES,
) -> tuple[str, ...]:
    expanded: list[str] = []
    for prefix in prefixes:
        expanded.extend(f"{prefix}{tool_name}" for tool_name in tool_names)
    return tuple(expanded)


MEMPALACE_MUTATION_DENY_TOOL_IDS = _compile_mempalace_policy_tool_ids(MEMPALACE_MUTATION_TOOLS)
MEMPALACE_OBSOLETE_MUTATION_ALIAS_TOOL_IDS = _compile_mempalace_alias_tool_ids(
    MEMPALACE_MUTATION_TOOLS
)
MEMPALACE_READONLY_DISPLAY_TOOL_IDS = _compile_mempalace_codex_display_tool_ids(
    MEMPALACE_READONLY_TOOL_NAMES,
    namespace=MEMPALACE_READONLY_DISPLAY_NAMESPACE,
)
MEMPALACE_MUTATION_DENY_TOOL_ID_SET = frozenset(MEMPALACE_MUTATION_DENY_TOOL_IDS)
MEMPALACE_OBSOLETE_MUTATION_ALIAS_TOOL_ID_SET = frozenset(
    MEMPALACE_OBSOLETE_MUTATION_ALIAS_TOOL_IDS
)
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


def _canonical_json_digest(value: Mapping[str, object]) -> str:
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
class ImplementationResultArtifact:
    summary: str
    workspace_path: str
    commit_sha: str
    module_path: str
    notebook_path: str
    tests_added_or_updated: tuple[str, ...]
    commands_run: tuple[str, ...]
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

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "workspace_path": self.workspace_path,
            "commit_sha": self.commit_sha,
            "module_path": self.module_path,
            "notebook_path": self.notebook_path,
            "tests_added_or_updated": list(self.tests_added_or_updated),
            "commands_run": list(self.commands_run),
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

    @classmethod
    def from_readiness(cls, readiness: PlatformReadinessManifest) -> AutoresearchValidationContext:
        try:
            digest, evidence = load_xnys_calendar_evidence(readiness)
            identity = readiness.require_ready()
        except ValueError as exc:
            raise AutoresearchValidationError(str(exc)) from exc
        sessions = evidence.sessions
        if not sessions:
            raise AutoresearchValidationError("XNYS calendar evidence contains no sessions")
        return cls(identity, digest, sessions)

    def validate_for_state(self, state: AutoresearchState) -> None:
        if state.platform_readiness != self.readiness_identity:
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


def _verify_member_union_manifest(receipt: UniverseVerificationReceipt) -> None:
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
    infra_gate_outcome: InfraGateOutcome | None = None
    infra_rationale: str | None = None
    universe_verification_receipt: UniverseVerificationReceipt | None = None
    price_hydration_receipt: PriceHydrationReceipt | None = None

    @classmethod
    def from_dict(
        cls, raw: object, *, mode: ResearchMode | None = None
    ) -> VerificationResultArtifact:
        data = _ensure_mapping(raw, label="verification_result")
        _require_exact_keys(
            data,
            label="verification_result",
            expected=(
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
                "infra_gate_outcome",
                "infra_rationale",
                "universe_verification_receipt",
                "price_hydration_receipt",
            ),
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
        )
        artifact.validate(mode=mode)
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
        if mode is ResearchMode.DATA_INFRA_G0 and (outcome is None or not self.infra_rationale):
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 verification requires infra_gate_outcome and infra_rationale"
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
            ),
        )
        artifact = cls(
            trigger_phase=FixTriggerPhase(_require_str(data, "trigger_phase")),
            summary=_require_str(data, "summary"),
            workspace_path=_require_workspace_path(data, "workspace_path"),
            commit_sha=_require_str(data, "commit_sha"),
            fixes_applied=_require_string_list(data, "fixes_applied"),
            tests_rerun=_require_string_list(data, "tests_rerun"),
            remaining_issues=_require_string_list(data, "remaining_issues"),
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
                "schema-less live state must run `gateway-cli autoresearch-migrate-state` "
                "before `gateway-cli autoresearch-next`; "
                f"expected schema_version={AUTORESEARCH_STATE_SCHEMA_VERSION}"
            )
        schema_version = _require_int(data, "schema_version")
        if schema_version != AUTORESEARCH_STATE_SCHEMA_VERSION:
            raise AutoresearchValidationError(
                "autoresearch state schema migration is required; "
                f"expected {AUTORESEARCH_STATE_SCHEMA_VERSION}, got {schema_version}"
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
        pending_fix_trigger_raw = data.get("pending_fix_trigger")
        if pending_fix_trigger_raw is not None and not isinstance(pending_fix_trigger_raw, str):
            raise AutoresearchValidationError("pending_fix_trigger must be a string or null")
        if "mode" not in data:
            raise AutoresearchValidationError(
                "mode must be explicit in persisted autoresearch state"
            )
        _require_exact_keys(
            data,
            label="autoresearch_state",
            expected=(
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
                "suspended",
                "suspension_reason",
            ),
        )
        mode_raw = data.get("mode")
        if mode_raw is not None and not isinstance(mode_raw, str):
            raise AutoresearchValidationError("mode must be a string or null")

        def _parse_state_implementation(raw_implementation: object) -> ImplementationResultArtifact:
            implementation_data = dict(
                _ensure_mapping(raw_implementation, label="implementation_result")
            )
            implementation_data.setdefault("price_hydration_scope_preflight", None)
            return ImplementationResultArtifact.from_dict(implementation_data)

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
            implementation_result=_parse_state_implementation(implementation_raw)
            if implementation_raw is not None
            else None,
            verification_history=_parse_tuple(
                "verification_history",
                lambda item: VerificationResultArtifact.from_dict(
                    item, mode=ResearchMode(mode_raw) if mode_raw is not None else None
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
            suspended=_require_bool(data, "suspended") if "suspended" in data else False,
            suspension_reason=(
                _require_str(data, "suspension_reason")
                if data.get("suspension_reason") is not None
                else None
            ),
        )
        return state

    def to_dict(self) -> dict[str, object]:
        return {
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
            "verification_history": [artifact.to_dict() for artifact in self.verification_history],
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
            "suspended": self.suspended,
            "suspension_reason": self.suspension_reason,
        }


def build_authoritative_state_reference(
    state: AutoresearchState,
    *,
    state_path: Path = DEFAULT_AUTORESEARCH_STATE_PATH,
) -> AuthoritativeStateReference:
    """Bind a stage dispatch to one canonical, complete persisted state."""
    canonical_state_model = normalize_autoresearch_state(
        AutoresearchState.from_dict(state.to_dict())
    )
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
        ]
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
            "infra_gate_outcome",
            "infra_rationale",
            "universe_verification_receipt",
            "price_hydration_receipt",
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
        context_curator=_agent_policy_from_json(agent_map, "context-curator"),
        debate_agents=tuple(
            _agent_policy_from_json(agent_map, agent_id)
            for agent_id in (
                "debater-microstructure",
                "debater-data",
                "debater-skeptic",
                "debater-theory",
                "debater-implementation",
            )
        ),
        consensus=_agent_policy_from_json(agent_map, "consensus-arbiter"),
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
    if policy.main_interface.skills:
        raise AutoresearchConfigError("main must load no skills")
    main_raw = agent_map["main"]
    if main_raw.get("subagents") is not None:
        raise AutoresearchConfigError("main must not declare a subagent allowlist")
    if policy.pm.model != "openai/gpt-5.6-sol" or policy.pm.reasoning != "high":
        raise AutoresearchConfigError("PM must be openai/gpt-5.6-sol with high reasoning")
    if (
        policy.context_curator.model != "openai/gpt-5.4"
        or policy.context_curator.reasoning != "high"
    ):
        raise AutoresearchConfigError("context-curator must be openai/gpt-5.4 with high reasoning")

    expected_debate_models = {
        "debater-microstructure": "openai/gpt-5.5",
        "debater-data": "openai/gpt-5.6-terra",
        "debater-skeptic": "openai/gpt-5.5",
        "debater-theory": "openai/gpt-5.4",
        "debater-implementation": "openai/gpt-5.4",
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
            "consensus-arbiter must be openai/gpt-5.6-sol with high reasoning"
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

    if tuple(policy.pm.skills) != ("mempalace", "autoresearch"):
        raise AutoresearchConfigError("PM must load exactly mempalace and autoresearch")
    pm_raw = agent_map["autoresearch-pm"]
    subagents = _ensure_mapping(pm_raw.get("subagents"), label="autoresearch-pm.subagents")
    allow_agents = _require_string_list(subagents, "allowAgents")
    if tuple(allow_agents) != policy.all_stage_agent_ids:
        raise AutoresearchConfigError(
            "PM allowAgents must exactly match the autoresearch stage roster"
        )
    _validate_mempalace_server_split(config, policy)
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
        if "mempalace" in agent.skills:
            raise AutoresearchConfigError(f"{agent.agent_id} must not load mempalace")
        tools = _ensure_mapping(
            agent_map[agent.agent_id].get("tools"),
            label=f"{agent.agent_id}.tools",
        )
        # These denies must use internal OpenClaw policy ids, not the dotted
        # display ids that Codex shows in traces and docs.
        denied_tool_list = _require_string_list(tools, "deny")
        denied_tools = set(denied_tool_list)
        missing_deny = sorted(MEMPALACE_MUTATION_DENY_TOOL_ID_SET - denied_tools)
        if missing_deny:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must deny exact MemPalace mutation policy IDs: "
                f"{', '.join(missing_deny)}"
            )
        obsolete_aliases = sorted(MEMPALACE_OBSOLETE_MUTATION_ALIAS_TOOL_ID_SET & denied_tools)
        if obsolete_aliases:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must not deny obsolete MemPalace mutation aliases: "
                f"{', '.join(obsolete_aliases)}"
            )
        unexpected_deny = sorted(denied_tools - MEMPALACE_MUTATION_DENY_TOOL_ID_SET)
        if unexpected_deny:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must deny only canonical MemPalace mutation policy IDs: "
                f"{', '.join(unexpected_deny)}"
            )
        if tuple(denied_tool_list) != MEMPALACE_MUTATION_DENY_TOOL_IDS:
            raise AutoresearchConfigError(
                f"{agent.agent_id} must deny exactly the canonical MemPalace mutation policy IDs"
            )


def _validate_mempalace_server_split(
    config: Mapping[str, object],
    policy: AutoresearchPolicy,
) -> None:
    try:
        mcp = _ensure_mapping(config.get("mcp"), label="mcp")
        servers = _ensure_mapping(mcp.get("servers"), label="mcp.servers")
        full_server = _ensure_mapping(
            servers.get(MEMPALACE_FULL_SERVER_ID),
            label=f"mcp.servers.{MEMPALACE_FULL_SERVER_ID}",
        )
        readonly_server = _ensure_mapping(
            servers.get(MEMPALACE_READONLY_SERVER_ID),
            label=f"mcp.servers.{MEMPALACE_READONLY_SERVER_ID}",
        )
        _validate_mempalace_server(
            full_server,
            server_id=MEMPALACE_FULL_SERVER_ID,
            expected_agents=(policy.pm.agent_id,),
            expected_args_prefix=("-m", "mempalace.mcp_server", "--palace"),
        )
        _validate_mempalace_server(
            readonly_server,
            server_id=MEMPALACE_READONLY_SERVER_ID,
            expected_agents=policy.all_stage_agent_ids,
            expected_args_prefix=(MEMPALACE_READONLY_WRAPPER_BASENAME, "--palace"),
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
    if server_id == MEMPALACE_FULL_SERVER_ID:
        if len(args) != 4 or args[:3] != expected_args_prefix:
            raise AutoresearchConfigError(
                "mcp.servers.mempalace.args must be "
                "['-m', 'mempalace.mcp_server', '--palace', '<path>']"
            )
        if not args[3].strip():
            raise AutoresearchConfigError("mcp.servers.mempalace.args[3] must be a palace path")
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


def _revalidate_accepted_member_union_manifests(state: AutoresearchState) -> None:
    if state.mode is not ResearchMode.ALPHA_RESEARCH:
        return
    for verification in state.verification_history:
        receipt = verification.universe_verification_receipt
        if verification.status is VerificationStatus.PASS and receipt is not None:
            receipt.member_union_manifest.validate()
            _verify_member_union_manifest(receipt)


def _validate_state(state: AutoresearchState, policy: AutoresearchPolicy) -> None:
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
    if state.debate_rounds and state.context_packet is None:
        raise AutoresearchValidationError("debate history requires a context_packet")
    if state.consensus_history and state.latest_debate is None:
        raise AutoresearchValidationError("consensus history requires a debate_result")
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
        elif (
            not decision.memory_write_required
            and not _is_operator_precondition_no_memory_state(state)
            and not _is_data_infra_g0_blocked_no_memory_state(state)
            and not is_operator_infrastructure_suspension
        ):
            raise AutoresearchValidationError(
                "completed final decisions require memory_write_required=true"
            )
        if not is_operator_infrastructure_suspension:
            _validate_final_decision_artifact(decision, state)
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
        verification.validate(mode=state.mode)
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


def validate_state(state: AutoresearchState, policy: AutoresearchPolicy) -> None:
    _validate_state(state, policy)


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
    _validate_implementation_workspace(
        state,
        replace(
            state.implementation_result,
            commit_sha=artifact.commit_sha,
        ),
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
    return _require_strict_canonical_workspace_path(
        str(DEFAULT_AUTORESEARCH_WORKTREE_ROOT),
        label="autoresearch worktree root",
    )


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


def _require_ancestor(
    worktree: Path,
    ancestor: str,
    descendant: str,
    *,
    error_message: str,
) -> None:
    result = _run_git(
        worktree,
        ("merge-base", "--is-ancestor", ancestor, descendant),
        operation="ancestry check",
    )
    if result.returncode == 1:
        raise AutoresearchValidationError(error_message)
    if result.returncode != 0:
        raise AutoresearchValidationError(
            f"Git ancestry check failed in {_render_literal(str(worktree))}"
        )


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

    workspace = _require_git_worktree_root(workspace, label="artifact workspace_path")
    target_checkout = _require_git_worktree_root(
        Path(state.setup.target_repo).expanduser(),
        label="authoritative target_repo",
    )
    if workspace == target_checkout:
        raise AutoresearchValidationError(
            "artifact workspace_path must be distinct from authoritative target_repo"
        )
    registered_worktrees = {
        Path(line.removeprefix("worktree ")).resolve()
        for line in _require_git_output(
            target_checkout,
            ("worktree", "list", "--porcelain"),
            operation="worktree registration check",
        ).splitlines()
        if line.startswith("worktree ")
    }
    if workspace not in registered_worktrees:
        raise AutoresearchValidationError(
            "artifact workspace_path is not a Git worktree registered to authoritative target_repo"
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
        implementation_commit = _resolve_git_commit(
            workspace,
            state.implementation_result.commit_sha,
            label="prior implementation commit_sha",
        )
        _require_ancestor(
            workspace,
            implementation_commit,
            artifact_commit,
            error_message="prior implementation commit_sha is not an ancestor of final fix commit",
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
        )


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
) -> None:
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
        expected_no_consensus = (
            FinalDecision.INFRA_BLOCKED
            if state.mode is ResearchMode.DATA_INFRA_G0
            else FinalDecision.NO_CONSENSUS
        )
        if artifact.decision is not expected_no_consensus:
            raise AutoresearchValidationError(
                f"final_decision must be {expected_no_consensus.value} when consensus "
                "never reached a majority"
            )
        if artifact.memory_write_required:
            raise AutoresearchValidationError(
                "NO_CONSENSUS requires final_decision.memory_write_required=false"
            )
        if state.mode is ResearchMode.DATA_INFRA_G0 and not artifact.infra_rationale:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 no-consensus final_decision requires infra_rationale"
            )
        if state.mode is not ResearchMode.DATA_INFRA_G0 and artifact.infra_rationale is not None:
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

    if state.mode is ResearchMode.DATA_INFRA_G0:
        if artifact.decision not in (FinalDecision.INFRA_REPAIRED, FinalDecision.INFRA_BLOCKED):
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision must be INFRA_REPAIRED or INFRA_BLOCKED"
            )
        if not artifact.infra_rationale:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision requires infra_rationale"
            )
        if latest_verification is None or latest_verification.infra_gate_outcome is None:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision requires an infrastructure verification gate"
            )
        expected = (
            FinalDecision.INFRA_REPAIRED
            if latest_verification.infra_gate_outcome is InfraGateOutcome.GATE_PASSED
            else FinalDecision.INFRA_BLOCKED
        )
        if artifact.decision is not expected:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 final_decision must match infra_gate_outcome"
            )
        if artifact.decision is FinalDecision.INFRA_BLOCKED and artifact.memory_write_required:
            raise AutoresearchValidationError(
                "DATA_INFRA_G0 INFRA_BLOCKED requires memory_write_required=false"
            )
        return

    if artifact.infra_rationale:
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH final_decision cannot contain infra_rationale"
        )

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
            "NO_CONSENSUS only. The first NO_CONSENSUS gets exactly one retry. For an "
            "ALPHA_RESEARCH MAJORITY, freeze one compact universe_plan with profile "
            "identity/digests, sorted unique explicit selection dates, and "
            "next-session-or-later execution policy."
        ),
        Phase.IMPLEMENTATION: (
            "Implementation is allowed only after a majority consensus. "
            "Use the final implementation brief exactly "
            "as approved. No implementation without consensus majority. For "
            "ALPHA_RESEARCH, prewarm every frozen explicit selection date once with "
            "qp.security_universe_screen(), derive deterministic contiguous batches from "
            "the frozen canonical plan inputs, perform one "
            "qp.security_universe_history() operation per batch, form only an in-memory "
            "sorted member union, and call "
            "qp.prices() exactly once for that union and the full experiment "
            "range/timeframe/market-hours before constructing any fold. Before any "
            "hydrate-capable command, compute price_hydration_scope_preflight with "
            "member_union_count, experiment range, timeframe, market_hours, XNYS "
            "session_count, planned_symbol_sessions, and within_budget; include it "
            "in implementation_result. If within_budget is false, do not run any "
            "qp.prices(), hydrate, full backtest, or notebook command that would load "
            "the price panel; commit the scaffold, focused tests, notebook shell, and "
            "over-budget preflight so verification can emit the structured feasibility "
            "BUG_SIGNAL without spending the hydrate cost."
        ),
        Phase.VERIFICATION: (
            "Verify the produced experiment deterministically. "
            "Use implementation_result.workspace_path and "
            "implementation_result.commit_sha as the source under test. "
            "Reject impossible metrics, failing tests, "
            "or incomplete required metrics."
        ),
        Phase.REVIEW: (
            "Run exactly one configured reviewer. "
            "The reviewer must return PASS, CONDITIONAL PASS, or FAIL "
            "with concrete fix requests."
        ),
        Phase.FIX_TEST: (
            "Apply a narrow fix against the latest verification or review failure. "
            "After a fix, the next step "
            "is always verification."
        ),
        Phase.DECISION_LOG: (
            "Decide and log the completed iteration. "
            "Memory writes are forbidden before this final decision "
            "artifact exists."
        ),
        Phase.REPEAT: (
            "The iteration is complete. Do not start the next loop from prompt memory. "
            "Write memory only if allowed, then create a new state explicitly."
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
            "- The context packet must choose ALPHA_RESEARCH or DATA_INFRA_G0 and give "
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
    price_scope_preflight: PriceHydrationScopePreflight | None = None,
) -> str:
    if (
        phase is not Phase.VERIFICATION
        or expected_artifact_type is not ArtifactType.VERIFICATION_RESULT
    ):
        return ""
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
        "test, and notebook execution plus experiment correctness, never whether "
        "the infrastructure gate passed. REMEDIATION_REQUIRED is a valid completed "
        "verification outcome: emit PASS with tests_passed=true when commands, tests, "
        "and notebook execution succeeded. A DATA_INFRA_G0 PASS may set alpha metrics, "
        "data_coverage, and both universe and price hydration receipts to null when unavailable; "
        "never fabricate them. It advances to review and then final INFRA_BLOCKED; do not send "
        "operator-owned remediation to fixer. Use "
        "TEST_FAILURE only for actual nonzero command or test execution, a malformed "
        "or missing required receipt, an experiment defect, or inability to execute "
        "verification. Do not use Sharpe as the gate rationale.\n\n"
    )


def _workspace_isolation_contract(state: AutoresearchState, phase: Phase) -> str:
    if phase is Phase.IMPLEMENTATION:
        worktree_root = _render_literal(str(DEFAULT_AUTORESEARCH_WORKTREE_ROOT))
        return (
            "Workspace isolation contract:\n"
            "- Create and use a disposable git worktree for this iteration under the "
            f"canonical operator-controlled root {worktree_root}; "
            "before git worktree add, run mkdir -p "
            "/home/dev/.openclaw/autoresearch/worktrees. "
            "never use /tmp. /tmp is a 31G tmpfs, and each Quantipy worktree virtualenv "
            "is about 1.5G, so stale iteration worktrees exhaust it. Do not implement "
            "directly in the main target repo checkout.\n"
            "- Do not leave background experiment, notebook, pytest, or data-generation "
            "processes running after the stage exits.\n"
            "- Commit all accepted implementation changes before emitting the artifact; if "
            "the worktree cannot be made clean, fail closed and report that blocker.\n"
            "- Include the disposable worktree path in workspace_path and the accepted "
            "commit SHA in commit_sha.\n"
            "- Preserve unrelated user files such as "
            "docs/quantipy_experiment_mempalace_preload.md.\n\n"
        )
    if phase is not Phase.FIX_TEST:
        return ""
    if state.implementation_result is None:
        raise AutoresearchValidationError(
            "fix_test workspace contract requires implementation_result"
        )
    return (
        "Fix/Test workspace continuity contract:\n"
        "- From the verified authoritative state, reuse the exact persisted implementation "
        "worktree and accepted implementation commit. Never create another worktree or edit "
        "the main target checkout.\n"
        "- Before editing, require a clean or recoverable Git state. Do not discard or "
        "overwrite unrelated changes; if reconciliation is ambiguous or would lose "
        "unrelated work, fail closed and report the blocker.\n"
        "- If the authoritative target checkout advanced because human/Codex promoted "
        "shared infrastructure, incorporate that already-authoritative history into this "
        "same experiment worktree while preserving the accepted experiment commit. Never "
        "independently edit shared infrastructure.\n"
        "- Do not leave background experiment, notebook, pytest, or data-generation "
        "processes running after the stage exits.\n"
        "- Finish with a clean, committed result. The fix_result artifact must use the same "
        "verified workspace_path exactly and report its accepted final commit SHA in commit_sha.\n"
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
    _validate_state(state, policy)
    if state.suspended:
        raise AutoresearchValidationError(
            "autoresearch is suspended on an infrastructure blocker; "
            "run autoresearch-resume after platform readiness changes"
        )
    try:
        validate_state_readiness(state.platform_readiness, readiness)
        validation_context = AutoresearchValidationContext.from_readiness(readiness)
        validation_context.validate_for_state(state)
        if state.phase is not Phase.REVIEW:
            _revalidate_accepted_member_union_manifests(state)
        _validate_alpha_verification_price_preflight(state)
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
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
    _validate_state(state, policy)
    if state.mode is ResearchMode.ALPHA_RESEARCH and state.phase is Phase.VERIFICATION:
        if validation_context is None:
            raise AutoresearchValidationError(
                "ALPHA_RESEARCH artifact advancement requires a strict readiness validation context"
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
            if state.mode is ResearchMode.ALPHA_RESEARCH and artifact.universe_plan is None:
                raise AutoresearchValidationError(
                    "ALPHA_RESEARCH majority consensus requires a frozen universe_plan"
                )
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
        next_verification_history = (*state.verification_history, artifact)
        if artifact.status is VerificationStatus.PASS:
            next_state = replace(
                state,
                verification_history=next_verification_history,
                pending_fix_trigger=None,
                phase=Phase.REVIEW,
            )
            _validate_alpha_universe_chain(next_state, validation_context)
            return next_state
        if (
            artifact.status is VerificationStatus.TEST_FAILURE
            and state.verification_fix_attempts >= 2
        ):
            next_state = replace(
                state,
                verification_history=next_verification_history,
                pending_fix_trigger=None,
                phase=Phase.DECISION_LOG,
            )
            _validate_alpha_universe_chain(next_state, validation_context)
            return next_state
        next_state = replace(
            state,
            verification_history=next_verification_history,
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
        _validate_fix_workspace(state, artifact)
        assert state.implementation_result is not None
        next_implementation = replace(
            state.implementation_result,
            commit_sha=artifact.commit_sha,
        )
        return replace(
            state,
            implementation_result=next_implementation,
            fix_history=(*state.fix_history, artifact),
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
        _validate_final_decision_artifact(artifact, state)
        if artifact.decision is FinalDecision.INFRA_BLOCKED:
            return replace(
                state,
                final_decision=artifact,
                phase=Phase.REPEAT,
                suspended=True,
                suspension_reason=artifact.infra_rationale,
            )
        return replace(state, final_decision=artifact, phase=Phase.REPEAT)

    raise AutoresearchValidationError(
        "repeat phase does not accept artifacts; mark memory or start next iteration"
    )


def can_write_memory(state: AutoresearchState) -> bool:
    return (
        state.phase is Phase.REPEAT
        and state.final_decision is not None
        and state.final_decision.memory_write_required
    )


def _is_explicit_no_memory_transition(state: AutoresearchState) -> bool:
    if _is_operator_precondition_no_memory_state(state):
        return True
    decision = state.final_decision
    return (
        state.phase is Phase.REPEAT
        and decision is not None
        and decision.decision is FinalDecision.NO_CONSENSUS
        and not decision.memory_write_required
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


def _is_operator_precondition_no_memory_state(state: AutoresearchState) -> bool:
    decision = state.final_decision
    return (
        state.phase is Phase.REPEAT
        and state.suspended
        and decision is not None
        and decision.decision is FinalDecision.INFRA_BLOCKED
        and decision.reviewer_verdict is FinalReviewerVerdict.NOT_RUN
        and decision.recommended_metric_value is None
        and bool(decision.infra_rationale)
        and not decision.memory_write_required
        and _is_operator_precondition_consensus(state.latest_consensus)
        and state.implementation_result is None
        and state.latest_verification is None
        and not state.memory_written
        and state.memory_verification_receipt is None
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
        and (
            (
                state.implementation_result is not None
                and latest_verification is not None
                and latest_verification.infra_gate_outcome is InfraGateOutcome.REMEDIATION_REQUIRED
            )
            or (
                state.implementation_result is None
                and state.latest_consensus is not None
                and state.latest_consensus.status is ConsensusStatus.NO_CONSENSUS
            )
        )
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


def verify_mempalace_final_decision(
    state: AutoresearchState,
    kg_path: Path | None = None,
) -> MemoryVerificationReceipt:
    """Read and attest KG facts; this function never mutates MemPalace."""
    if state.final_decision is None or state.mode is None:
        raise AutoresearchValidationError("MemPalace verification requires final_decision and mode")
    if not state.final_decision.memory_write_required:
        raise AutoresearchValidationError(
            "MemPalace verification is not required for this final decision"
        )
    path = (kg_path if kg_path is not None else _default_mempalace_kg_path()).expanduser()
    if not path.is_file():
        raise AutoresearchValidationError(f"MemPalace KG does not exist: {path}")
    decision = state.final_decision
    expected_objects = standardized_mempalace_kg_facts(state)
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
        if not source_file and not source_drawer_id:
            raise AutoresearchValidationError(
                "MemPalace standardized facts require source_file or source_drawer_id"
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
            "cannot start next iteration before memory is written or an explicit "
            "NO_CONSENSUS no-memory transition"
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


def load_artifact_file(
    path: Path,
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    *,
    instruction_manifest_sha256: str,
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

    _validate_state(state, policy)
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
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchValidationError(f"missing state file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchValidationError(f"invalid state JSON: {path}") from exc
    return normalize_autoresearch_state(AutoresearchState.from_dict(raw))


def migrate_state_file(source_path: Path, output_path: Path) -> AutoresearchState:
    """Explicitly migrate the sole lossless schema-less live-state shape to v2."""
    resolved_source_path = source_path.expanduser().resolve(strict=False)
    resolved_output_path = output_path.expanduser().resolve(strict=False)
    with _exclusive_state_locks((resolved_source_path, resolved_output_path)):
        try:
            raw = json.loads(resolved_source_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AutoresearchValidationError(
                f"missing state file: {resolved_source_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise AutoresearchValidationError(
                f"invalid state JSON: {resolved_source_path}"
            ) from exc
        data = _ensure_mapping(raw, label="autoresearch_state")
        if "schema_version" in data:
            state = AutoresearchState.from_dict(data)
        else:
            expected_schema_less = set(AutoresearchState().to_dict()) - {"schema_version"}
            pristine_values: dict[str, object] = {
                "phase": Phase.SETUP_CONTEXT.value,
                "iteration": 1,
                "consensus_retry_count": 0,
                "verification_fix_attempts": 0,
                "setup": None,
                "context_packet": None,
                "debate_rounds": [],
                "consensus_history": [],
                "implementation_result": None,
                "verification_history": [],
                "review_history": [],
                "fix_history": [],
                "pending_fix_trigger": None,
                "final_decision": None,
                "memory_written": False,
                "mode": None,
                "memory_verification_receipt": None,
                "suspended": False,
                "suspension_reason": None,
            }
            is_pristine = set(data) == expected_schema_less and all(
                data.get(key) == value for key, value in pristine_values.items()
            )
            if not is_pristine or data.get("platform_readiness") is None:
                raise AutoresearchValidationError(
                    "schema-less historical state is incompatible with lossless v2 migration. "
                    "Archive it, then start a new campaign with `gateway-cli "
                    "autoresearch-init-state --output <new-state.json> "
                    "--readiness-manifest <platform-readiness.json>`."
                )
            migrated = dict(data)
            migrated["schema_version"] = AUTORESEARCH_STATE_SCHEMA_VERSION
            state = AutoresearchState.from_dict(migrated)
        _atomic_save_state_file(resolved_output_path, state)
        return state


def initialize_state(readiness: PlatformReadinessManifest) -> AutoresearchState:
    """Create a pristine v2 campaign state pinned to authoritative readiness."""
    try:
        identity = readiness.require_ready()
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    return AutoresearchState(platform_readiness=identity)


def save_state_file(path: Path, state: AutoresearchState) -> None:
    resolved_path = path.expanduser().resolve(strict=False)
    with _exclusive_state_locks((resolved_path,)):
        _atomic_save_state_file(resolved_path, state)


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
        _atomic_save_state_file(resolved_output_path, derived_state)


def _atomic_save_state_file(path: Path, state: AutoresearchState) -> None:
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
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def normalize_autoresearch_state(state: AutoresearchState) -> AutoresearchState:
    """Repair persisted states whose deterministic routing was tightened."""
    if (
        state.phase is Phase.IMPLEMENTATION
        and state.implementation_result is None
        and _is_operator_precondition_consensus(state.latest_consensus)
    ):
        return replace(state, phase=Phase.DECISION_LOG)
    return state
