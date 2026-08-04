"""Autoresearch constants."""

from __future__ import annotations

import os
import re
from pathlib import Path

from gateway.autoresearch_panel_receipts import (
    PANEL_RECEIPT_MAX_BYTES as PANEL_RECEIPT_MAX_BYTES,
)
from gateway.autoresearch_panel_receipts import (
    RUN_ENVELOPE_MAX_BYTES as RUN_ENVELOPE_MAX_BYTES,
)

QUANTIPY_PANEL_RECEIPT_MAX_BYTES = PANEL_RECEIPT_MAX_BYTES
QUANTIPY_RUN_ENVELOPE_MAX_BYTES = RUN_ENVELOPE_MAX_BYTES
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

AUTORESEARCH_STATE_SCHEMA_VERSION = 5
HYPOTHESIS_FINGERPRINT_VERSION = "g2-openclaw-autoresearch-hypothesis-fingerprint-v1"
HYPOTHESIS_FINGERPRINT_DIGEST_DOMAIN = "g2-openclaw.autoresearch.hypothesis-fingerprint"
HYPOTHESIS_REGISTRY_REASON_MAX_CHARS = 160
MAX_HYPOTHESIS_REGISTRY_ENTRIES = 512
MAX_CAMPAIGN_REVIEW_RECORDS = 32
NEGATIVE_RESULTS_LEDGER_MAX_ENTRIES = 12
NEGATIVE_RESULTS_LEDGER_MAX_BYTES = 2048
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

OPERATOR_INFRASTRUCTURE_SUSPENSION_METRIC_NAME = "operator-infrastructure-suspension"
OPERATOR_INFRASTRUCTURE_SUSPENSION_RATIONALE = (
    "Operator suspended the active iteration for infrastructure repair."
)
OPERATOR_INFRASTRUCTURE_SUSPENSION_LOG_SUMMARY = (
    "Active iteration suspended for operator infrastructure repair."
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

PM_NATIVE_CODEX_DELEGATION_DENY_TOOL_IDS = (
    "sessions_spawn",
    "sessions_yield",
    "agents_list",
    "sessions_list",
    "sessions_history",
)

LEGACY_AUTORESEARCH_WORKTREE_ROOT = Path("/home/dev/.openclaw/autoresearch/worktrees")
DEFAULT_ALLOWED_TARGET_STATUS_LINES = ("?? docs/quantipy_experiment_mempalace_preload.md",)
