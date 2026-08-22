"""Quantipy execution contracts and immutable run-evidence validators."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode, urlsplit

import gateway.autoresearch.evidence as evidence_module
from gateway.autoresearch import constants
from gateway.autoresearch.artifacts import (
    ImplementationResultArtifact as ImplementationResultArtifact,
)
from gateway.autoresearch.artifacts import (
    QuantipyExecutionInterruptedEvidence as QuantipyExecutionInterruptedEvidence,
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
    VerificationResultArtifact as VerificationResultArtifact,
)
from gateway.autoresearch.constants import (
    DEFAULT_QUANTIPY_ROOT as DEFAULT_QUANTIPY_ROOT,
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
    QUANTIPY_SENTIMENT_BUNDLE_MAX_BYTES as QUANTIPY_SENTIMENT_BUNDLE_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    QUANTIPY_SENTIMENT_RECEIPT_MAX_BYTES as QUANTIPY_SENTIMENT_RECEIPT_MAX_BYTES,
)
from gateway.autoresearch.enums import (
    Phase as Phase,
)
from gateway.autoresearch.enums import (
    ResearchMode as ResearchMode,
)
from gateway.autoresearch.enums import (
    VerificationStatus as VerificationStatus,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    _canonical_json_digest as _canonical_json_digest,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _parse_utc_request_datetime as _parse_utc_request_datetime,
)
from gateway.autoresearch.fields import (
    _require_exact_keys as _require_exact_keys,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
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
from gateway.autoresearch.gitops import (
    _require_strict_canonical_workspace_path as _require_strict_canonical_workspace_path,
)
from gateway.autoresearch.gitops import (
    _resolve_git_commit as _resolve_git_commit,
)
from gateway.autoresearch.gitops import (
    _run_git as _run_git,
)
from gateway.autoresearch.secure_io import (
    _parse_json_snapshot as _parse_json_snapshot,
)
from gateway.autoresearch.secure_io import (
    _require_canonical_absolute_path as _require_canonical_absolute_path,
)
from gateway.autoresearch.secure_io import (
    _require_private_directory as _require_private_directory,
)
from gateway.autoresearch.secure_io import (
    _require_sealed_quantipy_panel_file as _require_sealed_quantipy_panel_file,
)
from gateway.autoresearch.secure_io import (
    _secure_open_snapshot as _secure_open_snapshot,
)
from gateway.autoresearch.secure_io import (
    _validate_panel_request as _validate_panel_request,
)
from gateway.autoresearch.secure_io import (
    _validate_quantipy_relative_path as _validate_quantipy_relative_path,
)
from gateway.autoresearch_panel_receipts import (
    PanelReceiptValidationError as PanelReceiptValidationError,
)
from gateway.autoresearch_panel_receipts import (
    validate_research_panel_receipt as validate_research_panel_receipt,
)

if TYPE_CHECKING:
    from gateway.autoresearch.secure_io import (
        _SecureFileSnapshot as _SecureFileSnapshot,
    )
    from gateway.autoresearch.state import AutoresearchState as AutoresearchState
    from gateway.autoresearch.state import (
        AutoresearchValidationContext as AutoresearchValidationContext,
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


def _target_repo_root_for_state(state: AutoresearchState) -> Path:
    target_repo = (
        Path(state.setup.target_repo)
        if state.setup is not None
        else constants.DEFAULT_QUANTIPY_ROOT
    )
    return target_repo.expanduser().resolve(strict=False)


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


def _validate_http_base_url(value: object, *, label: str) -> str:
    url = _strict_json_string(value, label=label, minimum=1, maximum=2048)
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url
    ):
        raise AutoresearchValidationError(f"{label} must be an HTTP(S) base URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise AutoresearchValidationError(f"{label} must be an HTTP(S) base URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
    ):
        raise AutoresearchValidationError(
            f"{label} must be an HTTP(S) base URL without credentials, query, or fragment"
        )
    return url.rstrip("/")


def _canonical_sentiment_datetime(value: object, *, label: str) -> tuple[str, datetime]:
    raw = _strict_json_string(value, label=label, minimum=1)
    parsed = _strict_json_datetime(raw, label=label, utc_only=True)
    canonical = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if raw != canonical:
        raise AutoresearchValidationError(f"{label} must use canonical UTC timestamp spelling")
    return canonical, parsed.astimezone(UTC)


def _validate_sentiment_string_list(
    value: object,
    *,
    label: str,
    lowercase: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise AutoresearchValidationError(f"{label} must be a JSON array of strings")
    items: list[str] = []
    for item in value:
        text = _strict_json_string(item, label=label)
        if lowercase and text != text.lower():
            raise AutoresearchValidationError(f"{label} must contain lower-case strings")
        items.append(text)
    if items != sorted(items) or len(items) != len(set(items)):
        raise AutoresearchValidationError(f"{label} must be sorted and unique")
    return items


_SENTIMENT_ATTENTION_KEYS = (
    "schema_version",
    "pyarrow_version",
    "pandas_version",
    "extractor_version",
    "generated_at",
    "date_start",
    "date_end",
    "subreddits",
    "universe_size",
    "universe_sha256",
    "blocklist_sha256",
    "universe_market",
    "universe_locale",
    "source_post_count",
    "source_max_post_id",
    "hourly_row_count",
    "daily_row_count",
    "hourly_parquet_sha256",
    "daily_parquet_sha256",
)
_SENTIMENT_TONE_KEYS = (
    "schema_version",
    "pyarrow_version",
    "pandas_version",
    "generated_at",
    "date_start",
    "date_end",
    "subreddits",
    "judge_selectors",
    "subreddit_row_count",
    "fused_row_count",
    "subreddit_parquet_sha256",
    "fused_parquet_sha256",
)


def _validate_sentiment_attention(
    value: object,
    *,
    label: str,
) -> tuple[dict[str, object], datetime, date, date]:
    data = _strict_json_keys(value, label=label, expected=_SENTIMENT_ATTENTION_KEYS)
    if data["schema_version"] != "reddit-attention-panel-v1":
        raise AutoresearchValidationError(f"{label}.schema_version is invalid")
    generated_at, generated_at_dt = _canonical_sentiment_datetime(
        data["generated_at"], label=f"{label}.generated_at"
    )
    date_start = _strict_json_date(data["date_start"], label=f"{label}.date_start")
    date_end = _strict_json_date(data["date_end"], label=f"{label}.date_end")
    if date_start > date_end:
        raise AutoresearchValidationError(f"{label} date span is not ordered")
    normalized: dict[str, object] = {
        "schema_version": "reddit-attention-panel-v1",
        "pyarrow_version": _strict_json_string(
            data["pyarrow_version"], label=f"{label}.pyarrow_version"
        ),
        "pandas_version": _strict_json_string(
            data["pandas_version"], label=f"{label}.pandas_version"
        ),
        "extractor_version": _strict_json_string(
            data["extractor_version"], label=f"{label}.extractor_version"
        ),
        "generated_at": generated_at,
        "date_start": date_start.isoformat(),
        "date_end": date_end.isoformat(),
        "subreddits": _validate_sentiment_string_list(
            data["subreddits"], label=f"{label}.subreddits", lowercase=True
        ),
        "universe_size": _strict_json_int(data["universe_size"], label=f"{label}.universe_size"),
        "universe_sha256": _strict_json_sha256(
            data["universe_sha256"], label=f"{label}.universe_sha256"
        ),
        "blocklist_sha256": _strict_json_sha256(
            data["blocklist_sha256"], label=f"{label}.blocklist_sha256"
        ),
        "universe_market": _strict_json_string(
            data["universe_market"], label=f"{label}.universe_market"
        ),
        "universe_locale": _strict_json_string(
            data["universe_locale"], label=f"{label}.universe_locale"
        ),
        "source_post_count": _strict_json_int(
            data["source_post_count"], label=f"{label}.source_post_count"
        ),
        "source_max_post_id": _strict_json_int(
            data["source_max_post_id"], label=f"{label}.source_max_post_id"
        ),
        "hourly_row_count": _strict_json_int(
            data["hourly_row_count"], label=f"{label}.hourly_row_count"
        ),
        "daily_row_count": _strict_json_int(
            data["daily_row_count"], label=f"{label}.daily_row_count"
        ),
        "hourly_parquet_sha256": _strict_json_sha256(
            data["hourly_parquet_sha256"], label=f"{label}.hourly_parquet_sha256"
        ),
        "daily_parquet_sha256": _strict_json_sha256(
            data["daily_parquet_sha256"], label=f"{label}.daily_parquet_sha256"
        ),
    }
    return normalized, generated_at_dt, date_start, date_end


def _validate_sentiment_tone(
    value: object,
    *,
    label: str,
) -> tuple[dict[str, object], datetime, date, date]:
    data = _strict_json_keys(value, label=label, expected=_SENTIMENT_TONE_KEYS)
    if data["schema_version"] != "reddit-tone-panel-v1":
        raise AutoresearchValidationError(f"{label}.schema_version is invalid")
    generated_at, generated_at_dt = _canonical_sentiment_datetime(
        data["generated_at"], label=f"{label}.generated_at"
    )
    date_start = _strict_json_date(data["date_start"], label=f"{label}.date_start")
    date_end = _strict_json_date(data["date_end"], label=f"{label}.date_end")
    if date_start > date_end:
        raise AutoresearchValidationError(f"{label} date span is not ordered")
    normalized = {
        "schema_version": "reddit-tone-panel-v1",
        "pyarrow_version": _strict_json_string(
            data["pyarrow_version"], label=f"{label}.pyarrow_version"
        ),
        "pandas_version": _strict_json_string(
            data["pandas_version"], label=f"{label}.pandas_version"
        ),
        "generated_at": generated_at,
        "date_start": date_start.isoformat(),
        "date_end": date_end.isoformat(),
        "subreddits": _validate_sentiment_string_list(
            data["subreddits"], label=f"{label}.subreddits", lowercase=True
        ),
        "judge_selectors": _validate_sentiment_string_list(
            data["judge_selectors"], label=f"{label}.judge_selectors"
        ),
        "subreddit_row_count": _strict_json_int(
            data["subreddit_row_count"], label=f"{label}.subreddit_row_count"
        ),
        "fused_row_count": _strict_json_int(
            data["fused_row_count"], label=f"{label}.fused_row_count"
        ),
        "subreddit_parquet_sha256": _strict_json_sha256(
            data["subreddit_parquet_sha256"], label=f"{label}.subreddit_parquet_sha256"
        ),
        "fused_parquet_sha256": _strict_json_sha256(
            data["fused_parquet_sha256"], label=f"{label}.fused_parquet_sha256"
        ),
    }
    return normalized, generated_at_dt, date_start, date_end


def _validate_sentiment_receipt(value: object, *, label: str) -> dict[str, object]:
    data = _strict_json_keys(
        value,
        label=label,
        expected=("contract_version", "panels_sha256", "attention", "tone", "packaged_at"),
    )
    if data["contract_version"] != "research-sentiment-panels-v1":
        raise AutoresearchValidationError(f"{label}.contract_version is invalid")
    attention, attention_generated_at, attention_start, attention_end = (
        _validate_sentiment_attention(data["attention"], label=f"{label}.attention")
    )
    tone, tone_generated_at, tone_start, tone_end = _validate_sentiment_tone(
        data["tone"], label=f"{label}.tone"
    )
    if (attention_start, attention_end) != (tone_start, tone_end):
        raise AutoresearchValidationError(f"{label} attention and tone date spans differ")
    packaged_at, packaged_at_dt = _canonical_sentiment_datetime(
        data["packaged_at"], label=f"{label}.packaged_at"
    )
    if packaged_at_dt < attention_generated_at or packaged_at_dt < tone_generated_at:
        raise AutoresearchValidationError(f"{label}.packaged_at precedes panel generation")
    normalized: dict[str, object] = {
        "contract_version": "research-sentiment-panels-v1",
        "panels_sha256": _strict_json_sha256(data["panels_sha256"], label=f"{label}.panels_sha256"),
        "attention": attention,
        "tone": tone,
        "packaged_at": packaged_at,
    }
    canonical_size = len(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if canonical_size > QUANTIPY_SENTIMENT_RECEIPT_MAX_BYTES:
        raise AutoresearchValidationError(f"{label} exceeds its size limit")
    return normalized


def _validate_quantipy_manifest_sentiment(value: object, *, label: str) -> dict[str, str]:
    data = _strict_json_keys(value, label=label, expected=("api_url", "receipt_sha256"))
    return {
        "api_url": _validate_http_base_url(data["api_url"], label=f"{label}.api_url"),
        "receipt_sha256": _strict_json_sha256(
            data["receipt_sha256"], label=f"{label}.receipt_sha256"
        ),
    }


def _validate_quantipy_sentiment_manifest_binding(
    value: object,
    receipt: Mapping[str, object],
    *,
    label: str,
) -> None:
    data = _ensure_mapping(value, label=label)
    expected_receipt_sha256 = _strict_json_sha256(
        data["receipt_sha256"], label=f"{label}.receipt_sha256"
    )
    if _canonical_json_digest(receipt) != expected_receipt_sha256:
        raise AutoresearchValidationError(
            f"{label}.receipt_sha256 does not match the persisted sentiment receipt"
        )


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
        (
            "schema_version",
            "experiment_id",
            "package_path",
            "notebook_path",
            "stage_files",
            "panel",
            "sentiment",
        )
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
    if "sentiment" in manifest and manifest["sentiment"] is not None:
        manifest["sentiment"] = _validate_quantipy_manifest_sentiment(
            manifest["sentiment"], label="manifest sentiment"
        )
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


def _quantipy_manifest_sha256_aliases(manifest: Mapping[str, object]) -> tuple[str, ...]:
    """Digests under which the pinned runtime may record this exact manifest.

    The runtime digests the parsed ExperimentManifest's canonical model dump,
    not the file bytes. Quantipy d3987cd added the optional `sentiment` field,
    so the dump of every pre-sentiment manifest gains `"sentiment": null` and
    its digest diverges from the file's. Accept exactly that one evolution
    alias for manifests that do not declare the key; any other content change
    still fails the binding.
    """
    aliases = [_canonical_quantipy_manifest_sha256(manifest)]
    if "sentiment" not in manifest:
        aliased: dict[str, object] = dict(manifest)
        aliased["sentiment"] = None
        aliases.append(_canonical_quantipy_manifest_sha256(aliased))
    return tuple(aliases)


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


def _run_failure_from_mapping(raw: object) -> QuantipyExperimentFailureEvidence | None:
    if raw is None:
        return None
    return QuantipyExperimentFailureEvidence.from_dict(raw)


def _existing_reservation_matches(
    root_fd: int,
    run_id: str,
    payload: bytes,
) -> bool:
    """True only when the run directory is exactly a prior reservation of `payload`.

    Submission and supervisor consumption both validate the same envelope, so the
    reservation must be idempotent for byte-identical evidence; anything else in
    the directory keeps meaning a run started.
    """
    try:
        run_fd = os.open(
            run_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
    except OSError:
        return False
    try:
        if os.fstat(run_fd).st_mode & 0o777 != 0o700:
            return False
        if os.listdir(run_fd) != [QUANTIPY_EXECUTION_NOT_STARTED_TOMBSTONE]:
            return False
        try:
            marker_fd = os.open(
                QUANTIPY_EXECUTION_NOT_STARTED_TOMBSTONE,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=run_fd,
            )
        except OSError:
            return False
        try:
            marker_stat = os.fstat(marker_fd)
            if not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_nlink != 1:
                return False
            if marker_stat.st_mode & 0o777 != 0o600:
                return False
            if marker_stat.st_size != len(payload):
                return False
            return os.read(marker_fd, len(payload) + 1) == payload
        finally:
            os.close(marker_fd)
    finally:
        os.close(run_fd)


def _reserve_quantipy_execution_not_started(
    evidence: QuantipyExecutionNotStartedEvidence,
    *,
    runs_root: Path,
) -> None:
    """Reserve the deterministic run directory so a concurrent run cannot start later."""
    _require_private_directory(runs_root, label="trusted Quantipy runs root")
    payload = json.dumps(
        {
            "schema_version": "g2-quantipy-execution-not-started-v1",
            **evidence.to_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    root_fd = os.open(
        runs_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        try:
            os.mkdir(evidence.expected_run_id, mode=0o700, dir_fd=root_fd)
        except FileExistsError as exc:
            if _existing_reservation_matches(root_fd, evidence.expected_run_id, payload):
                return
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


def _validate_quantipy_execution_interrupted(
    state: AutoresearchState,
    implementation: ImplementationResultArtifact,
    evidence: QuantipyExecutionInterruptedEvidence,
    *,
    expected_run_id: str,
    expected_run_path: Path,
    state_path: Path | None,
    expected_instruction_manifest_sha256: str | None = None,
    runs_root: Path | None = None,
) -> None:
    """Bind interrupted verification recovery evidence to sealed detached run files."""
    import gateway.autoresearch_runs as detached_runs

    if (
        evidence.manifest_path != implementation.experiment_manifest_path
        or evidence.manifest_sha256 != implementation.experiment_manifest_sha256
        or evidence.expected_run_id != expected_run_id
        or evidence.expected_run_json_path != str(expected_run_path)
    ):
        raise AutoresearchValidationError(
            "execution-interrupted evidence does not bind the implementation and expected run"
        )
    if expected_run_path.parent.name != expected_run_id:
        raise AutoresearchValidationError(
            "execution-interrupted expected run path does not bind its run ID"
        )
    try:
        expected_run_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            "execution-interrupted expected run.json cannot be inspected"
        ) from exc
    else:
        raise AutoresearchValidationError("execution-interrupted expected run.json must be absent")
    configured_runs_root = (
        detached_runs.DEFAULT_AUTORESEARCH_RUNS_ROOT if runs_root is None else runs_root
    )
    trusted_runs_root = _require_canonical_absolute_path(
        configured_runs_root,
        label="trusted detached runs root",
    )
    run_directory = _require_canonical_absolute_path(
        evidence.detached_run_directory,
        label="quantipy_execution_interrupted.detached_run_directory",
    )
    try:
        run_directory.relative_to(trusted_runs_root)
    except ValueError as exc:
        raise AutoresearchValidationError(
            "execution-interrupted detached run directory escaped the trusted runs root"
        ) from exc
    try:
        directory_metadata = run_directory.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(
            "execution-interrupted detached run directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_ISLNK(directory_metadata.st_mode)
        or directory_metadata.st_uid != os.getuid()
        or stat.S_IMODE(directory_metadata.st_mode) != 0o500
    ):
        raise AutoresearchValidationError(
            "execution-interrupted detached run directory must be a sealed private directory"
        )
    manifest_snapshot = _secure_open_snapshot(
        run_directory / "manifest.json",
        label="execution-interrupted detached manifest",
        trusted_root=trusted_runs_root,
        private=True,
        max_bytes=QUANTIPY_RUN_ENVELOPE_MAX_BYTES,
    )
    status_snapshot = _secure_open_snapshot(
        run_directory / "status.json",
        label="execution-interrupted detached status",
        trusted_root=trusted_runs_root,
        private=True,
        max_bytes=QUANTIPY_RUN_ENVELOPE_MAX_BYTES,
    )
    try:
        detached_manifest = detached_runs.RunManifest.from_dict(
            dict(
                _parse_json_snapshot(
                    manifest_snapshot,
                    label="execution-interrupted detached manifest",
                )
            )
        )
        detached_status = detached_runs.RunStatus.from_dict(
            dict(
                _parse_json_snapshot(
                    status_snapshot,
                    label="execution-interrupted detached status",
                )
            )
        )
    except ValueError as exc:
        raise AutoresearchValidationError(
            "execution-interrupted detached run manifest or status is invalid"
        ) from exc
    try:
        detached_record = detached_runs.read_run_record(
            run_dir=run_directory,
            runs_root=Path(trusted_runs_root),
        )
    except (OSError, ValueError) as exc:
        raise AutoresearchValidationError(
            "execution-interrupted detached run record is invalid or unavailable"
        ) from exc
    if detached_record.manifest != detached_manifest or detached_record.status != detached_status:
        raise AutoresearchValidationError(
            "execution-interrupted detached run record changed during validation"
        )
    if manifest_snapshot.sha256 != evidence.detached_manifest_sha256:
        raise AutoresearchValidationError(
            "execution-interrupted detached manifest digest does not match evidence"
        )
    if detached_status.manifest_sha256 != evidence.detached_manifest_sha256:
        raise AutoresearchValidationError(
            "execution-interrupted detached status manifest digest does not match evidence"
        )
    if _canonical_json_digest(detached_status.to_dict()) != evidence.detached_status_sha256:
        raise AutoresearchValidationError(
            "execution-interrupted detached status digest does not match evidence"
        )
    contract = build_quantipy_execution_contract(
        runtime_root=_target_repo_root_for_state(state),
        manifest_path=Path(implementation.experiment_manifest_path),
        output_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        run_id=expected_run_id,
    )
    if (
        detached_manifest.iteration != state.iteration
        or detached_manifest.phase is not Phase.VERIFICATION
        or detached_manifest.run_directory != str(run_directory)
        or detached_manifest.working_directory != str(contract.working_directory)
        or detached_manifest.expected_artifact_path != str(expected_run_path)
        or detached_manifest.command_sha256 != detached_runs.command_sha256(contract.command)
    ):
        raise AutoresearchValidationError(
            "execution-interrupted detached manifest does not bind the expected run"
        )
    from gateway.autoresearch.transitions import (
        build_authoritative_state_reference,
    )

    expected_state_reference = build_authoritative_state_reference(
        state,
        state_path=state_path or constants.DEFAULT_AUTORESEARCH_STATE_PATH,
    ).sha256()
    if (
        expected_instruction_manifest_sha256 is not None
        and detached_manifest.instruction_manifest_sha256 != expected_instruction_manifest_sha256
    ):
        raise AutoresearchValidationError(
            "execution-interrupted detached manifest instruction digest does not match current "
            "instructions"
        )
    instruction_manifest_for_matching = (
        expected_instruction_manifest_sha256
        if expected_instruction_manifest_sha256 is not None
        else detached_manifest.instruction_manifest_sha256
    )
    if detached_manifest.state_reference_sha256 != expected_state_reference:
        raise AutoresearchValidationError(
            "execution-interrupted detached manifest state reference does not match state"
        )
    matching_records: list[detached_runs.RunRecord] = []
    matching_attempts: set[int] = set()
    try:
        trusted_root_metadata = trusted_runs_root.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(
            "execution-interrupted trusted detached runs root is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(trusted_root_metadata.st_mode)
        or stat.S_ISLNK(trusted_root_metadata.st_mode)
        or trusted_root_metadata.st_uid != os.getuid()
        or stat.S_IMODE(trusted_root_metadata.st_mode) != 0o700
    ):
        raise AutoresearchValidationError(
            "execution-interrupted trusted detached runs root must be a private directory"
        )
    for directory, child_directories, files in os.walk(trusted_runs_root, followlinks=False):
        parent = Path(directory)
        child_directories.sort()
        files.sort()
        for child_directory in child_directories:
            if (parent / child_directory).is_symlink():
                raise AutoresearchValidationError(
                    "execution-interrupted trusted detached runs root contains a symlinked "
                    "run directory"
                )
        if "manifest.json" not in files:
            continue
        status_path = parent / "status.json"
        if not status_path.is_symlink() and not status_path.exists():
            # A manifest with no status file is a prepared run that never
            # launched — historical residue, not a corrupt record.
            continue
        try:
            candidate = detached_runs.read_run_record(
                run_dir=parent,
                runs_root=trusted_runs_root,
            )
        except (OSError, ValueError) as exc:
            raise AutoresearchValidationError(
                "execution-interrupted matching detached run record is invalid"
            ) from exc
        candidate_manifest = candidate.manifest
        if (
            candidate_manifest.phase is Phase.VERIFICATION
            and candidate_manifest.iteration == state.iteration
            and candidate_manifest.state_reference_sha256 == expected_state_reference
            and candidate_manifest.instruction_manifest_sha256 == instruction_manifest_for_matching
        ):
            if candidate_manifest.attempt in matching_attempts:
                raise AutoresearchValidationError(
                    "execution-interrupted matching detached runs contain duplicate attempts"
                )
            matching_attempts.add(candidate_manifest.attempt)
            matching_records.append(candidate)
    if any(record.status.state is detached_runs.RunState.RUNNING for record in matching_records):
        raise AutoresearchValidationError(
            "execution-interrupted evidence cannot cite a run while a newer matching run is "
            "still running"
        )
    if not matching_records:
        raise AutoresearchValidationError(
            "execution-interrupted detached run is not a matching current verification run"
        )
    latest_record = max(matching_records, key=lambda record: record.manifest.attempt)
    if latest_record.run_directory != detached_record.run_directory:
        raise AutoresearchValidationError(
            "execution-interrupted evidence must cite the latest matching verification attempt"
        )
    if (
        detached_status.state is not detached_runs.RunState.FAILED
        or detached_status.exit_code != evidence.exit_code
        or detached_status.signal_number != evidence.signal_number
        or detached_status.failure_classification is None
        or detached_status.failure_classification.value != evidence.failure_classification
    ):
        raise AutoresearchValidationError(
            "execution-interrupted detached terminal status does not match evidence"
        )
    if (
        detached_status.failure_classification
        is detached_runs.RunFailureClassification.OPERATOR_STOPPED
    ):
        raise AutoresearchValidationError(
            "execution-interrupted evidence cannot accept an operator-stopped detached run"
        )
    if detached_status.finished_at is None:
        raise AutoresearchValidationError(
            "execution-interrupted detached run lacks a terminal finish timestamp"
        )
    started_at = _strict_json_datetime(
        detached_status.started_at,
        label="execution-interrupted detached status started_at",
        utc_only=True,
    )
    finished_at = _strict_json_datetime(
        detached_status.finished_at,
        label="execution-interrupted detached status finished_at",
        utc_only=True,
    )
    observed_wall_seconds = (finished_at - started_at).total_seconds()
    if (
        detached_manifest.timeout_seconds != evidence.timeout_seconds
        or observed_wall_seconds != evidence.wall_seconds_observed
    ):
        raise AutoresearchValidationError(
            "execution-interrupted timeout or observed wall time does not match detached run"
        )
    if (
        detached_status.expected_artifact_attestation_status
        is not detached_runs.ExpectedArtifactAttestationStatus.FAILED
        or detached_status.expected_artifact_attestation_error
        is not detached_runs.ExpectedArtifactAttestationError.MISSING
        or detached_status.expected_artifact_attestation is not None
    ):
        raise AutoresearchValidationError(
            "execution-interrupted detached run requires a missing artifact attestation"
        )
    if (
        detached_status.systemd_unit is None
        or constants.OPENCLAW_LONG_TASK_UNIT_RE.fullmatch(detached_status.systemd_unit) is None
    ):
        raise AutoresearchValidationError(
            "execution-interrupted detached run systemd unit is invalid"
        )
    capture = detached_status.output_capture
    if capture is None:
        raise AutoresearchValidationError("execution-interrupted detached run lacks output capture")
    streams = (
        (
            "stdout",
            capture.stdout,
            evidence.stdout_sha256,
            evidence.stdout_bytes_observed,
            evidence.stdout_truncated,
        ),
        (
            "stderr",
            capture.stderr,
            evidence.stderr_sha256,
            evidence.stderr_bytes_observed,
            evidence.stderr_truncated,
        ),
    )
    for label, stream, expected_digest, expected_bytes_observed, expected_truncated in streams:
        if not stream.eof_observed:
            raise AutoresearchValidationError(
                "execution-interrupted detached run output capture is incomplete"
            )
        capture_snapshot = _secure_open_snapshot(
            run_directory / stream.relative_path,
            label=f"execution-interrupted {label} capture",
            trusted_root=trusted_runs_root,
            private=True,
            max_bytes=detached_runs.OUTPUT_CAPTURE_MAX_BYTES,
        )
        if (
            stream.bytes_observed != expected_bytes_observed
            or stream.truncated is not expected_truncated
            or not stream.eof_observed
        ):
            raise AutoresearchValidationError(
                f"execution-interrupted {label} capture metadata does not match evidence"
            )
        if (
            capture_snapshot.sha256 != stream.sha256
            or capture_snapshot.sha256 != expected_digest
            or len(capture_snapshot.content) != stream.bytes_stored
        ):
            raise AutoresearchValidationError(
                f"execution-interrupted {label} capture digest does not match evidence"
            )


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


def _validate_quantipy_run_sentiment(value: object, *, label: str) -> dict[str, object]:
    data = _strict_json_keys(
        value,
        label=label,
        expected=("bundle_path", "bundle_sha256", "receipt_path", "receipt_sha256", "receipt"),
    )
    if data["bundle_path"] != "sentiment/panels.zip":
        raise AutoresearchValidationError(f"{label}.bundle_path is invalid")
    if data["receipt_path"] != "sentiment/receipt.json":
        raise AutoresearchValidationError(f"{label}.receipt_path is invalid")
    receipt = _validate_sentiment_receipt(data["receipt"], label=f"{label}.receipt")
    bundle_sha = _strict_json_sha256(data["bundle_sha256"], label=f"{label}.bundle_sha256")
    receipt_sha = _strict_json_sha256(data["receipt_sha256"], label=f"{label}.receipt_sha256")
    if bundle_sha != receipt["panels_sha256"]:
        raise AutoresearchValidationError(
            f"{label}.bundle_sha256 does not match receipt.panels_sha256"
        )
    if receipt_sha != _canonical_json_digest(receipt):
        raise AutoresearchValidationError(
            f"{label}.receipt_sha256 does not match the canonical nested receipt digest"
        )
    return {
        "bundle_path": "sentiment/panels.zip",
        "bundle_sha256": bundle_sha,
        "receipt_path": "sentiment/receipt.json",
        "receipt_sha256": receipt_sha,
        "receipt": receipt,
    }


def _reject_unbound_quantipy_sentiment(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AutoresearchValidationError(
            "unbound Quantipy sentiment artifact cannot be inspected"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise AutoresearchValidationError(
            "unbound Quantipy sentiment artifact must not be a symlink"
        )
    raise AutoresearchValidationError(
        "Quantipy sentiment artifact is present without bound run evidence"
    )


def _require_sealed_quantipy_directory(path: Path, *, label: str) -> None:
    """Require an owned mode-0500 directory for a sealed Quantipy artifact set."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(f"{label} does not exist") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o500
    ):
        raise AutoresearchValidationError(
            f"{label} must be an owned mode-0500 non-symlink directory"
        )


def _validate_quantipy_run_envelope(
    snapshot: _SecureFileSnapshot,
    *,
    mode: ResearchMode | None = None,
    declared_bug_signals: tuple[str, ...] = (),
) -> dict[str, object]:
    parsed_run = _parse_json_snapshot(snapshot, label="Quantipy run.json")
    # Envelopes sealed before quantipy added runtime-derived provenance lack the
    # key entirely; current envelopes always carry it (null when no panel).
    _has_derived_provenance = isinstance(parsed_run, dict) and "derived_provenance" in parsed_run
    _has_sentiment_requested = isinstance(parsed_run, dict) and "sentiment_requested" in parsed_run
    _has_sentiment = isinstance(parsed_run, dict) and "sentiment" in parsed_run
    if _has_sentiment_requested is not _has_sentiment:
        raise AutoresearchValidationError(
            "Quantipy run.json sentiment_requested and sentiment must be present together"
        )
    run = _strict_json_keys(
        parsed_run,
        label="Quantipy run.json",
        expected=(
            "run_id",
            "identity",
            "manifest_sha256",
            "source",
            "success",
            "panel_requested",
            "panel",
            *(("sentiment_requested", "sentiment") if _has_sentiment_requested else ()),
            *(("derived_provenance",) if _has_derived_provenance else ()),
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
    sentiment_requested = (
        _strict_json_bool(run["sentiment_requested"], label="Quantipy run.json sentiment_requested")
        if _has_sentiment_requested
        else False
    )
    sentiment = (
        _validate_quantipy_run_sentiment(run["sentiment"], label="Quantipy run.json sentiment")
        if _has_sentiment and run["sentiment"] is not None
        else None
    )
    derived_provenance_raw = run.get("derived_provenance") if _has_derived_provenance else None
    normalized_derived_provenance: dict[str, object] | None = None
    if derived_provenance_raw is not None:
        provenance = _strict_json_keys(
            derived_provenance_raw,
            label="Quantipy run.json derived_provenance",
            expected=(
                "member_union_count",
                "member_union_digest",
                "member_union_digest_algorithm",
                "experiment_start",
                "experiment_end",
                "timeframe",
                "market_hours",
                "request_sha256",
                "coverage_sha256",
                "panel_sha256",
                "hydrated_at",
                "exported_at",
            ),
        )
        if panel is None:
            raise AutoresearchValidationError(
                "Quantipy run.json derived_provenance requires bound panel evidence"
            )
        for digest_key in ("request_sha256", "coverage_sha256", "panel_sha256"):
            if provenance[digest_key] != panel[digest_key]:
                raise AutoresearchValidationError(
                    f"Quantipy run.json derived_provenance {digest_key} "
                    "does not match panel evidence"
                )
        normalized_derived_provenance = dict(provenance)
    if (
        mode is ResearchMode.ALPHA_RESEARCH
        and panel is not None
        and derived_provenance_raw is None
        and _has_derived_provenance
    ):
        raise AutoresearchValidationError(
            "ALPHA_RESEARCH runs with panel evidence must carry runtime-derived "
            "provenance; the quantipy runtime assembles it from the panel receipt"
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
            # The measured-projection guard binds only on SUCCESSFUL runs: it
            # exists to keep timeout derivations honest, and a failed run's
            # receipts are failure evidence that must remain submittable. A
            # guard that blocks reporting a defect is the anti-pattern this
            # validator family is meant to eliminate.
            # A submission that DECLARES the telemetry defect as its own bug
            # signal stays submittable: self-indicting evidence cannot be
            # silently ignored, and without this escape a successful run with
            # unmeasured telemetry and an exhausted fix budget has no truthful
            # exit path in the artifact contract.
            _telemetry_defect_declared = any(
                "calibration_fit_seconds" in signal or "feasibility_telemetry" in signal
                for signal in declared_bug_signals
            )
            if (
                stage == "feasibility"
                and success
                and not _telemetry_defect_declared
                and (mode is None or mode is ResearchMode.ALPHA_RESEARCH)
            ):
                summary = cast(str, result["summary"])
                try:
                    parsed_summary = json.loads(
                        summary,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError(f"non-finite JSON number {value}")
                        ),
                    )
                except (ValueError, json.JSONDecodeError) as exc:
                    raise AutoresearchValidationError(
                        f"{label}.result.summary feasibility summary must be a JSON object"
                    ) from exc
                if not isinstance(parsed_summary, dict):
                    raise AutoresearchValidationError(
                        f"{label}.result.summary feasibility summary must be a JSON object"
                    )
                for field_name in ("calibration_fit_seconds", "projected_model_seconds"):
                    value = parsed_summary.get(field_name)
                    try:
                        numeric_value = float(value) if isinstance(value, int | float) else 0.0
                    except OverflowError:
                        numeric_value = math.inf
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int | float)
                        or not math.isfinite(numeric_value)
                        or numeric_value <= 0
                    ):
                        raise AutoresearchValidationError(
                            f"{label}.result.summary feasibility stage must MEASURE one real "
                            f"fit at the true encoded width: {field_name} must be a number "
                            "strictly greater than 0"
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
    if not sentiment_requested and sentiment is not None:
        raise AutoresearchValidationError(
            "unrequested Quantipy runs cannot bind sentiment evidence"
        )
    requested_artifact_missing = (panel_requested and panel is None) or (
        sentiment_requested and sentiment is None
    )
    if failure is not None and failure["category"] == "panel" and not requested_artifact_missing:
        raise AutoresearchValidationError("panel failures require a missing requested artifact")
    if (
        panel_requested
        and panel is None
        and (failure is None or failure["category"] not in {"panel", "preflight", "filesystem"})
    ):
        raise AutoresearchValidationError(
            "requested Quantipy panels require evidence or a valid pre-stage failure"
        )
    if (
        sentiment_requested
        and sentiment is None
        and (failure is None or failure["category"] not in {"panel", "preflight", "filesystem"})
    ):
        raise AutoresearchValidationError(
            "requested Quantipy sentiment requires evidence or a valid pre-stage failure"
        )
    if panel is not None and failure is not None and failure["category"] == "preflight":
        raise AutoresearchValidationError("preflight failures cannot bind panel evidence")
    if sentiment is not None and failure is not None and failure["category"] == "preflight":
        raise AutoresearchValidationError("preflight failures cannot bind sentiment evidence")
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
    if (
        failure is not None
        and failure["category"] == "filesystem"
        and requested_artifact_missing
        and normalized_receipts
    ):
        raise AutoresearchValidationError(
            "filesystem failures with missing requested artifacts require zero stage receipts"
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
        **(
            {
                "sentiment_requested": sentiment_requested,
                "sentiment": sentiment,
            }
            if _has_sentiment_requested
            else {}
        ),
        **(
            {"derived_provenance": normalized_derived_provenance} if _has_derived_provenance else {}
        ),
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
                str(constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT),
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
    from gateway.autoresearch.recovery_receipts import (
        _deterministic_quantipy_run_id,
    )

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
            output_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            run_id=expected_run_id,
        )
        if expected_run_id == historical_v2_run_id
        else build_quantipy_execution_contract(
            runtime_root=target_root,
            manifest_path=Path(implementation.experiment_manifest_path),
            output_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
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
    from gateway.autoresearch import attestation

    current_runtime = attestation._attest_canonical_quantipy_runtime(
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
    state_path: Path | None = None,
    expected_instruction_manifest_sha256: str | None = None,
    runs_root: Path | None = None,
) -> None:
    evidence = artifact.quantipy_experiment_evidence
    not_started = artifact.quantipy_execution_not_started
    interrupted = artifact.quantipy_execution_interrupted
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
    from gateway.autoresearch.recovery_receipts import (
        _expected_quantipy_verification_run_id,
    )

    expected_run_id = _expected_quantipy_verification_run_id(state, canonical_commit)
    expected_run_path = (
        constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT / expected_run_id / "run.json"
    )
    _require_private_directory(
        constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        label="trusted Quantipy runs root",
    )

    if evidence is None:
        if artifact.status is VerificationStatus.PASS:
            raise AutoresearchValidationError(
                "PASS verification requires Quantipy experiment evidence"
            )
        if not_started is None and interrupted is None:
            raise AutoresearchValidationError(
                "non-PASS without runtime evidence requires execution-not-started or "
                "execution-interrupted evidence"
            )
        if not_started is not None:
            if (
                not_started.manifest_path != implementation.experiment_manifest_path
                or not_started.manifest_sha256 != implementation.experiment_manifest_sha256
                or not_started.expected_run_id != expected_run_id
                or not_started.expected_run_json_path != str(expected_run_path)
            ):
                raise AutoresearchValidationError(
                    "execution-not-started evidence does not bind the implementation and "
                    "expected run"
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
                runs_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            )
        else:
            assert interrupted is not None
            _validate_quantipy_execution_interrupted(
                state,
                implementation,
                interrupted,
                expected_run_id=expected_run_id,
                expected_run_path=expected_run_path,
                state_path=state_path,
                expected_instruction_manifest_sha256=expected_instruction_manifest_sha256,
                runs_root=runs_root,
            )
        return
    if not_started is not None or interrupted is not None:
        raise AutoresearchValidationError(
            "runtime evidence, execution-not-started evidence, and execution-interrupted "
            "evidence are mutually exclusive"
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
        trusted_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
        private=True,
        max_bytes=QUANTIPY_RUN_ENVELOPE_MAX_BYTES - 1,
    )
    _require_private_directory(run_snapshot.path.parent, label="Quantipy run directory")
    if run_snapshot.sha256 != evidence.run_json_sha256:
        raise AutoresearchValidationError(
            "quantipy_experiment_evidence.run_json_sha256 does not match run.json"
        )
    run = _validate_quantipy_run_envelope(
        run_snapshot,
        mode=state.mode,
        declared_bug_signals=tuple(artifact.bug_signals),
    )
    if run["run_id"] != evidence.run_id or run["success"] is not evidence.success:
        raise AutoresearchValidationError("Quantipy run.json identity does not match evidence")
    if run["manifest_sha256"] not in _quantipy_manifest_sha256_aliases(manifest):
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
    evidence_module._validate_quantipy_detached_run_attestation(
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
    sentiment_requested = bool(run.get("sentiment_requested", False))
    manifest_sentiment_requested = manifest.get("sentiment") is not None
    if sentiment_requested is not manifest_sentiment_requested:
        raise AutoresearchValidationError(
            "Quantipy run.json sentiment_requested does not match manifest"
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
        _require_sealed_quantipy_directory(panel_directory, label="Quantipy panel directory")
        panel_snapshot = _secure_open_snapshot(
            run_snapshot.path.parent / evidence.panel.panel_path,
            label="Quantipy panel file",
            trusted_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            private=True,
            max_bytes=1024 * 1024 * 1024,
        )
        receipt_snapshot = _secure_open_snapshot(
            run_snapshot.path.parent / evidence.panel.receipt_path,
            label="Quantipy panel receipt",
            trusted_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
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
    run_sentiment = run.get("sentiment")
    if run_sentiment is None:
        if evidence.sentiment is not None:
            raise AutoresearchValidationError(
                "Quantipy experiment sentiment evidence is not present in run.json"
            )
        _reject_unbound_quantipy_sentiment(run_snapshot.path.parent / "sentiment")
    else:
        run_sentiment_data = _ensure_mapping(run_sentiment, label="Quantipy run.json sentiment")
        if evidence.sentiment is None or evidence.sentiment.to_dict() != {
            "bundle_path": run_sentiment_data["bundle_path"],
            "bundle_sha256": run_sentiment_data["bundle_sha256"],
            "receipt_path": run_sentiment_data["receipt_path"],
            "receipt_sha256": run_sentiment_data["receipt_sha256"],
        }:
            raise AutoresearchValidationError(
                "Quantipy experiment sentiment evidence does not match run.json"
            )
        sentiment_directory = run_snapshot.path.parent / "sentiment"
        _require_sealed_quantipy_directory(
            sentiment_directory, label="Quantipy sentiment directory"
        )
        bundle_snapshot = _secure_open_snapshot(
            run_snapshot.path.parent / evidence.sentiment.bundle_path,
            label="Quantipy sentiment bundle",
            trusted_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            private=True,
            max_bytes=QUANTIPY_SENTIMENT_BUNDLE_MAX_BYTES,
        )
        receipt_snapshot = _secure_open_snapshot(
            run_snapshot.path.parent / evidence.sentiment.receipt_path,
            label="Quantipy sentiment receipt",
            trusted_root=constants.DEFAULT_QUANTIPY_EXPERIMENT_RUNS_ROOT,
            private=True,
            max_bytes=QUANTIPY_SENTIMENT_RECEIPT_MAX_BYTES,
        )
        _require_sealed_quantipy_panel_file(bundle_snapshot, label="Quantipy sentiment bundle")
        _require_sealed_quantipy_panel_file(receipt_snapshot, label="Quantipy sentiment receipt")
        if bundle_snapshot.sha256 != evidence.sentiment.bundle_sha256:
            raise AutoresearchValidationError(
                "Quantipy sentiment bundle digest does not match evidence"
            )
        if receipt_snapshot.sha256 != evidence.sentiment.receipt_sha256:
            raise AutoresearchValidationError(
                "Quantipy sentiment receipt digest does not match evidence"
            )
        persisted_receipt = _validate_sentiment_receipt(
            _parse_json_snapshot(receipt_snapshot, label="Quantipy sentiment receipt"),
            label="Quantipy sentiment receipt",
        )
        if persisted_receipt != run_sentiment_data["receipt"]:
            raise AutoresearchValidationError(
                "Quantipy sentiment receipt does not match nested run evidence"
            )
        _validate_quantipy_sentiment_manifest_binding(
            manifest["sentiment"], persisted_receipt, label="manifest sentiment"
        )
