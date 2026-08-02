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
import sqlite3
import stat
import subprocess
import tempfile
import time
import tomllib
from bisect import bisect_right
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from ctypes.util import find_library  # noqa: F401
from dataclasses import dataclass, field, replace
from datetime import UTC as UTC
from datetime import date, datetime
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
    XNYS_CALENDAR_IDENTITY as XNYS_CALENDAR_IDENTITY,
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
    _validate_quantipy_committed_sources as _validate_quantipy_committed_sources,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_execution_source_against_commit as _validate_quantipy_execution_source_against_commit,  # noqa: E501
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_failure as _validate_quantipy_failure,
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
from gateway.autoresearch.manifest import (
    AuthoritativeStateReference as AuthoritativeStateReference,
)
from gateway.autoresearch.manifest import (
    InstructionSourceEntry as InstructionSourceEntry,
)
from gateway.autoresearch.manifest import (
    SourceReceipt as SourceReceipt,
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

G2_OPENCLAW_REPO_ROOT = Path(__file__).resolve().parent.parent
QUANTIPY_PANEL_RECEIPT_MAX_BYTES = PANEL_RECEIPT_MAX_BYTES
QUANTIPY_RUN_ENVELOPE_MAX_BYTES = RUN_ENVELOPE_MAX_BYTES
_T = TypeVar("_T")


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


KEEP_DECISIONS = frozenset(
    {FinalDecision.KEEP, FinalDecision.SIGNIFICANT_KEEP, FinalDecision.STRONG_KEEP}
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


# R3c blocker: this context calls runner-only _verify_member_union_manifest and
# gateway.autoresearch_readiness APIs.
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


# R3c blocker: this recovery receipt depends on the runner-resident
# ExternalVerificationRetryReceipt chain and state evidence.
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


# R3c blocker: for_state calls runner-only _validate_external_verification_retry_eligibility
# and _deterministic_quantipy_run_id.
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


# R3c blocker: state still references VerificationResultArtifact, the blocked
# recovery-receipt chain, external ReadinessIdentity, and runner-local _T.
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


def _validate_panel_receipt(value: object, *, label: str) -> dict[str, object]:
    try:
        return validate_research_panel_receipt(value, label=label)
    except PanelReceiptValidationError as exc:
        raise AutoresearchValidationError(str(exc)) from exc


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
