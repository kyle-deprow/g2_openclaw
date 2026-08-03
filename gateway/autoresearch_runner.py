"""Deterministic control-plane for the Quantipy autoresearch loop.

This module owns the fixed phase graph, stage-agent policy validation,
skill/source receipts, artifact validation, and next-action selection.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math  # noqa: F401
import os
import platform  # noqa: F401
import re
import shutil  # noqa: F401
import sqlite3  # noqa: F401
import stat
import subprocess
import tempfile
import time
import tomllib
from bisect import bisect_right  # noqa: F401
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from ctypes.util import find_library  # noqa: F401
from dataclasses import dataclass, field, replace  # noqa: F401
from datetime import UTC as UTC
from datetime import date, datetime  # noqa: F401
from enum import StrEnum  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast  # noqa: F401
from urllib.parse import unquote, urlencode, urlparse  # noqa: F401

from gateway.autoresearch.artifacts import (
    ARTIFACT_CONTRACTS as ARTIFACT_CONTRACTS,
)
from gateway.autoresearch.artifacts import (
    ConsensusResultArtifact as ConsensusResultArtifact,
)
from gateway.autoresearch.artifacts import (
    ContextPacketArtifact as ContextPacketArtifact,
)
from gateway.autoresearch.artifacts import (
    DebateResultArtifact as DebateResultArtifact,
)
from gateway.autoresearch.artifacts import (
    DebateSubmission as DebateSubmission,
)
from gateway.autoresearch.artifacts import (
    FinalDecisionArtifact as FinalDecisionArtifact,
)
from gateway.autoresearch.artifacts import (
    FixResultArtifact as FixResultArtifact,
)
from gateway.autoresearch.artifacts import (
    ImplementationResultArtifact as ImplementationResultArtifact,
)
from gateway.autoresearch.artifacts import (
    MemoryVerificationReceipt as MemoryVerificationReceipt,
)
from gateway.autoresearch.artifacts import (
    NextAction as NextAction,
)
from gateway.autoresearch.artifacts import (
    PhaseTarget as PhaseTarget,
)
from gateway.autoresearch.artifacts import (
    PriceHydrationScopePreflight as PriceHydrationScopePreflight,
)
from gateway.autoresearch.artifacts import (
    QuantipyExecutionNotStartedEvidence as QuantipyExecutionNotStartedEvidence,
)
from gateway.autoresearch.artifacts import (
    QuantipyExperimentEvidence as QuantipyExperimentEvidence,
)
from gateway.autoresearch.artifacts import (
    QuantipyExperimentFailureEvidence as QuantipyExperimentFailureEvidence,
)
from gateway.autoresearch.artifacts import (
    QuantipyExperimentPanelEvidence as QuantipyExperimentPanelEvidence,
)
from gateway.autoresearch.artifacts import (
    ReviewResultArtifact as ReviewResultArtifact,
)
from gateway.autoresearch.artifacts import (
    SetupContextArtifact as SetupContextArtifact,
)
from gateway.autoresearch.artifacts import (
    UniversePlanArtifact as UniversePlanArtifact,
)
from gateway.autoresearch.artifacts import (
    VerificationResultArtifact as VerificationResultArtifact,
)
from gateway.autoresearch.compute import (
    _GPU_PROBE_MODULES as _GPU_PROBE_MODULES,
)
from gateway.autoresearch.compute import (
    ComputeCapabilitySnapshot as ComputeCapabilitySnapshot,
)
from gateway.autoresearch.compute import (
    ComputeFitArtifact as ComputeFitArtifact,
)
from gateway.autoresearch.compute import (
    StageAgentPolicy as StageAgentPolicy,
)
from gateway.autoresearch.compute import (
    _probe_cuda_runtime as _probe_cuda_runtime,
)
from gateway.autoresearch.compute import (
    _probe_installed_gpu_packages as _probe_installed_gpu_packages,
)
from gateway.autoresearch.compute import (
    _probe_nvidia as _probe_nvidia,
)
from gateway.autoresearch.compute import (
    _read_memory_gib as _read_memory_gib,
)
from gateway.autoresearch.compute import (
    _target_python_path as _target_python_path,
)
from gateway.autoresearch.compute import (
    collect_compute_capability_snapshot as collect_compute_capability_snapshot,
)
from gateway.autoresearch.constants import (
    _OPERATOR_PRECONDITION_BRIEF_MARKERS as _OPERATOR_PRECONDITION_BRIEF_MARKERS,
)
from gateway.autoresearch.constants import (
    _OPERATOR_PRECONDITION_MARKERS as _OPERATOR_PRECONDITION_MARKERS,
)
from gateway.autoresearch.constants import (
    AUTHORITATIVE_STATE_DIGEST_DOMAIN as AUTHORITATIVE_STATE_DIGEST_DOMAIN,
)
from gateway.autoresearch.constants import (
    AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN as AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN,
)
from gateway.autoresearch.constants import (
    AUTHORITATIVE_STATE_REFERENCE_VERSION as AUTHORITATIVE_STATE_REFERENCE_VERSION,
)
from gateway.autoresearch.constants import (
    AUTORESEARCH_LOCK_NAMESPACE as AUTORESEARCH_LOCK_NAMESPACE,
)
from gateway.autoresearch.constants import (
    AUTORESEARCH_STATE_LOCK_DIGEST_DOMAIN as AUTORESEARCH_STATE_LOCK_DIGEST_DOMAIN,
)
from gateway.autoresearch.constants import (
    AUTORESEARCH_STATE_SCHEMA_VERSION as AUTORESEARCH_STATE_SCHEMA_VERSION,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES as CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES as CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_PYPROJECT_MAX_BYTES as CANONICAL_QUANTIPY_PYPROJECT_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_UV_LOCK_MAX_BYTES as CANONICAL_QUANTIPY_UV_LOCK_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    DEFAULT_ALLOWED_TARGET_STATUS_LINES as DEFAULT_ALLOWED_TARGET_STATUS_LINES,
)
from gateway.autoresearch.constants import (
    DEFAULT_AUTORESEARCH_MODEL_WORKSPACE_ROOT as DEFAULT_AUTORESEARCH_MODEL_WORKSPACE_ROOT,
)
from gateway.autoresearch.constants import (
    DEFAULT_AUTORESEARCH_STAGE_INBOX as DEFAULT_AUTORESEARCH_STAGE_INBOX,
)
from gateway.autoresearch.constants import (
    DEFAULT_AUTORESEARCH_STATE_PATH as DEFAULT_AUTORESEARCH_STATE_PATH,
)
from gateway.autoresearch.constants import (
    DEFAULT_AUTORESEARCH_WORKTREE_ROOT as DEFAULT_AUTORESEARCH_WORKTREE_ROOT,
)
from gateway.autoresearch.constants import (
    DEFAULT_OPENCLAW_CONFIG_PATH as DEFAULT_OPENCLAW_CONFIG_PATH,
)
from gateway.autoresearch.constants import (
    DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT as DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
)
from gateway.autoresearch.constants import (
    DEFAULT_QUANTIPY_ROOT as DEFAULT_QUANTIPY_ROOT,
)
from gateway.autoresearch.constants import (
    EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION as EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.constants import (
    G2_CONTROL_MODULE as G2_CONTROL_MODULE,
)
from gateway.autoresearch.constants import (
    G2_CONTROL_RUNTIME_NAMESPACE as G2_CONTROL_RUNTIME_NAMESPACE,
)
from gateway.autoresearch.constants import (
    G2_CONTROL_SERVER_ID as G2_CONTROL_SERVER_ID,
)
from gateway.autoresearch.constants import (
    G2_CONTROL_TOOL_NAMES as G2_CONTROL_TOOL_NAMES,
)
from gateway.autoresearch.constants import (
    HYDRATE_CAPABLE_COMMAND_RE as HYDRATE_CAPABLE_COMMAND_RE,
)
from gateway.autoresearch.constants import (
    INSTRUCTION_SOURCE_MANIFEST_DIGEST_DOMAIN as INSTRUCTION_SOURCE_MANIFEST_DIGEST_DOMAIN,
)
from gateway.autoresearch.constants import (
    INSTRUCTION_SOURCE_MANIFEST_VERSION as INSTRUCTION_SOURCE_MANIFEST_VERSION,
)
from gateway.autoresearch.constants import (
    INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_ENV_VAR as INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_ENV_VAR,  # noqa: E501
)
from gateway.autoresearch.constants import (
    INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_VALUE as INTERRUPTED_VERIFICATION_RECOVERY_OPERATOR_VALUE,  # noqa: E501
)
from gateway.autoresearch.constants import (
    INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION as INTERRUPTED_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.constants import (
    LEGACY_AUTORESEARCH_WORKTREE_ROOT as LEGACY_AUTORESEARCH_WORKTREE_ROOT,
)
from gateway.autoresearch.constants import (
    LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION as LEGACY_EXTERNAL_VERIFICATION_RETRY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.constants import (
    MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS as MAX_ALPHA_PRICE_HYDRATION_SYMBOL_SESSIONS,
)
from gateway.autoresearch.constants import (
    MAX_ARTIFACT_FILE_BYTES as MAX_ARTIFACT_FILE_BYTES,
)
from gateway.autoresearch.constants import (
    MAX_EXAMPLE_TICKERS as MAX_EXAMPLE_TICKERS,
)
from gateway.autoresearch.constants import (
    MAX_FIXED_SLEEVE_SYMBOLS as MAX_FIXED_SLEEVE_SYMBOLS,
)
from gateway.autoresearch.constants import (
    MAX_NEXT_ACTION_PROMPT_BYTES as MAX_NEXT_ACTION_PROMPT_BYTES,
)
from gateway.autoresearch.constants import (
    MAX_STAGE_SUBMISSION_BYTES as MAX_STAGE_SUBMISSION_BYTES,
)
from gateway.autoresearch.constants import (
    MAX_UNIVERSE_BATCH_DATES as MAX_UNIVERSE_BATCH_DATES,
)
from gateway.autoresearch.constants import (
    MAX_UNIVERSE_BATCH_RESULTS as MAX_UNIVERSE_BATCH_RESULTS,
)
from gateway.autoresearch.constants import (
    MAX_UNIVERSE_MEMBERS_PER_DATE as MAX_UNIVERSE_MEMBERS_PER_DATE,
)
from gateway.autoresearch.constants import (
    MAX_UNIVERSE_SELECTION_DATES as MAX_UNIVERSE_SELECTION_DATES,
)
from gateway.autoresearch.constants import (
    MEMBER_UNION_DIGEST_ALGORITHM as MEMBER_UNION_DIGEST_ALGORITHM,
)
from gateway.autoresearch.constants import (
    MEMPALACE_CONFIG_PLACEHOLDER as MEMPALACE_CONFIG_PLACEHOLDER,
)
from gateway.autoresearch.constants import (
    MEMPALACE_KG_OBJECT_MAX_LENGTH as MEMPALACE_KG_OBJECT_MAX_LENGTH,
)
from gateway.autoresearch.constants import (
    MEMPALACE_KG_OBJECT_SHA256_LENGTH as MEMPALACE_KG_OBJECT_SHA256_LENGTH,
)
from gateway.autoresearch.constants import (
    MEMPALACE_READONLY_DISPLAY_NAMESPACE as MEMPALACE_READONLY_DISPLAY_NAMESPACE,
)
from gateway.autoresearch.constants import (
    MEMPALACE_READONLY_RUNTIME_NAMESPACE as MEMPALACE_READONLY_RUNTIME_NAMESPACE,
)
from gateway.autoresearch.constants import (
    MEMPALACE_READONLY_SERVER_ID as MEMPALACE_READONLY_SERVER_ID,
)
from gateway.autoresearch.constants import (
    MEMPALACE_READONLY_TOOL_NAMES as MEMPALACE_READONLY_TOOL_NAMES,
)
from gateway.autoresearch.constants import (
    MEMPALACE_READONLY_WRAPPER_BASENAME as MEMPALACE_READONLY_WRAPPER_BASENAME,
)
from gateway.autoresearch.constants import (
    NEXT_ACTION_PROMPT_TARGET_BYTES as NEXT_ACTION_PROMPT_TARGET_BYTES,
)
from gateway.autoresearch.constants import (
    NEXT_SESSION_EXECUTION_POLICY as NEXT_SESSION_EXECUTION_POLICY,
)
from gateway.autoresearch.constants import (
    OPENCLAW_LONG_TASK_UNIT_RE as OPENCLAW_LONG_TASK_UNIT_RE,
)
from gateway.autoresearch.constants import (
    OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY as OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY,  # noqa: E501
)
from gateway.autoresearch.constants import (
    OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME as OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME,  # noqa: E501
)
from gateway.autoresearch.constants import (
    OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE as OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE,
)
from gateway.autoresearch.constants import (
    PLATFORM_RUNTIME_RECOVERY_OPERATOR_ENV_VAR as PLATFORM_RUNTIME_RECOVERY_OPERATOR_ENV_VAR,
)
from gateway.autoresearch.constants import (
    PLATFORM_RUNTIME_RECOVERY_OPERATOR_VALUE as PLATFORM_RUNTIME_RECOVERY_OPERATOR_VALUE,
)
from gateway.autoresearch.constants import (
    PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION as PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.constants import (
    PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS as PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXECUTION_NOT_STARTED_REASONS as QUANTIPY_EXECUTION_NOT_STARTED_REASONS,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXECUTION_NOT_STARTED_TOMBSTONE as QUANTIPY_EXECUTION_NOT_STARTED_TOMBSTONE,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_FAILURE_CATEGORIES as QUANTIPY_EXPERIMENT_FAILURE_CATEGORIES,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_FAILURE_MESSAGE_MAX_LENGTH as QUANTIPY_EXPERIMENT_FAILURE_MESSAGE_MAX_LENGTH,  # noqa: E501
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_IDENTITY_PATH_MAX_LENGTH as QUANTIPY_EXPERIMENT_IDENTITY_PATH_MAX_LENGTH,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_NOTEBOOK_MAX_BYTES as QUANTIPY_EXPERIMENT_NOTEBOOK_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_SCHEMA_VERSION as QUANTIPY_EXPERIMENT_SCHEMA_VERSION,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_SOURCE_DIGEST_DOMAIN as QUANTIPY_EXPERIMENT_SOURCE_DIGEST_DOMAIN,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_BYTES as QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_COUNT as QUANTIPY_EXPERIMENT_SOURCE_FILE_MAX_COUNT,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_SOURCE_PATH_MAX_LENGTH as QUANTIPY_EXPERIMENT_SOURCE_PATH_MAX_LENGTH,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_SOURCE_TOTAL_MAX_BYTES as QUANTIPY_EXPERIMENT_SOURCE_TOTAL_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_STAGE_ORDER as QUANTIPY_EXPERIMENT_STAGE_ORDER,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_STAGE_SUMMARY_MAX_LENGTH as QUANTIPY_EXPERIMENT_STAGE_SUMMARY_MAX_LENGTH,
)
from gateway.autoresearch.constants import (
    QUANTIPY_PANEL_RECEIPT_MAX_BYTES as QUANTIPY_PANEL_RECEIPT_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    QUANTIPY_RUN_ENVELOPE_MAX_BYTES as QUANTIPY_RUN_ENVELOPE_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    XNYS_CALENDAR_IDENTITY as XNYS_CALENDAR_IDENTITY,
)
from gateway.autoresearch.engine import (
    _build_prompt_text as _build_prompt_text,
)
from gateway.autoresearch.engine import (
    _phase_instruction as _phase_instruction,
)
from gateway.autoresearch.engine import (
    _verification_handoff_contract as _verification_handoff_contract,
)
from gateway.autoresearch.engine import (
    _workspace_isolation_contract as _workspace_isolation_contract,
)
from gateway.autoresearch.enums import (
    ArtifactType as ArtifactType,
)
from gateway.autoresearch.enums import (
    ComputeTarget as ComputeTarget,
)
from gateway.autoresearch.enums import (
    ConsensusStatus as ConsensusStatus,
)
from gateway.autoresearch.enums import (
    FinalDecision as FinalDecision,
)
from gateway.autoresearch.enums import (
    FinalReviewerVerdict as FinalReviewerVerdict,
)
from gateway.autoresearch.enums import (
    FixTriggerPhase as FixTriggerPhase,
)
from gateway.autoresearch.enums import (
    InfraGateOutcome as InfraGateOutcome,
)
from gateway.autoresearch.enums import (
    MetricDirection as MetricDirection,
)
from gateway.autoresearch.enums import (
    Phase as Phase,
)
from gateway.autoresearch.enums import (
    ResearchMode as ResearchMode,
)
from gateway.autoresearch.enums import (
    ReviewVerdict as ReviewVerdict,
)
from gateway.autoresearch.enums import (
    VerificationStatus as VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchConfigError as AutoresearchConfigError,
)
from gateway.autoresearch.errors import (
    AutoresearchError as AutoresearchError,
)
from gateway.autoresearch.errors import (
    AutoresearchReceiptError as AutoresearchReceiptError,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.evidence import (
    QuantipyExecutionContract as QuantipyExecutionContract,
)
from gateway.autoresearch.evidence import (
    _build_historical_v2_quantipy_execution_contract as _build_historical_v2_quantipy_execution_contract,  # noqa: E501
)
from gateway.autoresearch.evidence import (
    _canonical_quantipy_manifest_sha256 as _canonical_quantipy_manifest_sha256,
)
from gateway.autoresearch.evidence import (
    _expected_local_research_panel_http_error_message as _expected_local_research_panel_http_error_message,  # noqa: E501
)
from gateway.autoresearch.evidence import (
    _git_show_committed_bytes as _git_show_committed_bytes,
)
from gateway.autoresearch.evidence import (
    _git_tree_file_paths as _git_tree_file_paths,
)
from gateway.autoresearch.evidence import (
    _is_quantipy_package_source_path as _is_quantipy_package_source_path,
)
from gateway.autoresearch.evidence import (
    _quantipy_experiment_source_digest as _quantipy_experiment_source_digest,
)
from gateway.autoresearch.evidence import (
    _reserve_quantipy_execution_not_started as _reserve_quantipy_execution_not_started,
)
from gateway.autoresearch.evidence import (
    _run_failure_from_mapping as _run_failure_from_mapping,
)
from gateway.autoresearch.evidence import (
    _secure_package_file_paths as _secure_package_file_paths,
)
from gateway.autoresearch.evidence import (
    _target_repo_root_for_state as _target_repo_root_for_state,
)
from gateway.autoresearch.evidence import (
    _validate_panel_receipt as _validate_panel_receipt,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_committed_sources as _validate_quantipy_committed_sources,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_execution_source_against_commit as _validate_quantipy_execution_source_against_commit,  # noqa: E501
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_failure as _validate_quantipy_failure,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_run_envelope as _validate_quantipy_run_envelope,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_run_panel as _validate_quantipy_run_panel,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_run_source as _validate_quantipy_run_source,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_v2_manifest as _validate_quantipy_v2_manifest,
)
from gateway.autoresearch.evidence import (
    _verified_panel_request_for_state as _verified_panel_request_for_state,
)
from gateway.autoresearch.evidence import (
    build_quantipy_execution_contract as build_quantipy_execution_contract,
)
from gateway.autoresearch.fields import (
    _canonical_json_digest as _canonical_json_digest,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _normalise_identifier as _normalise_identifier,
)
from gateway.autoresearch.fields import (
    _normalise_predicate as _normalise_predicate,
)
from gateway.autoresearch.fields import (
    _optional_float as _optional_float,
)
from gateway.autoresearch.fields import (
    _optional_int as _optional_int,
)
from gateway.autoresearch.fields import (
    _optional_string_list as _optional_string_list,
)
from gateway.autoresearch.fields import (
    _parse_timestamp as _parse_timestamp,
)
from gateway.autoresearch.fields import (
    _parse_utc_request_datetime as _parse_utc_request_datetime,
)
from gateway.autoresearch.fields import (
    _require_bool as _require_bool,
)
from gateway.autoresearch.fields import (
    _require_canonical_identifier as _require_canonical_identifier,
)
from gateway.autoresearch.fields import (
    _require_exact_keys as _require_exact_keys,
)
from gateway.autoresearch.fields import (
    _require_float as _require_float,
)
from gateway.autoresearch.fields import (
    _require_int as _require_int,
)
from gateway.autoresearch.fields import (
    _require_iso_date as _require_iso_date,
)
from gateway.autoresearch.fields import (
    _require_sha256 as _require_sha256,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
)
from gateway.autoresearch.fields import (
    _require_string_list as _require_string_list,
)
from gateway.autoresearch.fields import (
    _require_string_sequence as _require_string_sequence,
)
from gateway.autoresearch.fields import (
    _require_workspace_path as _require_workspace_path,
)
from gateway.autoresearch.fields import (
    _sha256_text as _sha256_text,
)
from gateway.autoresearch.fields import (
    _strict_json_bool as _strict_json_bool,
)
from gateway.autoresearch.fields import (
    _strict_json_date as _strict_json_date,
)
from gateway.autoresearch.fields import (
    _strict_json_datetime as _strict_json_datetime,
)
from gateway.autoresearch.fields import (
    _strict_json_enum as _strict_json_enum,
)
from gateway.autoresearch.fields import (
    _strict_json_float as _strict_json_float,
)
from gateway.autoresearch.fields import (
    _strict_json_int as _strict_json_int,
)
from gateway.autoresearch.fields import (
    _strict_json_keys as _strict_json_keys,
)
from gateway.autoresearch.fields import (
    _strict_json_sha256 as _strict_json_sha256,
)
from gateway.autoresearch.fields import (
    _strict_json_string as _strict_json_string,
)
from gateway.autoresearch.fields import (
    _validate_iso_date_value as _validate_iso_date_value,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)
from gateway.autoresearch.fields import (
    _validate_workspace_path as _validate_workspace_path,
)
from gateway.autoresearch.fields import (
    canonical_member_union_digest as canonical_member_union_digest,
)
from gateway.autoresearch.fields import (
    canonical_member_union_manifest as canonical_member_union_manifest,
)
from gateway.autoresearch.fields import (
    platform_requested_sessions_digest as platform_requested_sessions_digest,
)
from gateway.autoresearch.fields import (
    price_hydration_coverage_digest as price_hydration_coverage_digest,
)
from gateway.autoresearch.fields import (
    price_hydration_request_digest as price_hydration_request_digest,
)
from gateway.autoresearch.fields import (
    quantipy_member_union_digest as quantipy_member_union_digest,
)
from gateway.autoresearch.gitops import (
    _path_under_root as _path_under_root,
)
from gateway.autoresearch.gitops import (
    _render_literal as _render_literal,
)
from gateway.autoresearch.gitops import (
    _require_artifact_origin_matches_target as _require_artifact_origin_matches_target,
)
from gateway.autoresearch.gitops import (
    _require_clean_git_worktree as _require_clean_git_worktree,
)
from gateway.autoresearch.gitops import (
    _require_git_descends_from as _require_git_descends_from,
)
from gateway.autoresearch.gitops import (
    _require_git_output as _require_git_output,
)
from gateway.autoresearch.gitops import (
    _require_git_success as _require_git_success,
)
from gateway.autoresearch.gitops import (
    _require_git_worktree_root as _require_git_worktree_root,
)
from gateway.autoresearch.gitops import (
    _require_isolated_git_clone_root as _require_isolated_git_clone_root,
)
from gateway.autoresearch.gitops import (
    _require_strict_canonical_workspace_path as _require_strict_canonical_workspace_path,
)
from gateway.autoresearch.gitops import (
    _require_workspace_under_autoresearch_worktree_root as _require_workspace_under_autoresearch_worktree_root,  # noqa: E501
)
from gateway.autoresearch.gitops import (
    _resolve_git_commit as _resolve_git_commit,
)
from gateway.autoresearch.gitops import (
    _run_git as _run_git,
)
from gateway.autoresearch.lifecycle import (
    OPERATOR_INFRASTRUCTURE_SUSPENSION_ACTIVE_PHASES as OPERATOR_INFRASTRUCTURE_SUSPENSION_ACTIVE_PHASES,  # noqa: E501
)
from gateway.autoresearch.lifecycle import (
    resume_suspended_iteration as resume_suspended_iteration,
)
from gateway.autoresearch.lifecycle import (
    start_next_iteration as start_next_iteration,
)
from gateway.autoresearch.lifecycle import (
    suspend_for_infrastructure as suspend_for_infrastructure,
)
from gateway.autoresearch.manifest import (
    AuthoritativeStateReference as AuthoritativeStateReference,
)
from gateway.autoresearch.manifest import (
    InstructionSourceEntry as InstructionSourceEntry,
)
from gateway.autoresearch.manifest import (
    InstructionSourceManifest as InstructionSourceManifest,
)
from gateway.autoresearch.manifest import (
    SourceReceipt as SourceReceipt,
)
from gateway.autoresearch.memory import (
    G2_OPENCLAW_REPO_ROOT as G2_OPENCLAW_REPO_ROOT,
)
from gateway.autoresearch.memory import (
    _committed_finalization_journal_drawer_id as _committed_finalization_journal_drawer_id,
)
from gateway.autoresearch.memory import (
    _default_mempalace_kg_path as _default_mempalace_kg_path,
)
from gateway.autoresearch.memory import (
    _is_explicit_no_memory_transition as _is_explicit_no_memory_transition,
)
from gateway.autoresearch.memory import (
    _standard_data_window_object as _standard_data_window_object,
)
from gateway.autoresearch.memory import (
    _standard_metric_object as _standard_metric_object,
)
from gateway.autoresearch.memory import (
    build_final_memory_write_request as build_final_memory_write_request,
)
from gateway.autoresearch.memory import (
    can_write_memory as can_write_memory,
)
from gateway.autoresearch.memory import (
    finalize_repeat_memory as finalize_repeat_memory,
)
from gateway.autoresearch.memory import (
    mark_memory_written as mark_memory_written,
)
from gateway.autoresearch.memory import (
    standardize_mempalace_kg_object as standardize_mempalace_kg_object,
)
from gateway.autoresearch.memory import (
    standardized_mempalace_kg_facts as standardized_mempalace_kg_facts,
)
from gateway.autoresearch.memory import (
    verify_mempalace_final_decision as verify_mempalace_final_decision,
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
    _operator_precondition_decision_instruction as _operator_precondition_decision_instruction,
)
from gateway.autoresearch.prompts import (
    _render_instruction_source_manifest as _render_instruction_source_manifest,
)
from gateway.autoresearch.prompts import (
    _select_phase_target as _select_phase_target,
)
from gateway.autoresearch.receipts import (
    AggregateCoverageReceipt as AggregateCoverageReceipt,
)
from gateway.autoresearch.receipts import (
    AuthoritativeSnapshotReceipt as AuthoritativeSnapshotReceipt,
)
from gateway.autoresearch.receipts import (
    CoverageReceipt as CoverageReceipt,
)
from gateway.autoresearch.receipts import (
    DynamicUniverseCoverageReceipt as DynamicUniverseCoverageReceipt,
)
from gateway.autoresearch.receipts import (
    GroupedSummaryReceipt as GroupedSummaryReceipt,
)
from gateway.autoresearch.receipts import (
    MemberUnionManifestReceipt as MemberUnionManifestReceipt,
)
from gateway.autoresearch.receipts import (
    PriceHydrationReceipt as PriceHydrationReceipt,
)
from gateway.autoresearch.receipts import (
    UniverseDateVerificationReceipt as UniverseDateVerificationReceipt,
)
from gateway.autoresearch.receipts import (
    UniverseHistoryBatchReceipt as UniverseHistoryBatchReceipt,
)
from gateway.autoresearch.receipts import (
    UniverseVerificationReceipt as UniverseVerificationReceipt,
)
from gateway.autoresearch.receipts import (
    _validate_coverage_values as _validate_coverage_values,
)
from gateway.autoresearch.recovery_receipts import (
    CanonicalQuantipyRuntimeAttestation as CanonicalQuantipyRuntimeAttestation,
)
from gateway.autoresearch.recovery_receipts import (
    ExternalVerificationRetryReceipt as ExternalVerificationRetryReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    InterruptedVerificationAttemptReceipt as InterruptedVerificationAttemptReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    PlatformRuntimeRecoveryReceipt as PlatformRuntimeRecoveryReceipt,
)
from gateway.autoresearch.recovery_receipts import (
    _deterministic_quantipy_run_id as _deterministic_quantipy_run_id,
)
from gateway.autoresearch.recovery_receipts import (
    _expected_quantipy_verification_run_id as _expected_quantipy_verification_run_id,
)
from gateway.autoresearch.recovery_receipts import (
    _is_historically_authorized_local_research_panel_http_404 as _is_historically_authorized_local_research_panel_http_404,  # noqa: E501
)
from gateway.autoresearch.recovery_receipts import (
    _is_manifest_bound_legacy_local_research_panel_http_413 as _is_manifest_bound_legacy_local_research_panel_http_413,  # noqa: E501
)
from gateway.autoresearch.recovery_receipts import (
    _validate_external_verification_retry_eligibility as _validate_external_verification_retry_eligibility,  # noqa: E501
)
from gateway.autoresearch.recovery_receipts import (
    _validate_external_verification_retry_history as _validate_external_verification_retry_history,
)
from gateway.autoresearch.recovery_receipts import (
    _validate_external_verification_retry_history_artifact as _validate_external_verification_retry_history_artifact,  # noqa: E501
)
from gateway.autoresearch.recovery_receipts import (
    _verify_member_union_manifest as _verify_member_union_manifest,
)
from gateway.autoresearch.secure_io import (
    _canonical_json_sha256 as _canonical_json_sha256,
)
from gateway.autoresearch.secure_io import (
    _canonical_utc_text as _canonical_utc_text,
)
from gateway.autoresearch.secure_io import (
    _create_or_normalize_private_directory as _create_or_normalize_private_directory,
)
from gateway.autoresearch.secure_io import (
    _load_json_mapping as _load_json_mapping,
)
from gateway.autoresearch.secure_io import (
    _open_no_follow_directory as _open_no_follow_directory,
)
from gateway.autoresearch.secure_io import (
    _parse_json_snapshot as _parse_json_snapshot,
)
from gateway.autoresearch.secure_io import (
    _path_is_within as _path_is_within,
)
from gateway.autoresearch.secure_io import (
    _provision_private_quantipy_control_plane_ancestors as _provision_private_quantipy_control_plane_ancestors,  # noqa: E501
)
from gateway.autoresearch.secure_io import (
    _require_canonical_absolute_path as _require_canonical_absolute_path,
)
from gateway.autoresearch.secure_io import (
    _require_private_directory as _require_private_directory,
)
from gateway.autoresearch.secure_io import (
    _require_runtime_venv_prefix as _require_runtime_venv_prefix,
)
from gateway.autoresearch.secure_io import (
    _require_sealed_quantipy_panel_directory as _require_sealed_quantipy_panel_directory,
)
from gateway.autoresearch.secure_io import (
    _require_sealed_quantipy_panel_file as _require_sealed_quantipy_panel_file,
)
from gateway.autoresearch.secure_io import (
    _require_strict_regular_file as _require_strict_regular_file,
)
from gateway.autoresearch.secure_io import (
    _secure_open_external_uv_base_interpreter as _secure_open_external_uv_base_interpreter,
)
from gateway.autoresearch.secure_io import (
    _secure_open_snapshot as _secure_open_snapshot,
)
from gateway.autoresearch.secure_io import (
    _SecureFileSnapshot as _SecureFileSnapshot,
)
from gateway.autoresearch.secure_io import (
    _sha256_file as _sha256_file,
)
from gateway.autoresearch.secure_io import (
    _validate_panel_request as _validate_panel_request,
)
from gateway.autoresearch.secure_io import (
    _validate_quantipy_relative_path as _validate_quantipy_relative_path,
)
from gateway.autoresearch.state import (
    _T as _T,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.state import (
    AutoresearchValidationContext as AutoresearchValidationContext,
)
from gateway.autoresearch.transitions import (
    KEEP_DECISIONS as KEEP_DECISIONS,
)
from gateway.autoresearch.transitions import (
    _baseline_metric as _baseline_metric,
)
from gateway.autoresearch.transitions import (
    _canonical_iteration_experiment_id as _canonical_iteration_experiment_id,
)
from gateway.autoresearch.transitions import (
    _compact_json_block as _compact_json_block,
)
from gateway.autoresearch.transitions import (
    _extract_first_float as _extract_first_float,
)
from gateway.autoresearch.transitions import (
    _final_decision_requires_memory_write as _final_decision_requires_memory_write,
)
from gateway.autoresearch.transitions import (
    _is_authorized_no_memory_final_decision as _is_authorized_no_memory_final_decision,
)
from gateway.autoresearch.transitions import (
    _is_data_infra_g0_blocked_no_memory_state as _is_data_infra_g0_blocked_no_memory_state,
)
from gateway.autoresearch.transitions import (
    _is_fail_closed_g0_platform_contract_bug_signal as _is_fail_closed_g0_platform_contract_bug_signal,  # noqa: E501
)
from gateway.autoresearch.transitions import (
    _is_operator_infrastructure_suspension_state as _is_operator_infrastructure_suspension_state,
)
from gateway.autoresearch.transitions import (
    _is_operator_precondition_consensus as _is_operator_precondition_consensus,
)
from gateway.autoresearch.transitions import (
    _latest_verification_is_price_scope_bug_signal as _latest_verification_is_price_scope_bug_signal,  # noqa: E501
)
from gateway.autoresearch.transitions import (
    _platform_receipt_has_expected_runner_provenance as _platform_receipt_has_expected_runner_provenance,  # noqa: E501
)
from gateway.autoresearch.transitions import (
    _requested_sessions_for_preflight as _requested_sessions_for_preflight,
)
from gateway.autoresearch.transitions import (
    _require_g0_platform_provenance as _require_g0_platform_provenance,
)
from gateway.autoresearch.transitions import (
    _revalidate_accepted_member_union_manifests as _revalidate_accepted_member_union_manifests,
)
from gateway.autoresearch.transitions import (
    _validate_alpha_implementation_price_preflight as _validate_alpha_implementation_price_preflight,  # noqa: E501
)
from gateway.autoresearch.transitions import (
    _validate_alpha_price_preflight_matches_receipts as _validate_alpha_price_preflight_matches_receipts,  # noqa: E501
)
from gateway.autoresearch.transitions import (
    _validate_alpha_price_scope_verification as _validate_alpha_price_scope_verification,
)
from gateway.autoresearch.transitions import (
    _validate_alpha_universe_chain as _validate_alpha_universe_chain,
)
from gateway.autoresearch.transitions import (
    _validate_alpha_verification_price_preflight as _validate_alpha_verification_price_preflight,
)
from gateway.autoresearch.transitions import (
    _validate_consensus_history_universe_plans as _validate_consensus_history_universe_plans,
)
from gateway.autoresearch.transitions import (
    _validate_final_decision_artifact as _validate_final_decision_artifact,
)
from gateway.autoresearch.transitions import (
    _validate_final_decision_memory_requirement as _validate_final_decision_memory_requirement,
)
from gateway.autoresearch.transitions import (
    _validate_no_consensus_completion as _validate_no_consensus_completion,
)
from gateway.autoresearch.transitions import (
    _validate_operator_precondition_infra_blocked_suspension as _validate_operator_precondition_infra_blocked_suspension,  # noqa: E501
)
from gateway.autoresearch.transitions import (
    _validate_price_scope_fix_result_commands as _validate_price_scope_fix_result_commands,
)
from gateway.autoresearch.transitions import (
    build_authoritative_state_reference as build_authoritative_state_reference,
)
from gateway.autoresearch.workspace import (
    _common_git_base as _common_git_base,
)
from gateway.autoresearch.workspace import (
    _require_ancestor as _require_ancestor,
)
from gateway.autoresearch.workspace import (
    validate_target_worktree_clean as validate_target_worktree_clean,
)
from gateway.autoresearch_systemd import SystemdUnitStateError, systemd_unit_is_active
from gateway.mempalace_finalizer import (
    FINAL_MEMORY_SOURCE_FILE,  # noqa: F401
    FinalMemoryWriter,
    FinalMemoryWriteRequest,  # noqa: F401
    MempalaceFinalizationError,  # noqa: F401
    SubprocessFinalMemoryWriter,  # noqa: F401
    finalization_journal_path,  # noqa: F401
)

if TYPE_CHECKING:
    from gateway.autoresearch_runs import RunRecord

from gateway.autoresearch_panel_receipts import (
    PANEL_RECEIPT_MAX_BYTES,  # noqa: F401
    RUN_ENVELOPE_MAX_BYTES,  # noqa: F401
    PanelReceiptValidationError,  # noqa: F401
    validate_research_panel_receipt,  # noqa: F401
)
from gateway.autoresearch_platform_validation import (
    PLATFORM_COVERAGE_CONTRACT_MISMATCH_SIGNAL,  # noqa: F401
    DynamicPriceCoverageReceipt,  # noqa: F401
    PlatformCoverageStatus,  # noqa: F401
)
from gateway.autoresearch_readiness import (
    PlatformReadinessManifest,
    ReadinessIdentity,
    ResearchPanelProbeReceipt,
    load_xnys_calendar_evidence,  # noqa: F401
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


def _require_autoresearch_worktree_root() -> Path:
    root = _require_strict_canonical_workspace_path(
        str(DEFAULT_AUTORESEARCH_WORKTREE_ROOT),
        label="autoresearch worktree root",
    )
    _require_private_directory(root, label="autoresearch worktree root")
    return root


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
