"""Deterministic control-plane for the Quantipy autoresearch loop.

This module owns the fixed phase graph, stage-agent policy validation,
skill/source receipts, artifact validation, and next-action selection.
"""

from __future__ import annotations

import fcntl  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import math  # noqa: F401
import os  # noqa: F401
import platform  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import sqlite3  # noqa: F401
import stat  # noqa: F401
import subprocess  # noqa: F401
import tempfile  # noqa: F401
import time  # noqa: F401
import tomllib  # noqa: F401
from bisect import bisect_right  # noqa: F401
from collections.abc import (
    Callable as Callable,
)
from collections.abc import (
    Iterator as Iterator,
)
from collections.abc import (
    Mapping as Mapping,
)
from collections.abc import (
    Sequence as Sequence,
)
from contextlib import (
    ExitStack as ExitStack,
)
from contextlib import (
    contextmanager as contextmanager,
)
from contextlib import (
    suppress as suppress,
)
from ctypes.util import find_library  # noqa: F401
from dataclasses import dataclass, field, replace  # noqa: F401
from datetime import UTC as UTC
from datetime import date, datetime  # noqa: F401
from enum import StrEnum  # noqa: F401
from pathlib import Path  # noqa: F401
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
from gateway.autoresearch.attestation import (
    _attest_canonical_quantipy_runtime as _attest_canonical_quantipy_runtime,
)
from gateway.autoresearch.attestation import (
    _probe_quantipy_runtime_resolution as _probe_quantipy_runtime_resolution,
)
from gateway.autoresearch.attestation import (
    _require_canonical_verification_runtime_attestation as _require_canonical_verification_runtime_attestation,  # noqa: E501
)
from gateway.autoresearch.attestation import (
    require_canonical_verification_dispatch_attestation as require_canonical_verification_dispatch_attestation,  # noqa: E501
)
from gateway.autoresearch.attestation import (
    seal_canonical_verification_dispatch_state_file as seal_canonical_verification_dispatch_state_file,  # noqa: E501
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
from gateway.autoresearch.configuration import (
    G2_CONTROL_DISPLAY_TOOL_IDS as G2_CONTROL_DISPLAY_TOOL_IDS,
)
from gateway.autoresearch.configuration import (
    G2_CONTROL_RUNTIME_TOOL_IDS as G2_CONTROL_RUNTIME_TOOL_IDS,
)
from gateway.autoresearch.configuration import (
    MAIN_ALLOWED_TOOL_IDS as MAIN_ALLOWED_TOOL_IDS,
)
from gateway.autoresearch.configuration import (
    MAIN_OPENCLAW_TOOL_ALLOW_POLICY as MAIN_OPENCLAW_TOOL_ALLOW_POLICY,
)
from gateway.autoresearch.configuration import (
    MEMPALACE_READONLY_DISPLAY_TOOL_IDS as MEMPALACE_READONLY_DISPLAY_TOOL_IDS,
)
from gateway.autoresearch.configuration import (
    MEMPALACE_READONLY_RUNTIME_TOOL_IDS as MEMPALACE_READONLY_RUNTIME_TOOL_IDS,
)
from gateway.autoresearch.configuration import (
    _agent_policy_from_json as _agent_policy_from_json,
)
from gateway.autoresearch.configuration import (
    _codex_agent_model as _codex_agent_model,
)
from gateway.autoresearch.configuration import (
    _compile_codex_mcp_runtime_tool_ids as _compile_codex_mcp_runtime_tool_ids,
)
from gateway.autoresearch.configuration import (
    _compile_mempalace_codex_display_tool_ids as _compile_mempalace_codex_display_tool_ids,
)
from gateway.autoresearch.configuration import (
    _load_codex_agent_toml as _load_codex_agent_toml,
)
from gateway.autoresearch.configuration import (
    _load_json as _load_json,
)
from gateway.autoresearch.configuration import (
    _validate_codex_app_server_sandbox as _validate_codex_app_server_sandbox,
)
from gateway.autoresearch.configuration import (
    _validate_codex_native_stage_agents as _validate_codex_native_stage_agents,
)
from gateway.autoresearch.configuration import (
    _validate_mempalace_server as _validate_mempalace_server,
)
from gateway.autoresearch.configuration import (
    _validate_mempalace_server_split as _validate_mempalace_server_split,
)
from gateway.autoresearch.configuration import (
    _validate_policy as _validate_policy,
)
from gateway.autoresearch.configuration import (
    load_autoresearch_policy as load_autoresearch_policy,
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
from gateway.autoresearch.engine import (
    next_action as next_action,
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
    _legacy_quantipy_bash_command as _legacy_quantipy_bash_command,
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
    _validate_quantipy_detached_run_attestation as _validate_quantipy_detached_run_attestation,
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_execution_source_against_commit as _validate_quantipy_execution_source_against_commit,  # noqa: E501
)
from gateway.autoresearch.evidence import (
    _validate_quantipy_experiment_evidence as _validate_quantipy_experiment_evidence,
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
    pin_platform_readiness as pin_platform_readiness,
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
from gateway.autoresearch.manifest_runtime import (
    LOCAL_RECEIPT_PATHS as LOCAL_RECEIPT_PATHS,
)
from gateway.autoresearch.manifest_runtime import (
    PHASE_RECEIPTS as PHASE_RECEIPTS,
)
from gateway.autoresearch.manifest_runtime import (
    QUANTIPY_RECEIPT_PATHS as QUANTIPY_RECEIPT_PATHS,
)
from gateway.autoresearch.manifest_runtime import (
    _canonical_receipt_path as _canonical_receipt_path,
)
from gateway.autoresearch.manifest_runtime import (
    _load_receipt as _load_receipt,
)
from gateway.autoresearch.manifest_runtime import (
    build_instruction_source_manifest as build_instruction_source_manifest,
)
from gateway.autoresearch.manifest_runtime import (
    build_receipt_catalog as build_receipt_catalog,
)
from gateway.autoresearch.manifest_runtime import (
    expected_instruction_manifest_sha256 as expected_instruction_manifest_sha256,
)
from gateway.autoresearch.manifest_runtime import (
    instruction_source_manifest_sha256 as instruction_source_manifest_sha256,
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
    finalize_repeat_memory_state_file as finalize_repeat_memory_state_file,
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
from gateway.autoresearch.operator_recovery import (
    _attest_sealed_interrupted_run as _attest_sealed_interrupted_run,
)
from gateway.autoresearch.operator_recovery import (
    _default_systemd_is_active as _default_systemd_is_active,
)
from gateway.autoresearch.operator_recovery import (
    _detached_pid_is_alive as _detached_pid_is_alive,
)
from gateway.autoresearch.operator_recovery import (
    _find_exact_interrupted_detached_run as _find_exact_interrupted_detached_run,
)
from gateway.autoresearch.operator_recovery import (
    _find_exact_platform_v4_detached_run as _find_exact_platform_v4_detached_run,
)
from gateway.autoresearch.operator_recovery import (
    _materialize_attested_pending_retry_failure as _materialize_attested_pending_retry_failure,
)
from gateway.autoresearch.operator_recovery import (
    _platform_runtime_recovery_identity_contexts as _platform_runtime_recovery_identity_contexts,
)
from gateway.autoresearch.operator_recovery import (
    _reattest_sealed_interrupted_run as _reattest_sealed_interrupted_run,
)
from gateway.autoresearch.operator_recovery import (
    _require_absent_interrupted_run_artifact as _require_absent_interrupted_run_artifact,
)
from gateway.autoresearch.operator_recovery import (
    _require_absent_platform_v5_identity as _require_absent_platform_v5_identity,
)
from gateway.autoresearch.operator_recovery import (
    _require_unchanged_platform_runtime_recovery_state as _require_unchanged_platform_runtime_recovery_state,  # noqa: E501
)
from gateway.autoresearch.operator_recovery import (
    _SealedInterruptedRunAttestation as _SealedInterruptedRunAttestation,
)
from gateway.autoresearch.operator_recovery import (
    recover_interrupted_verification_state_file as recover_interrupted_verification_state_file,
)
from gateway.autoresearch.operator_recovery import (
    recover_platform_runtime_state_file as recover_platform_runtime_state_file,
)
from gateway.autoresearch.operator_recovery import (
    retry_external_verification as retry_external_verification,
)
from gateway.autoresearch.operator_recovery import (
    retry_external_verification_state_file as retry_external_verification_state_file,
)
from gateway.autoresearch.persistence import (
    _ArtifactAdvancePublicationGuard as _ArtifactAdvancePublicationGuard,
)
from gateway.autoresearch.persistence import (
    _atomic_save_state_file as _atomic_save_state_file,
)
from gateway.autoresearch.persistence import (
    _canonical_state_paths as _canonical_state_paths,
)
from gateway.autoresearch.persistence import (
    _exclusive_state_lock as _exclusive_state_lock,
)
from gateway.autoresearch.persistence import (
    _exclusive_state_locks as _exclusive_state_locks,
)
from gateway.autoresearch.persistence import (
    _load_state_raw as _load_state_raw,
)
from gateway.autoresearch.persistence import (
    _move_stage_inbox_entry_no_replace as _move_stage_inbox_entry_no_replace,
)
from gateway.autoresearch.persistence import (
    _open_stage_inbox_child_directory as _open_stage_inbox_child_directory,
)
from gateway.autoresearch.persistence import (
    _open_state_lock_file as _open_state_lock_file,
)
from gateway.autoresearch.persistence import (
    _prepare_lock_namespace as _prepare_lock_namespace,
)
from gateway.autoresearch.persistence import (
    _revalidate_artifact_advance_for_atomic_publication as _revalidate_artifact_advance_for_atomic_publication,  # noqa: E501
)
from gateway.autoresearch.persistence import (
    _state_lock_path as _state_lock_path,
)
from gateway.autoresearch.persistence import (
    _validate_stage_inbox_directory_fd as _validate_stage_inbox_directory_fd,
)
from gateway.autoresearch.persistence import (
    advance_artifact_state_file as advance_artifact_state_file,
)
from gateway.autoresearch.persistence import (
    advance_infrastructure_verification_failure as advance_infrastructure_verification_failure,
)
from gateway.autoresearch.persistence import (
    consume_stage_submission_inbox as consume_stage_submission_inbox,
)
from gateway.autoresearch.persistence import (
    initialize_state as initialize_state,
)
from gateway.autoresearch.persistence import (
    load_artifact_file as load_artifact_file,
)
from gateway.autoresearch.persistence import (
    load_state_file as load_state_file,
)
from gateway.autoresearch.persistence import (
    persist_derived_state as persist_derived_state,
)
from gateway.autoresearch.persistence import (
    persist_next_iteration_state as persist_next_iteration_state,
)
from gateway.autoresearch.persistence import (
    provision_quantipy_experiment_runs_root as provision_quantipy_experiment_runs_root,
)
from gateway.autoresearch.persistence import (
    save_state_file as save_state_file,
)
from gateway.autoresearch.persistence import (
    submit_stage_artifact_file as submit_stage_artifact_file,
)
from gateway.autoresearch.persistence import (
    validate_authoritative_state_reference as validate_authoritative_state_reference,
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
    _validate_external_verification_retry_receipt as _validate_external_verification_retry_receipt,
)
from gateway.autoresearch.recovery_receipts import (
    _validate_interrupted_verification_history as _validate_interrupted_verification_history,
)
from gateway.autoresearch.recovery_receipts import (
    _validate_platform_runtime_recovery_receipt as _validate_platform_runtime_recovery_receipt,
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
    _clear_consumed_platform_runtime_receipts as _clear_consumed_platform_runtime_receipts,
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
    _require_autoresearch_worktree_root as _require_autoresearch_worktree_root,
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
    _validate_compute_fit_environment as _validate_compute_fit_environment,
)
from gateway.autoresearch.transitions import (
    _validate_consensus_history_universe_plans as _validate_consensus_history_universe_plans,
)
from gateway.autoresearch.transitions import (
    _validate_debate_result as _validate_debate_result,
)
from gateway.autoresearch.transitions import (
    _validate_final_decision_artifact as _validate_final_decision_artifact,
)
from gateway.autoresearch.transitions import (
    _validate_final_decision_memory_requirement as _validate_final_decision_memory_requirement,
)
from gateway.autoresearch.transitions import (
    _validate_fix_workspace as _validate_fix_workspace,
)
from gateway.autoresearch.transitions import (
    _validate_implementation_workspace as _validate_implementation_workspace,
)
from gateway.autoresearch.transitions import (
    _validate_no_consensus_completion as _validate_no_consensus_completion,
)
from gateway.autoresearch.transitions import (
    _validate_operator_precondition_infra_blocked_suspension as _validate_operator_precondition_infra_blocked_suspension,  # noqa: E501
)
from gateway.autoresearch.transitions import (
    _validate_persisted_autoresearch_workspace_path as _validate_persisted_autoresearch_workspace_path,  # noqa: E501
)
from gateway.autoresearch.transitions import (
    _validate_persisted_state_matches as _validate_persisted_state_matches,
)
from gateway.autoresearch.transitions import (
    _validate_price_scope_fix_result_commands as _validate_price_scope_fix_result_commands,
)
from gateway.autoresearch.transitions import (
    _validate_review_result as _validate_review_result,
)
from gateway.autoresearch.transitions import (
    _validate_state as _validate_state,
)
from gateway.autoresearch.transitions import (
    advance_state as advance_state,
)
from gateway.autoresearch.transitions import (
    build_authoritative_state_reference as build_authoritative_state_reference,
)
from gateway.autoresearch.transitions import (
    validate_artifact_workspace as validate_artifact_workspace,
)
from gateway.autoresearch.transitions import (
    validate_state as validate_state,
)
from gateway.autoresearch.workspace import (
    _common_git_base as _common_git_base,
)
from gateway.autoresearch.workspace import (
    _require_ancestor as _require_ancestor,
)
from gateway.autoresearch.workspace import (
    migrate_legacy_autoresearch_workspace_state_file as migrate_legacy_autoresearch_workspace_state_file,  # noqa: E501
)
from gateway.autoresearch.workspace import (
    state_has_legacy_autoresearch_workspace as state_has_legacy_autoresearch_workspace,
)
from gateway.autoresearch.workspace import (
    validate_target_worktree_clean as validate_target_worktree_clean,
)
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
    PlatformReadinessManifest,  # noqa: F401
    ReadinessIdentity,  # noqa: F401
    ResearchPanelProbeReceipt,  # noqa: F401
    load_xnys_calendar_evidence,  # noqa: F401
)
from gateway.autoresearch_systemd import (
    SystemdUnitStateError,  # noqa: F401
    systemd_unit_is_active,  # noqa: F401
)
from gateway.mempalace_finalizer import (
    FINAL_MEMORY_SOURCE_FILE,  # noqa: F401
    FinalMemoryWriter,  # noqa: F401
    FinalMemoryWriteRequest,  # noqa: F401
    MempalaceFinalizationError,  # noqa: F401
    SubprocessFinalMemoryWriter,  # noqa: F401
    finalization_journal_path,  # noqa: F401
)
