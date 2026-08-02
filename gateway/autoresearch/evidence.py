"""Quantipy execution contracts and immutable run-evidence validators."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

from gateway.autoresearch.artifacts import (
    QuantipyExecutionNotStartedEvidence as QuantipyExecutionNotStartedEvidence,
)
from gateway.autoresearch.artifacts import (
    QuantipyExperimentFailureEvidence as QuantipyExperimentFailureEvidence,
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
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
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
        Path(state.setup.target_repo) if state.setup is not None else DEFAULT_QUANTIPY_ROOT
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


def _run_failure_from_mapping(raw: object) -> QuantipyExperimentFailureEvidence | None:
    if raw is None:
        return None
    return QuantipyExperimentFailureEvidence.from_dict(raw)


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
