"""Contract tests for repo-managed OpenClaw runtime documentation."""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_CONFIG = REPO_ROOT / "gateway" / "agent_config"
PLAN = REPO_ROOT / "docs" / "reference" / "quantipy-autonomous-research-plan.md"
DATA_CONTRACT = AGENT_CONFIG / "skills" / "quantipy-data-contract" / "SKILL.md"
METHODOLOGY = AGENT_CONFIG / "skills" / "quantipy-methodology" / "SKILL.md"
AUTORESEARCH = AGENT_CONFIG / "skills" / "autoresearch" / "SKILL.md"
CODEX_SUBAGENTS = AGENT_CONFIG / "skills" / "codex-subagents" / "SKILL.md"
CAMPAIGN_XNYS_START = "2022-01-03"
CAMPAIGN_XNYS_END = "2025-12-31"


def _runtime_docs() -> tuple[Path, ...]:
    return (*sorted(AGENT_CONFIG.rglob("*.md")), PLAN)


@pytest.mark.parametrize("name", ("AGENTS.md", "BOOTSTRAP.md", "SOUL.md", "TOOLS.md"))
def test_bootstrap_file_stays_within_openclaw_character_limit(name: str) -> None:
    assert len((AGENT_CONFIG / name).read_text(encoding="utf-8")) <= 20_000


def test_quantipy_data_contract_covers_runtime_boundaries() -> None:
    contract = DATA_CONTRACT.read_text(encoding="utf-8")
    normalized_contract = " ".join(contract.split())

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
        assert phrase in normalized_contract


def test_all_runtime_routes_reference_data_contract_and_receipts() -> None:
    paths = (
        AGENT_CONFIG / "AGENTS.md",
        AGENT_CONFIG / "BOOTSTRAP.md",
        METHODOLOGY,
        AUTORESEARCH,
        PLAN,
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "quantipy-data-contract" in text
        assert "readiness" in text.lower()
        assert "receipt" in text.lower()


def test_stale_quantipy_contract_phrases_are_absent() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in _runtime_docs())

    stale_patterns = (
        r"databento",
        r"qp\.prices\(\)\s+or\s+direct\s+sql",
        r"(?:query|read|load)\s+(?:the\s+)?(?:sql|database|repository)\s+directly",
        r"(?:call|query|use)\s+(?:massive(?:\.com)?|the\s+provider)\s+directly",
        r"(?:infer|derive|select)\s+(?:the\s+)?universe\s+from\s+(?:the\s+)?cache",
        r"(?<!never )(?<!not )(?:trade|execute|order)\s+(?:at|on)\s+(?:date\s+)?d(?:'s)?\s+close",
        r"(?<!never )(?<!not )(?:reapply|apply)\s+(?:a\s+)?"
        r"(?:second\s+)?(?:split|dividend)\s+adjustment",
        r"prefetch\s+(?:all|the\s+entire)\s+(?:market|universe).*minute",
        r"(?:point-in-time|pit)[ -](?:certified|verified)\s+market\s+cap",
        r"historical\s+trades(?:,|\s+and)\s+quotes(?:,|\s+and)\s+fundamentals\s+(?:are\s+)?(?:available|supported)",
        r"localhost:5433",
        r"\$500m-\$20b",
        r"any ticker, any timeframe",
        r"download the full 2021-2026",
        r"auto-fetches missing data",
        r"schema version 1",
        r"schema-v2 platform-readiness",
        r"schema version 2[, ]+identify a canonical `manifest_id` and `snapshot_id`",
        r"sec/common-stock provenance",
    )
    for pattern in stale_patterns:
        assert re.search(pattern, combined, flags=re.IGNORECASE) is None, pattern


def test_quantipy_history_limits_and_batching_are_documented() -> None:
    for path in (DATA_CONTRACT, AUTORESEARCH, PLAN):
        text = " ".join(path.read_text(encoding="utf-8").split()).lower()
        assert "32 dates" in text
        assert "1,000 members per date" in text
        assert "10,000" in text and "date-member slots" in text
        assert "deterministic contiguous batch" in text
        assert "one" in text and "per batch" in text


def test_consensus_and_verification_carry_the_right_universe_identity() -> None:
    for path in (DATA_CONTRACT, AUTORESEARCH, PLAN):
        text = " ".join(path.read_text(encoding="utf-8").split()).lower()
        assert "consensus" in text and "plan/profile identity" in text
        assert "materialization" in text and "verification" in text


def test_mode_specific_coverage_docs_do_not_contradict_runtime_contract() -> None:
    for path in (AGENT_CONFIG / "AGENTS.md", AUTORESEARCH, METHODOLOGY, PLAN):
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "DynamicUniverseCoverageReceipt" in text
        assert "ALPHA_RESEARCH" in text
        assert "DATA_INFRA_G0" in text
        assert re.search(
            r"ALPHA_RESEARCH.{0,180}(?:only|requires only).{0,180}"
            r"DynamicUniverseCoverageReceipt",
            text,
        ) or re.search(
            r"ALPHA_RESEARCH.{0,180}DynamicUniverseCoverageReceipt.{0,180}only",
            text,
        )
        assert re.search(
            r"(?:per-symbol|CoverageReceipt).{0,220}DATA_INFRA_G0.{0,40}only",
            text,
        ) or re.search(
            r"DATA_INFRA_G0.{0,120}only.{0,220}(?:per-symbol|CoverageReceipt)",
            text,
        )


def test_runtime_docs_atomically_prepare_the_authoritative_v2_state() -> None:
    for path in (AGENT_CONFIG / "AGENTS.md", AGENT_CONFIG / "README.md", AUTORESEARCH):
        text = path.read_text(encoding="utf-8")
        migrate_index = text.index("autoresearch-migrate-state")
        init_index = text.index("autoresearch-init-state")
        next_index = text.find("autoresearch-next", max(migrate_index, init_index))
        assert next_index > max(migrate_index, init_index)
        assert "quantipy-state-v2.json" not in text
        assert text.count("state=/home/dev/.openclaw/autoresearch/quantipy-state.json") >= 2
        assert text.count("mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.") >= 2
        assert text.count('mv -- "$tmp" "$state"') == 2
        assert re.search(
            r"autoresearch-next\s+\\\n\s+"
            r"/home/dev/\.openclaw/autoresearch/quantipy-state\.json",
            text,
        )
        assert (
            "--readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json" in text
        )


def test_autoresearch_advance_uses_locked_atomic_in_place_state_persistence() -> None:
    text = AUTORESEARCH.read_text(encoding="utf-8")
    readme = (AGENT_CONFIG / "README.md").read_text(encoding="utf-8")

    assert 'next_state="$(mktemp' not in text
    assert 'mv -- "$next_state" "$state"' not in text
    assert '--output "$state"' in text
    assert "--output /home/dev/.openclaw/autoresearch/quantipy-state.json" in readme


def test_runtime_docs_distinguish_v2_state_from_v3_readiness_and_resume_suspended_campaigns() -> (
    None
):
    runtime_paths = (
        AGENT_CONFIG / "AGENTS.md",
        AGENT_CONFIG / "README.md",
        AUTORESEARCH,
    )
    for path in runtime_paths:
        text = path.read_text(encoding="utf-8")
        assert "schema-v2 state" in text
        assert "schema-v3 platform-readiness manifest" in text or "schema version 3" in text
        rebuild_index = text.index("autoresearch-build-readiness")
        resume_index = text.index("autoresearch-resume", rebuild_index)
        assert resume_index > rebuild_index
        assert 'resumed="$(mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.' in text
        assert 'mv -- "$resumed" "$state"' in text
        assert "--campaign-xnys-start" in text
        assert "--campaign-xnys-end" in text
        assert CAMPAIGN_XNYS_START in text
        assert CAMPAIGN_XNYS_END in text

    plan_text = " ".join(PLAN.read_text(encoding="utf-8").split())
    assert "schema-v3 platform-readiness manifest" in plan_text
    assert "schema-v2 state" in plan_text
    assert "autoresearch-build-readiness" in plan_text
    assert "autoresearch-resume" in plan_text
    assert "--campaign-xnys-start" in plan_text
    assert "--campaign-xnys-end" in plan_text
    assert CAMPAIGN_XNYS_START in plan_text
    assert CAMPAIGN_XNYS_END in plan_text


def test_consensus_docs_freeze_inputs_and_derive_batch_boundaries() -> None:
    paths = (
        AGENT_CONFIG / "AGENTS.md",
        DATA_CONTRACT,
        AUTORESEARCH,
        METHODOLOGY,
        PLAN,
    )
    for path in paths:
        text = " ".join(path.read_text(encoding="utf-8").split()).lower()
        assert "canonical" in text
        assert "runner" in text
        assert "derive" in text
        assert "batch" in text or "boundaries" in text
        assert re.search(
            r"(?:stores? no|do not store|but no) (?:redundant )?batch boundaries",
            text,
        )

    combined = " ".join(path.read_text(encoding="utf-8") for path in paths).lower()
    assert "consensus stores no redundant batch boundaries" in combined
    assert (
        "preserve only universe plan/profile identity and deterministic batch boundaries"
        not in combined
    )
    assert "maximum members per date, batch boundaries" not in combined


def test_routed_target_quantipy_skills_and_agents_are_declared() -> None:
    skill_names = ("backend-python", "backtesting", "data-querying", "experiment-data")
    agent_names = (
        "backend-python",
        "contrarian",
        "explorer",
        "orchestrator",
        "researcher",
        "reviewer",
        "theorist",
    )

    methodology = METHODOLOGY.read_text(encoding="utf-8")
    for name in skill_names:
        assert f"`{name}`" in methodology
    for name in agent_names:
        assert f"`{name}.toml`" in methodology


def test_ownership_memory_and_config_guidance_remain_explicit() -> None:
    agents = " ".join(AGENT_CONFIG.joinpath("AGENTS.md").read_text(encoding="utf-8").split())
    readme = " ".join(AGENT_CONFIG.joinpath("README.md").read_text(encoding="utf-8").split())

    assert "Human/Codex owns shared loaders" in agents
    assert "only the PM may write" in agents
    assert "Do not use OpenClaw built-in memory" in agents
    assert "strict production envelope" in readme
    assert "Never write or pass a raw unwrapped `verification_result`" in agents
    assert "bash scripts/push-openclaw-config.sh" in readme
    assert "restart the OpenClaw gateway service" in readme


def test_long_task_docs_require_detached_launch_and_cleanup() -> None:
    for path in (AUTORESEARCH, CODEX_SUBAGENTS):
        text = " ".join(path.read_text(encoding="utf-8").split())
        lowered = text.lower()
        assert (
            "/home/dev/repos/g2_openclaw/scripts/run-long-task.sh --run-dir <absolute-run-dir> --"
        ) in text
        assert "bounded polling" in lowered
        assert "foreground tool call" in lowered
        assert "unsafe" in lowered
        assert "progress" in lowered
        assert "clean up" in lowered or "cleanup" in lowered
        assert (
            "do not reduce scope" in lowered
            or "do not reduce experiment or verification scope" in lowered
        )


def test_long_task_docs_distinguish_launcher_status_from_pm_blocking() -> None:
    for path in (AUTORESEARCH, CODEX_SUBAGENTS):
        text = " ".join(path.read_text(encoding="utf-8").split())
        lowered = text.lower()
        assert "`running`, `succeeded`, or `failed`" in text
        assert "[TASK:blocked]" in text
        assert "not a literal launcher status" in lowered
        assert "bounded polling" in lowered


def test_dispatch_recovery_requires_task_ledger_unique_labels() -> None:
    agents = " ".join(AGENT_CONFIG.joinpath("AGENTS.md").read_text(encoding="utf-8").split())
    autoresearch = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split())

    for text in (agents.lower(), autoresearch.lower()):
        assert "complete task ledger" in text
        assert "owner session stop" in text or "owner-session stop" in text
        assert "never reuse `r1-a1`" in text
        assert "label already in use" in text
        assert "next unused attempt" in text


def test_no_memory_and_keep_rules_match_deterministic_runner() -> None:
    protocol = AUTORESEARCH.read_text(encoding="utf-8")

    assert "`NO_CONSENSUS` and `INFRA_BLOCKED` never enter MemPalace" in protocol
    assert "Plain KEEP is invalid without a numeric" in protocol
    assert "Decision Sharpe > 0.5: SIGNIFICANT KEEP or STRONG KEEP" in protocol
    assert "Decision Sharpe > 1.0 and reviewer PASS: STRONG KEEP" in protocol
    assert "`instruction_manifest_sha256` and `state_reference_sha256` envelope" in protocol
    assert "Never pass a raw unwrapped" in protocol
    assert "`verification_result`" in protocol


def test_runtime_docs_separate_no_consensus_from_infrastructure_blocking() -> None:
    paths = (
        AGENT_CONFIG / "AGENTS.md",
        AGENT_CONFIG / "SOUL.md",
        AGENT_CONFIG / "README.md",
        AUTORESEARCH,
        PLAN,
    )

    for path in paths:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "second-round `NO_CONSENSUS` remains `NO_CONSENSUS`" in text, path
        assert "`ALPHA_RESEARCH` and `DATA_INFRA_G0`" in text, path
        assert "does not suspend" in text, path
        assert "does not write MemPalace" in text, path
        assert "fresh context" in text, path
        assert "explicit `infra_gate_outcome=REMEDIATION_REQUIRED`" in text, path

    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "G0 uses only `INFRA_REPAIRED` or `INFRA_BLOCKED`" not in combined
    assert "G0 decides only `INFRA_REPAIRED` or `INFRA_BLOCKED`" not in combined


def test_runtime_docs_require_absolute_artifact_handoff_paths() -> None:
    protocol = AUTORESEARCH.read_text(encoding="utf-8")
    normalized = " ".join(protocol.split())

    assert "artifact=/home/dev/.openclaw/workspace-autoresearch-pm/<artifact-name>.json" in protocol
    assert 'jq -e . "$artifact"' in protocol
    assert 'wc -c "$artifact"' in protocol
    assert 'autoresearch-advance "$state" "$artifact"' in protocol
    assert "absolute-path handoff template" in normalized


def test_alpha_docs_require_dynamic_coverage_and_explicit_state_migration() -> None:
    alpha_docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (AGENT_CONFIG / "AGENTS.md", AUTORESEARCH, METHODOLOGY, PLAN)
    )

    assert "DynamicUniverseCoverageReceipt" in alpha_docs
    assert "autoresearch-migrate-state" in alpha_docs
    assert "Never run `autoresearch-next` against schema-less state" in " ".join(alpha_docs.split())
    assert "ALPHA_RESEARCH" in alpha_docs
    assert "AggregateCoverageReceipt" in alpha_docs
    assert "DATA_INFRA_G0`-only" in alpha_docs
    assert "per-symbol and aggregate common-calendar coverage" not in alpha_docs
