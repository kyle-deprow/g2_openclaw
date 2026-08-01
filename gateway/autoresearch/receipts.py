"""Autoresearch universe, price, and coverage receipt value objects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
    _require_sha256 as _require_sha256,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)


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
