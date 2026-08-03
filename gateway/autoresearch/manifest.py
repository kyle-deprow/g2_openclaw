"""Instruction-source manifest value objects and path helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch.constants import (
    AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN as AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN,
)
from gateway.autoresearch.constants import (
    INSTRUCTION_SOURCE_MANIFEST_DIGEST_DOMAIN as INSTRUCTION_SOURCE_MANIFEST_DIGEST_DOMAIN,
)
from gateway.autoresearch.constants import (
    INSTRUCTION_SOURCE_MANIFEST_VERSION as INSTRUCTION_SOURCE_MANIFEST_VERSION,
)
from gateway.autoresearch.errors import (
    AutoresearchReceiptError as AutoresearchReceiptError,
)
from gateway.autoresearch.fields import _sha256_text as _sha256_text

if TYPE_CHECKING:
    from gateway.autoresearch.enums import ArtifactType, Phase
    from gateway.autoresearch.state import AutoresearchState


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
        from gateway.autoresearch.transitions import (
            build_authoritative_state_reference as build_authoritative_state_reference,
        )

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
