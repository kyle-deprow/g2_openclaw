"""Instruction-source manifest value objects and path helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from gateway.autoresearch.constants import (
    AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN as AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN,
)
from gateway.autoresearch.fields import _sha256_text as _sha256_text


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    receipt_id: str
    path: Path
    sha256: str

    @property
    def label(self) -> str:
        return self.path.name

    def to_dict(self) -> dict[str, str]:
        return {
            "receipt_id": self.receipt_id,
            "path": str(self.path),
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class InstructionSourceEntry:
    receipt_id: str
    path: str
    sha256: str

    @classmethod
    def from_receipt(cls, receipt: SourceReceipt) -> InstructionSourceEntry:
        return cls(
            receipt_id=receipt.receipt_id,
            path=str(receipt.path),
            sha256=receipt.sha256,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "receipt_id": self.receipt_id,
            "path": self.path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeStateReference:
    """Digest-bound location of the complete state required by a stage agent."""

    version: str
    digest_domain: str
    path: str
    state_sha256: str
    phase: str
    iteration: int

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "digest_domain": self.digest_domain,
            "path": self.path,
            "state_sha256": self.state_sha256,
            "phase": self.phase,
            "iteration": self.iteration,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return _sha256_text(
            "\n".join(
                (
                    AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN,
                    self.version,
                    self.canonical_json(),
                )
            )
        )
