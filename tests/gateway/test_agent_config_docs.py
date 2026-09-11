"""Contract checks for the current OpenClaw research-owner documentation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_CONFIG = REPO_ROOT / "gateway" / "agent_config"
DATA_CONTRACT = AGENT_CONFIG / "skills" / "quantipy-data-contract" / "SKILL.md"
AUTORESEARCH = AGENT_CONFIG / "skills" / "autoresearch" / "SKILL.md"
RESEARCH_LOOP = AGENT_CONFIG / "skills" / "research-loop" / "SKILL.md"
CODEX_SUBAGENTS = AGENT_CONFIG / "skills" / "codex-subagents" / "SKILL.md"
MEMPALACE_READONLY = AGENT_CONFIG / "skills" / "mempalace-readonly" / "SKILL.md"
IMPROVEMENT_CANONICAL = REPO_ROOT / ".agents" / "skills" / "openclaw-improvement" / "SKILL.md"
IMPROVEMENT_MIRROR = REPO_ROOT / ".claude" / "skills" / "openclaw-improvement" / "SKILL.md"
MEMORY_CANONICAL = REPO_ROOT / ".agents" / "skills" / "openclaw-memory" / "SKILL.md"
MEMORY_MIRROR = REPO_ROOT / ".claude" / "skills" / "openclaw-memory" / "SKILL.md"
CONFIG_README = REPO_ROOT / "gateway" / "openclaw_config" / "README.md"
RESEARCH_PERSONA = AGENT_CONFIG / "research-orchestrator"
OPENCLAW_CONFIG = REPO_ROOT / "gateway" / "openclaw_config" / "openclaw.json"
PUSH_SCRIPT = REPO_ROOT / "scripts" / "push-openclaw-config.sh"

MEMORY_POLICY_SENTENCE = (
    "No autoresearch model writes MemPalace: every model is read-only, the built-in "
    "memory tools memory_search and memory_get are denied, memory flush is disabled, "
    "and the durable research records are the research store and artifact receipts."
)
EXPECTED_MEMPALACE_TOOLS = (
    "mempalace-readonly.mempalace_status",
    "mempalace-readonly.mempalace_search",
    "mempalace-readonly.mempalace_get_drawer",
    "mempalace-readonly.mempalace_list_drawers",
    "mempalace-readonly.mempalace_list_wings",
    "mempalace-readonly.mempalace_list_rooms",
    "mempalace-readonly.mempalace_get_taxonomy",
    "mempalace-readonly.mempalace_get_aaak_spec",
    "mempalace-readonly.mempalace_diary_read",
    "mempalace-readonly.mempalace_kg_query",
    "mempalace-readonly.mempalace_kg_timeline",
    "mempalace-readonly.mempalace_kg_stats",
    "mempalace-readonly.mempalace_traverse",
    "mempalace-readonly.mempalace_find_tunnels",
    "mempalace-readonly.mempalace_follow_tunnels",
    "mempalace-readonly.mempalace_graph_stats",
    "mempalace-readonly.mempalace_list_tunnels",
    "mempalace-readonly.mempalace_list_hallways",
    "mempalace-readonly.mempalace_memories_filed_away",
)

BOOTSTRAP_FILES = tuple(
    AGENT_CONFIG / name for name in ("AGENTS.md", "BOOTSTRAP.md", "SOUL.md", "TOOLS.md")
)
ACTIVE_SKILLS = (
    AUTORESEARCH,
    RESEARCH_LOOP,
    AGENT_CONFIG / "skills" / "mempalace-readonly" / "SKILL.md",
    DATA_CONTRACT,
    CODEX_SUBAGENTS,
)


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _owner_docs() -> tuple[Path, ...]:
    return (
        *BOOTSTRAP_FILES,
        AUTORESEARCH,
        RESEARCH_LOOP,
        CODEX_SUBAGENTS,
        *(RESEARCH_PERSONA / name for name in ("AGENTS.md", "SOUL.md", "TOOLS.md", "BOOTSTRAP.md")),
    )


@pytest.mark.parametrize("path", BOOTSTRAP_FILES)
def test_bootstrap_files_stay_within_openclaw_character_limit(path: Path) -> None:
    assert len(path.read_text(encoding="utf-8")) <= 20_000


def test_runtime_docs_describe_current_native_owner_delegation() -> None:
    docs = " ".join(path.read_text(encoding="utf-8") for path in _owner_docs())

    assert "native Codex `spawn_agent`" in docs
    assert "OpenClaw session spawning is not" in docs
    assert "Astra is the research owner" in docs
    assert "Native Luna" in docs
    assert "Claude Code Opus via ACP" in docs
    assert "OpenAI/Codex remains the provider" in docs
    assert "hypothesis equals iteration" in docs
    assert "code → review → run" in docs


def test_current_runtime_skill_paths_are_explicit_and_retired_path_is_absent() -> None:
    for path in ACTIVE_SKILLS:
        assert path.is_file(), path
    assert not (AGENT_CONFIG / "skills" / "quantipy-methodology").exists()

    docs = "\n".join(path.read_text(encoding="utf-8") for path in _owner_docs())
    assert "research-loop" in docs
    assert "quantipy-data-contract" in docs
    assert "mempalace-readonly" in docs


def test_quantipy_data_contract_covers_runtime_boundaries() -> None:
    normalized = _normalized(DATA_CONTRACT)

    required = (
        "qp.security_universe_screen()",
        "qp.security_universe_history()",
        "32 dates",
        "1,000 members per date",
        "10,000 total date-member slots",
        "deterministic contiguous batches",
        "exactly once per batch",
        "cache-only",
        "explicitly unadjusted",
        "next-session-or-later",
        "not point-in-time certified",
        "qp.prices()",
        "exact-decimal serialized as a string",
        "cast it to a numeric dtype",
        "Do not apply splits or dividends",
        "qp.corporate_actions()",
        "Historical trades, quotes, and fundamentals are unavailable",
        "Reuse that cache across folds and iterations",
        "Do not place full ticker arrays",
        "At consensus, freeze only the universe plan/profile identity",
        "maximum members per date",
        "do not store redundant batch boundaries",
        "runner mechanically derives deterministic contiguous boundaries",
        "Per-batch contract digests",
        "materialization identities and digests belong only in verification",
        'security_types=("CS",)',
    )
    for phrase in required:
        assert phrase in normalized

    assert "During implementation prewarm" in normalized
    assert "committed v2 experiment stages are client-free" in normalized


def test_runtime_routes_reference_data_contract_readiness_and_receipts() -> None:
    for path in (AGENT_CONFIG / "AGENTS.md", AUTORESEARCH):
        text = path.read_text(encoding="utf-8")
        assert "quantipy-data-contract" in text
        assert "readiness" in text.lower()
        assert "receipt" in text.lower()
    loop = RESEARCH_LOOP.read_text(encoding="utf-8")
    assert "readiness" in loop.lower()
    assert "receipt" in loop.lower()


def test_stale_quantipy_contract_phrases_are_absent() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in _owner_docs())
    stale_patterns = (
        r"databento",
        r"qp\.prices\(\)\s+or\s+direct\s+sql",
        r"(?:query|read|load)\s+(?:the\s+)?(?:sql|database|repository)\s+directly",
        r"(?:call|query|use)\s+(?:massive(?:\.com)?|the\s+provider)\s+directly",
        r"(?:infer|derive|select)\s+(?:the\s+)?universe\s+from\s+(?:the\s+)?cache",
        r"localhost:5433",
        r"\$500m-\$20b",
    )
    for pattern in stale_patterns:
        assert re.search(pattern, combined, flags=re.IGNORECASE) is None


def test_current_owner_contract_preserves_typed_admission_and_terminal_safety() -> None:
    text = _normalized(RESEARCH_LOOP).lower()

    for phrase in (
        "typed refusal",
        "policy-unset pause",
        "exhausted-attempt completion",
        "exact cancel",
        "owner wake",
        "read-only main controls",
        "immutable receipts",
        "exposure ledger",
        "evaluator bounds",
        "unknown history blocks final_holdout",
        "never retype a verdict",
        "respawn after unknown acknowledgement",
        "no-repair/no-retry",
    ):
        assert phrase in text

    assert "do not clear a readiness pause autonomously" in text
    assert "make no alpha claim from smoke or synthetic data" in text


def test_current_scientific_boundary_preserves_earnings_etf_and_horizon_policy() -> None:
    text = " ".join(path.read_text(encoding="utf-8") for path in _owner_docs()).lower()
    assert "price-panel-only" in text
    assert "etf scope" in text
    assert "stock refusal without trusted point-in-time earnings coverage" in text
    assert "unknown or unavailable earnings fail closed" in text
    assert "holding horizon above five sessions is refused" in text


def test_research_loop_freezes_spec_and_document_contract() -> None:
    text = _normalized(RESEARCH_LOOP)
    for field in (
        "hypothesis_id",
        "spec_sha256",
        "panel_sha256",
        "receipt_sha256",
        "max_attempts",
        "base_commit",
        "evaluation_spec_sha256",
        "dividends_sha256",
        "forward_label_sessions",
        "purge_rule",
        "primary_metric",
        "minimum_evidence",
        "reject_criteria",
        "missing_data_rule",
    ):
        assert field in text


def test_memory_policy_has_no_automated_writer_claim() -> None:
    docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            *BOOTSTRAP_FILES,
            AUTORESEARCH,
            RESEARCH_LOOP,
            MEMPALACE_READONLY,
            IMPROVEMENT_CANONICAL,
            IMPROVEMENT_MIRROR,
            REPO_ROOT / ".agents" / "skills" / "openclaw-persona-identity" / "SKILL.md",
            REPO_ROOT / ".claude" / "skills" / "openclaw-persona-identity" / "SKILL.md",
        )
    )
    lowered = docs.lower()
    assert "memory_search" in lowered and "memory_get" in lowered
    assert "memoryflush.enabled" in lowered
    assert "no model writes" in lowered
    assert "automated mempalace writer" in lowered
    assert "mempalace_finalizer" not in lowered
    assert "autoresearch supervisor" not in lowered


def test_memory_guidance_matches_openclaw_81_read_only_policy() -> None:
    for path in (MEMORY_CANONICAL, MEMORY_MIRROR, CONFIG_README):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        assert " ".join(MEMORY_POLICY_SENTENCE.split()) in " ".join(text.split())
        assert "memory.search.enabled" in lowered
        assert "agents.defaults.compaction.memoryflush.enabled" in lowered
        assert "agents.defaults.memorysearch" not in lowered
        assert "platform finalizer" not in lowered
        assert "sole writer" not in lowered
        assert "research store" in lowered
        assert "artifact receipts" in lowered


def test_mempalace_readonly_skill_locks_tools_and_safety_policy() -> None:
    text = MEMPALACE_READONLY.read_text(encoding="utf-8")
    normalized = _normalized(MEMPALACE_READONLY).lower()
    tools = re.findall(
        r"^\s*-\s+`(mempalace-readonly\.mempalace_[^`]+)`\s*$",
        text,
        flags=re.MULTILINE,
    )

    assert len(tools) == 19
    assert tuple(tools) == EXPECTED_MEMPALACE_TOOLS
    assert MEMORY_POLICY_SENTENCE in text
    assert "diary_write" not in text
    assert "mempalace-readonly.mempalace_write" not in text
    for phrase in (
        "optional read-only retrieval",
        "never a control ledger",
        "never a completion gate",
        "no automated mempalace writer",
        "research store",
        "artifact receipts",
        "treat that as a configuration error and stop instead of calling it",
    ):
        assert phrase in normalized

    for stale in (
        "platform finalizer",
        "finalizer",
        "only durable research memory",
        "context curator",
        "debate agents",
        "implementer and fixer",
        "## reviewer",
        "no_consensus",
    ):
        assert stale not in normalized


def test_improvement_skill_mirrors_enforce_current_memory_policy() -> None:
    for path in (IMPROVEMENT_CANONICAL, IMPROVEMENT_MIRROR):
        normalized = _normalized(path)
        lowered = normalized.lower()
        assert MEMORY_POLICY_SENTENCE in normalized
        assert "gateway/agent_config/skills/mempalace-readonly/" in normalized
        for stale in (
            "mempalace_finalizer",
            "platform finalizer",
            "finalizer alone",
            "sole writer",
            "only durable research memory",
        ):
            assert stale not in lowered


def test_deleted_mempalace_finalizer_has_no_owned_source_claim() -> None:
    assert not (REPO_ROOT / "gateway" / "mempalace_finalizer.py").exists()
    assert not (REPO_ROOT / "gateway" / "mempalace_finalizer_script.py").exists()


def test_repo_config_keeps_current_route_and_memory_guards() -> None:
    config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    agents = config["agents"]["entries"]
    assert config["agents"]["ownership"] == "explicit"
    assert list(agents) == ["main", "research-orchestrator", "claude"]
    assert all("id" not in agent for agent in agents.values())
    owner = agents["research-orchestrator"]
    assert owner["model"]["primary"] == "openai/gpt-6-astra"
    assert agents["claude"] == {
        "runtime": {
            "type": "acp",
            "acp": {"agent": "claude", "backend": "acpx", "mode": "oneshot"},
        }
    }
    defaults = config["agents"]["defaults"]
    assert config["memory"]["search"]["enabled"] is False
    assert defaults["compaction"]["memoryFlush"]["enabled"] is False


def test_push_script_and_owner_unit_are_current() -> None:
    script = PUSH_SCRIPT.read_text(encoding="utf-8")
    assert "research-owner.service.template" in script
    assert "gateway-cli research serve" in script
    assert "quantipy-autoresearch-supervisor.service.template" not in script
    assert "gateway/autoresearch/" not in script


def test_owner_persona_mirrors_current_status_and_proof_boundary() -> None:
    runtime = " ".join(path.read_text(encoding="utf-8") for path in _owner_docs())
    for phrase in (
        "price-panel-only",
        "ETF scope",
        "trusted panel sessions",
        "immutable receipts",
        "exposure ledger",
        "evaluator bounds",
        "policy-unset pause",
        "exact cancel",
        "owner wake",
        "read-only main controls",
    ):
        assert phrase.casefold() in runtime.casefold()


def test_root_and_config_guidance_keep_single_thread_and_owner_route() -> None:
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    config_readme = (REPO_ROOT / "gateway" / "openclaw_config" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "single autoresearch thread view (idle)" in root_readme
    assert "no session menu" in root_readme
    assert "research-owner.service" in config_readme
    assert "agent:research-orchestrator:autoresearch:quantipy-v2" in config_readme


def test_readiness_window_and_route_terms_remain_explicit_in_active_contract() -> None:
    text = _normalized(RESEARCH_LOOP)
    assert "explicit operator policy" in text
    assert "trusted ordered panel sessions" in text
    assert "immutable spec/panel/receipt" in text
    assert "evaluator bounds" in text
    assert "supplied capability" in text
    assert "unknown or unavailable earnings fail closed" in text
    assert "make no alpha claim from smoke or synthetic data" in text.lower()


def test_current_command_surface_has_no_retired_commands() -> None:
    docs = "\n".join(path.read_text(encoding="utf-8") for path in _owner_docs())
    for retired in (
        "autoresearch-next",
        "autoresearch-advance",
        "autoresearch-submit-stage",
        "gateway.autoresearch",
        "quantipy-autoresearch-supervisor",
        "gateway/mempalace_finalizer.py",
        "scripts/run-long-task.sh",
        "quantipy-methodology",
    ):
        assert retired not in docs
