"""Operator recovery receipt value objects."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES as CANONICAL_QUANTIPY_BASE_INTERPRETER_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES as CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION as PLATFORM_RUNTIME_RECOVERY_RECEIPT_SCHEMA_VERSION,  # noqa: E501
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _require_exact_keys as _require_exact_keys,
)
from gateway.autoresearch.fields import (
    _require_int as _require_int,
)
from gateway.autoresearch.fields import (
    _require_sha256 as _require_sha256,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)
from gateway.autoresearch.secure_io import (
    _path_is_within as _path_is_within,
)
from gateway.autoresearch.secure_io import (
    _require_canonical_absolute_path as _require_canonical_absolute_path,
)


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
