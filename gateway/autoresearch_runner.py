"""Deterministic control-plane for the Quantipy autoresearch loop.

This module owns the fixed phase graph, stage-agent policy validation,
skill/source receipts, artifact validation, and next-action selection.
"""

from __future__ import annotations

import fcntl  # noqa: F401
import hashlib
import json  # noqa: F401
import math  # noqa: F401
import os
import platform  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import sqlite3  # noqa: F401
import stat
import subprocess
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
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast  # noqa: F401
from urllib.parse import unquote, urlencode, urlparse  # noqa: F401

from gateway.autoresearch import constants as _constants
from gateway.autoresearch import manifest_runtime as _manifest_runtime_module
from gateway.autoresearch import persistence as _persistence_module
from gateway.autoresearch import transitions as _transitions_module
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
    save_state_file as save_state_file,
)
from gateway.autoresearch.persistence import (
    submit_stage_artifact_file as submit_stage_artifact_file,
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
    state = _persistence_module.load_state_file(state_path)
    expected = _transitions_module.build_authoritative_state_reference(state, state_path=state_path)
    if expected != reference:
        raise AutoresearchValidationError(
            "authoritative state reference does not match the current state file"
        )
    return state


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
        if _path_under_root(path, _constants.LEGACY_AUTORESEARCH_WORKTREE_ROOT):
            return True
    return False


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
        _transitions_module._validate_state(state, policy, validation_context)
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
    instruction_source_manifest = _manifest_runtime_module.build_instruction_source_manifest(
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


def finalize_repeat_memory_state_file(
    state_path: Path,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
    writer: FinalMemoryWriter | None = None,
) -> AutoresearchState:
    """Finalize and atomically mark the current repeat state under its state lock."""
    resolved_state_path = state_path.expanduser().resolve(strict=False)
    with _persistence_module._exclusive_state_lock(resolved_state_path):
        state = _persistence_module.load_state_file(resolved_state_path)
        _transitions_module._validate_state(state, policy, validation_context)
        finalized = finalize_repeat_memory(state, writer=writer)
        _transitions_module._validate_state(finalized, policy, validation_context)
        _persistence_module._atomic_save_state_file(resolved_state_path, finalized)
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
    with _persistence_module._exclusive_state_lock(resolved_state_path):
        state = _persistence_module.load_state_file(resolved_state_path)
        if not state_has_legacy_autoresearch_workspace(state):
            return state
        implementation = state.implementation_result
        if implementation is None:
            raise AutoresearchValidationError(
                "legacy workspace migration requires implementation_result"
            )
        old_root = _constants.LEGACY_AUTORESEARCH_WORKTREE_ROOT.resolve(strict=False)
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
        new_root = _constants.DEFAULT_AUTORESEARCH_WORKTREE_ROOT.resolve(strict=False)
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
        _transitions_module.validate_artifact_workspace(
            replace(migrated, implementation_result=None, fix_history=()),
            migrated_implementation,
        )
        for fix in migrated.fix_history:
            _transitions_module.validate_artifact_workspace(migrated, fix)
        _transitions_module._validate_state(migrated, policy, validation_context)
        _persistence_module._atomic_save_state_file(resolved_state_path, migrated)
        return migrated


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
    with _persistence_module._exclusive_state_locks((resolved_path,)):
        raw = _persistence_module._load_state_raw(resolved_path)
        schema_version = _require_int(raw, "schema_version")
        if schema_version != AUTORESEARCH_STATE_SCHEMA_VERSION:
            raise AutoresearchValidationError(
                "external verification retry accepts only the compatible schema-v4 state"
            )
        state = AutoresearchState.from_dict(raw)
        _transitions_module._validate_state(state, policy, validation_context)
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
        _transitions_module._validate_state(retried, policy, validation_context)
        _persistence_module._atomic_save_state_file(resolved_path, retried)
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
    if _persistence_module.load_state_file(state_path) != expected:
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
    with _persistence_module._exclusive_state_locks((authoritative_path, publication_path)):
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
        state = _persistence_module.load_state_file(authoritative_path)
        historical_validation_context, current_readiness_identity = (
            _platform_runtime_recovery_identity_contexts(state, validation_context)
        )
        _transitions_module._validate_state(state, policy, historical_validation_context)
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
        state_reference_sha256 = _transitions_module.build_authoritative_state_reference(
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
        _transitions_module._validate_state(recovered, policy, validation_context)
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
        _persistence_module._atomic_save_state_file(publication_path, recovered)
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
    with _persistence_module._exclusive_state_locks((resolved_path,)):
        state = _persistence_module.load_state_file(resolved_path)
        _transitions_module._validate_state(state, policy, validation_context)
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
        expected_state_reference = _transitions_module.build_authoritative_state_reference(
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
        _transitions_module._validate_state(recovered, policy, validation_context)
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
        _persistence_module._atomic_save_state_file(resolved_path, recovered)
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
    _transitions_module._validate_state(intermediate, policy, validation_context)
    return intermediate


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
    with _persistence_module._exclusive_state_locks((resolved_path,)):
        state = _persistence_module.load_state_file(resolved_path)
        current_reference = _transitions_module.build_authoritative_state_reference(
            state,
            state_path=resolved_path,
        ).sha256()
        if current_reference != state_reference_sha256:
            raise AutoresearchValidationError(
                "infrastructure verification failure state reference is stale"
            )
        current_instruction_manifest = (
            _manifest_runtime_module.expected_instruction_manifest_sha256(
                state,
                policy,
                receipts,
                state_path=resolved_path,
            )
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
        advanced = _transitions_module.advance_state(
            state,
            artifact,
            policy,
            validation_context=validation_context,
            state_path=resolved_path,
        )
        _persistence_module._atomic_save_state_file(resolved_path, advanced)
        return advanced
