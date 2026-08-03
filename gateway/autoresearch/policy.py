"""Autoresearch stage policy and instruction-source receipt catalog values."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from gateway.autoresearch.compute import (
    StageAgentPolicy as StageAgentPolicy,
)
from gateway.autoresearch.errors import (
    AutoresearchReceiptError as AutoresearchReceiptError,
)
from gateway.autoresearch.manifest import (
    SourceReceipt as SourceReceipt,
)
from gateway.autoresearch.transitions import (
    _compact_json_block as _compact_json_block,
)


@dataclass(frozen=True, slots=True)
class AutoresearchPolicy:
    pm: StageAgentPolicy
    main_interface: StageAgentPolicy
    context_curator: StageAgentPolicy
    debate_agents: tuple[StageAgentPolicy, ...]
    consensus: StageAgentPolicy
    implementer: StageAgentPolicy
    reviewer: StageAgentPolicy
    fixer: StageAgentPolicy

    @property
    def debate_agent_ids(self) -> tuple[str, ...]:
        return tuple(agent.agent_id for agent in self.debate_agents)

    @property
    def all_stage_agent_ids(self) -> tuple[str, ...]:
        return (
            self.context_curator.agent_id,
            *self.debate_agent_ids,
            self.consensus.agent_id,
            self.implementer.agent_id,
            self.reviewer.agent_id,
            self.fixer.agent_id,
        )

    def model_policy_summary(self) -> str:
        """Render an indexed, lossless policy without repeating shared values."""
        agents = (
            self.pm,
            self.main_interface,
            self.context_curator,
            *self.debate_agents,
            self.consensus,
            self.implementer,
            self.reviewer,
            self.fixer,
        )
        skill_sets: list[tuple[str, ...]] = []
        models: list[str] = []
        reasoning_levels: list[str] = []
        for agent in agents:
            if agent.skills not in skill_sets:
                skill_sets.append(agent.skills)
            if agent.model not in models:
                models.append(agent.model)
            if agent.reasoning not in reasoning_levels:
                reasoning_levels.append(agent.reasoning)
        payload: dict[str, object] = {
            "agent_format": (
                "agent_id",
                "model_index",
                "reasoning_index",
                "skill_set_index",
            ),
            "models": models,
            "reasoning_levels": reasoning_levels,
            "skill_sets": skill_sets,
            "agents": [
                (
                    agent.agent_id,
                    models.index(agent.model),
                    reasoning_levels.index(agent.reasoning),
                    skill_sets.index(agent.skills),
                )
                for agent in agents
            ],
        }
        return _compact_json_block(payload)


@dataclass(frozen=True, slots=True)
class ReceiptCatalog:
    receipts: dict[str, SourceReceipt]

    def require(self, receipt_ids: Sequence[str]) -> tuple[SourceReceipt, ...]:
        ordered: list[SourceReceipt] = []
        for receipt_id in receipt_ids:
            try:
                ordered.append(self.receipts[receipt_id])
            except KeyError as exc:
                raise AutoresearchReceiptError(f"missing receipt id: {receipt_id}") from exc
        return tuple(ordered)
