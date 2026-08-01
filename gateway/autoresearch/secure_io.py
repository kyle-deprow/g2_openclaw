"""Hardened filesystem snapshot and validation helpers for autoresearch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES as CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    MAX_ARTIFACT_FILE_BYTES as MAX_ARTIFACT_FILE_BYTES,
)
from gateway.autoresearch.constants import (
    QUANTIPY_EXPERIMENT_SOURCE_PATH_MAX_LENGTH as QUANTIPY_EXPERIMENT_SOURCE_PATH_MAX_LENGTH,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import _ensure_mapping as _ensure_mapping
from gateway.autoresearch.fields import _strict_json_datetime as _strict_json_datetime
from gateway.autoresearch.fields import _strict_json_enum as _strict_json_enum
from gateway.autoresearch.fields import _strict_json_keys as _strict_json_keys
from gateway.autoresearch.fields import _strict_json_string as _strict_json_string


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
