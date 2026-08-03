"""Instruction-source manifest construction and receipt catalog helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from gateway.autoresearch import constants, memory
from gateway.autoresearch.enums import ArtifactType as ArtifactType
from gateway.autoresearch.enums import Phase as Phase
from gateway.autoresearch.errors import AutoresearchReceiptError as AutoresearchReceiptError
from gateway.autoresearch.evidence import (
    _target_repo_root_for_state as _target_repo_root_for_state,
)
from gateway.autoresearch.manifest import (
    AuthoritativeStateReference as AuthoritativeStateReference,
)
from gateway.autoresearch.manifest import (
    InstructionSourceManifest as InstructionSourceManifest,
)
from gateway.autoresearch.manifest import (
    SourceReceipt as SourceReceipt,
)
from gateway.autoresearch.policy import ReceiptCatalog as ReceiptCatalog
from gateway.autoresearch.prompts import _select_phase_target as _select_phase_target
from gateway.autoresearch.transitions import (
    build_authoritative_state_reference as build_authoritative_state_reference,
)

if TYPE_CHECKING:
    from gateway.autoresearch.policy import AutoresearchPolicy as AutoresearchPolicy
    from gateway.autoresearch.state import AutoresearchState as AutoresearchState


def build_instruction_source_manifest(
    *,
    phase: Phase,
    expected_artifact_type: ArtifactType,
    target_agent_ids: Sequence[str],
    target_repo_root: Path,
    state: AutoresearchState,
    state_path: Path = constants.DEFAULT_AUTORESEARCH_STATE_PATH,
    receipts: Sequence[SourceReceipt],
) -> InstructionSourceManifest:
    return InstructionSourceManifest.from_context(
        phase=phase,
        expected_artifact_type=expected_artifact_type,
        target_agent_ids=target_agent_ids,
        target_repo_root=target_repo_root,
        state=state,
        state_path=state_path,
        receipts=receipts,
    )


def instruction_source_manifest_sha256(
    *,
    phase: Phase,
    expected_artifact_type: ArtifactType,
    target_agent_ids: Sequence[str],
    target_repo_root: Path,
    state: AutoresearchState,
    state_path: Path = constants.DEFAULT_AUTORESEARCH_STATE_PATH,
    receipts: Sequence[SourceReceipt],
) -> str:
    return build_instruction_source_manifest(
        phase=phase,
        expected_artifact_type=expected_artifact_type,
        target_agent_ids=target_agent_ids,
        target_repo_root=target_repo_root,
        state=state,
        state_path=state_path,
        receipts=receipts,
    ).sha256()


def expected_instruction_manifest_sha256(
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    receipts: ReceiptCatalog,
    *,
    state_path: Path = constants.DEFAULT_AUTORESEARCH_STATE_PATH,
) -> str:
    target = _select_phase_target(state, policy)
    required_receipts = receipts.require(PHASE_RECEIPTS[state.phase])
    return instruction_source_manifest_sha256(
        phase=state.phase,
        expected_artifact_type=target.artifact_type,
        target_agent_ids=target.agent_ids,
        target_repo_root=_target_repo_root_for_state(state),
        state=state,
        state_path=state_path,
        receipts=required_receipts,
    )


LOCAL_RECEIPT_PATHS: dict[str, Path] = {
    "g2.autoresearch_skill": Path("gateway/agent_config/skills/autoresearch/SKILL.md"),
    "g2.quantipy_methodology": Path("gateway/agent_config/skills/quantipy-methodology/SKILL.md"),
    "g2.quantipy_data_contract": Path(
        "gateway/agent_config/skills/quantipy-data-contract/SKILL.md"
    ),
}

QUANTIPY_RECEIPT_PATHS: dict[str, Path] = {
    "quantipy.agents": Path("AGENTS.md"),
    "quantipy.skill.backend_python": Path(".agents/skills/backend-python/SKILL.md"),
    "quantipy.skill.backtesting": Path(".agents/skills/backtesting/SKILL.md"),
    "quantipy.skill.data_collection": Path(".agents/skills/data-collection/SKILL.md"),
    "quantipy.skill.data_querying": Path(".agents/skills/data-querying/SKILL.md"),
    "quantipy.skill.experiment_data": Path(".agents/skills/experiment-data/SKILL.md"),
    "quantipy.agent.backend_python": Path(".codex/agents/backend-python.toml"),
    "quantipy.agent.contrarian": Path(".codex/agents/contrarian.toml"),
    "quantipy.agent.explorer": Path(".codex/agents/explorer.toml"),
    "quantipy.agent.orchestrator": Path(".codex/agents/orchestrator.toml"),
    "quantipy.agent.researcher": Path(".codex/agents/researcher.toml"),
    "quantipy.agent.reviewer": Path(".codex/agents/reviewer.toml"),
    "quantipy.agent.theorist": Path(".codex/agents/theorist.toml"),
}

PHASE_RECEIPTS: dict[Phase, tuple[str, ...]] = {
    Phase.SETUP_CONTEXT: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.experiment_data",
        "quantipy.skill.data_querying",
        "quantipy.agent.explorer",
        "quantipy.agent.researcher",
    ),
    Phase.DEBATE: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backend_python",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_collection",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.backend_python",
        "quantipy.agent.contrarian",
        "quantipy.agent.orchestrator",
        "quantipy.agent.researcher",
        "quantipy.agent.reviewer",
        "quantipy.agent.theorist",
    ),
    Phase.CONSENSUS: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_collection",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.contrarian",
        "quantipy.agent.researcher",
        "quantipy.agent.reviewer",
        "quantipy.agent.theorist",
    ),
    Phase.IMPLEMENTATION: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backend_python",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.backend_python",
        "quantipy.agent.orchestrator",
    ),
    Phase.VERIFICATION: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.reviewer",
    ),
    Phase.REVIEW: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.contrarian",
        "quantipy.agent.reviewer",
    ),
    Phase.FIX_TEST: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
        "quantipy.skill.backend_python",
        "quantipy.skill.backtesting",
        "quantipy.skill.data_querying",
        "quantipy.skill.experiment_data",
        "quantipy.agent.backend_python",
        "quantipy.agent.orchestrator",
        "quantipy.agent.reviewer",
    ),
    Phase.DECISION_LOG: (
        "g2.autoresearch_skill",
        "g2.quantipy_methodology",
        "g2.quantipy_data_contract",
        "quantipy.agents",
    ),
    Phase.REPEAT: (
        "g2.autoresearch_skill",
        "quantipy.agents",
    ),
}


def _canonical_receipt_path(path: Path) -> Path:
    try:
        canonical = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise AutoresearchReceiptError(f"missing required receipt source: {path}") from exc
    if not canonical.is_file():
        raise AutoresearchReceiptError(f"missing required receipt source: {canonical}")
    return canonical


def _load_receipt(receipt_id: str, path: Path) -> SourceReceipt:
    canonical = _canonical_receipt_path(path)
    try:
        content = canonical.read_bytes()
    except OSError as exc:
        raise AutoresearchReceiptError(f"unreadable required receipt source: {canonical}") from exc
    return SourceReceipt(
        receipt_id=receipt_id,
        path=canonical,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def build_receipt_catalog(quantipy_root: Path = constants.DEFAULT_QUANTIPY_ROOT) -> ReceiptCatalog:
    receipts: dict[str, SourceReceipt] = {}
    for receipt_id, path in LOCAL_RECEIPT_PATHS.items():
        receipts[receipt_id] = _load_receipt(receipt_id, memory.G2_OPENCLAW_REPO_ROOT / path)
    for receipt_id, relative_path in QUANTIPY_RECEIPT_PATHS.items():
        receipts[receipt_id] = _load_receipt(receipt_id, quantipy_root / relative_path)
    return ReceiptCatalog(receipts=receipts)
