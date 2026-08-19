"""Contract tests for repo-managed OpenClaw runtime documentation."""

import re
import tomllib
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
CODEX_AGENTS = REPO_ROOT / ".codex" / "agents"
NATIVE_CODEX_STAGE_MODELS = {
    "context_curator": "gpt-5.4",
    "debater_microstructure": "gpt-5.5",
    "debater_data": "gpt-5.6-terra",
    "debater_skeptic": "gpt-5.5",
    "debater_theory": "gpt-5.4",
    "debater_implementation": "gpt-5.4",
    "consensus_arbiter": "gpt-5.6-sol",
    "implementer": "gpt-5.4",
    "reviewer": "gpt-5.6-sol",
    "fixer": "gpt-5.4",
}


def _runtime_docs() -> tuple[Path, ...]:
    return (*sorted(AGENT_CONFIG.rglob("*.md")), PLAN)


@pytest.mark.parametrize("name", ("AGENTS.md", "BOOTSTRAP.md", "SOUL.md", "TOOLS.md"))
def test_bootstrap_file_stays_within_openclaw_character_limit(name: str) -> None:
    assert len((AGENT_CONFIG / name).read_text(encoding="utf-8")) <= 20_000


def test_native_codex_stage_agents_match_autoresearch_model_contract() -> None:
    for agent_name, model in NATIVE_CODEX_STAGE_MODELS.items():
        path = CODEX_AGENTS / f"{agent_name}.toml"
        data = tomllib.loads(path.read_text(encoding="utf-8"))

        assert data["name"] == agent_name
        assert data["model"] == model
        assert data["model_reasoning_effort"] == "high"


def test_runtime_docs_require_native_codex_stage_delegation() -> None:
    docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            AGENT_CONFIG / "AGENTS.md",
            AGENT_CONFIG / "SOUL.md",
            AGENT_CONFIG / "TOOLS.md",
            AUTORESEARCH,
            CODEX_SUBAGENTS,
        )
    )
    normalized = " ".join(docs.split())

    assert "native Codex `spawn_agent`" in normalized
    assert "OpenClaw `sessions_spawn` is not a valid substitute" in normalized
    assert (
        "PM config denies the OpenClaw tools `sessions_spawn`, `sessions_yield`, "
        "`agents_list`, `sessions_list`, and `sessions_history`" in normalized
    )


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

    assert "During implementation prewarm" in normalized_contract
    assert "committed v2 experiment stages are client-free" in normalized_contract


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
        init_index = text.index("autoresearch-init-state")
        next_index = text.find("autoresearch-next", init_index)
        assert next_index > init_index
        assert "autoresearch-migrate-state" not in text
        assert "quantipy-state-v2.json" not in text
        assert text.count("state=/home/dev/.openclaw/autoresearch/quantipy-state.json") >= 1
        assert text.count("mktemp /home/dev/.openclaw/autoresearch/.quantipy-state.json.") >= 1
        assert text.count('mv -- "$tmp" "$state"') >= 1
        assert re.search(
            r"autoresearch-next\s+\\\n\s+"
            r"/home/dev/\.openclaw/autoresearch/quantipy-state\.json",
            text,
        )
        assert (
            "--readiness-manifest /home/dev/.openclaw/autoresearch/platform-readiness.json" in text
        )


def test_runtime_docs_define_canonical_decision_receipt_authority() -> None:
    docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            AGENT_CONFIG / "AGENTS.md",
            AGENT_CONFIG / "BOOTSTRAP.md",
            AUTORESEARCH,
            PLAN,
        )
    )
    normalized = " ".join(docs.split()).lower()

    assert "canonical decision receipts" in normalized
    assert "platform decision authority" in normalized
    assert "supervisor/controller" in normalized
    assert "read-only" in normalized
    assert "memPalace".lower() in normalized


def test_model_state_advances_route_through_supervisor_owned_inbox() -> None:
    text = AUTORESEARCH.read_text(encoding="utf-8")
    readme = (AGENT_CONFIG / "README.md").read_text(encoding="utf-8")

    assert 'next_state="$(mktemp' not in text
    assert 'mv -- "$next_state" "$state"' not in text
    assert '--output "$state"' not in text
    assert "never write the authoritative state file directly" in text
    assert "reserved for the unsandboxed supervisor and" in text
    assert "--output /home/dev/.openclaw/autoresearch/quantipy-state.json" not in readme
    assert "never write the authoritative state file directly" in readme
    assert "reserved for the unsandboxed supervisor and operator" in readme


def test_runtime_docs_distinguish_v3_state_from_v3_readiness_and_resume_suspended_campaigns() -> (
    None
):
    runtime_paths = (
        AGENT_CONFIG / "AGENTS.md",
        AGENT_CONFIG / "README.md",
        AUTORESEARCH,
    )
    for path in runtime_paths:
        text = path.read_text(encoding="utf-8")
        assert "schema-v5 state" in text
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
    assert "schema-v5 state" in plan_text
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
    assert "platform finalizer alone writes" in agents
    assert "Do not use OpenClaw built-in memory" in agents
    assert "strict production envelope" in readme
    assert "Never write or pass a raw unwrapped `verification_result`" in agents
    assert "bash scripts/push-openclaw-config.sh" in readme
    assert "restart the OpenClaw gateway service" in readme


def test_long_task_docs_require_detached_launch_and_cleanup() -> None:
    for path in (AUTORESEARCH, CODEX_SUBAGENTS):
        text = " ".join(path.read_text(encoding="utf-8").split())
        lowered = text.lower()
        assert ("/home/dev/repos/g2_openclaw/scripts/run-long-task.sh") in text
        assert "--command-file" in text
        assert "autoresearch-create-command-file" in text
        assert "positional command payloads" in lowered
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
        assert "[TASK:blocked]" not in text
        assert "literal launcher status" in lowered
        assert "bounded polling" in lowered


def test_dispatch_recovery_requires_task_ledger_unique_labels() -> None:
    agents = " ".join(AGENT_CONFIG.joinpath("AGENTS.md").read_text(encoding="utf-8").split())
    autoresearch = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split())

    for text in (agents.lower(), autoresearch.lower()):
        assert "do not enumerate" in text
        assert "owner session stop" in text or "owner-session stop" in text
        assert "label already in use" in text
        assert "increment the attempt" in text


def test_no_memory_and_keep_rules_match_deterministic_runner() -> None:
    protocol = AUTORESEARCH.read_text(encoding="utf-8")
    readme = AGENT_CONFIG.joinpath("README.md").read_text(encoding="utf-8")
    soul = AGENT_CONFIG.joinpath("SOUL.md").read_text(encoding="utf-8")

    assert "Every policy-approved no-memory outcome bypasses MemPalace" in protocol
    assert "Plain KEEP is invalid without a numeric" in protocol
    assert "Decision Sharpe > 0.5: SIGNIFICANT KEEP or STRONG KEEP" in protocol
    assert "Decision Sharpe > 1.0 and reviewer PASS: STRONG KEEP" in protocol
    assert "`instruction_manifest_sha256` and `state_reference_sha256` envelope" in protocol
    assert "Never pass a raw unwrapped" in protocol
    assert "`verification_result`" in protocol
    assert "`status=PASS` and `tests_passed=true`" in readme
    assert "`status=PASS` and `tests_passed=true`" in soul
    assert "memory-required decisions such as `INFRA_REPAIRED`" not in protocol


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
        assert "`ALPHA_RESEARCH` and `DATA_INFRA_G0`" in text, path
        assert "operator-owned readiness suspension" in text, path
        if path is AUTORESEARCH:
            assert "cluster closely related proposals into theory-family clusters" in text
            assert "pre-registered deterministic tie-break" in text
            assert "lexicographically smallest normalized family name" in text
            assert "Record in `dissent_summary`" in text
            assert "The review flag is a standing advisory to the operator" in text
            assert "returns `NO_ACTION` with reason `campaign_review_pending`" not in text
            assert "sends no wake, finalization, or stage dispatch" not in text
        else:
            assert "second-round `NO_CONSENSUS` remains `NO_CONSENSUS`" in text, path
            assert "does not suspend" in text, path
            assert "does not write MemPalace" in text, path
            assert "fresh context" in text, path
            assert "non-suspending `DISCARD`" in text, path

    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "G0 uses only `INFRA_REPAIRED` or `INFRA_BLOCKED`" not in combined
    assert "G0 decides only `INFRA_REPAIRED` or `INFRA_BLOCKED`" not in combined
    assert "final `INFRA_BLOCKED`" not in combined


def test_runtime_docs_require_absolute_artifact_handoff_paths() -> None:
    protocol = AUTORESEARCH.read_text(encoding="utf-8")
    normalized = " ".join(protocol.split())

    assert "artifact=/home/dev/.openclaw/workspace-autoresearch-pm/<artifact-name>.json" in protocol
    assert 'jq -e . "$artifact"' in protocol
    assert 'wc -c "$artifact"' in protocol
    assert 'autoresearch-submit-stage "$state" "$artifact"' in protocol
    assert "absolute-path handoff template" in normalized


def test_alpha_docs_require_dynamic_coverage_and_strict_state_initialization() -> None:
    alpha_docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (AGENT_CONFIG / "AGENTS.md", AUTORESEARCH, METHODOLOGY, PLAN)
    )

    assert "DynamicUniverseCoverageReceipt" in alpha_docs
    assert "autoresearch-migrate-state" not in alpha_docs
    normalized = " ".join(alpha_docs.split()).lower()
    assert "schema-v2 state" in normalized
    assert "archive" in normalized
    assert "before restarting the supervisor" in normalized
    assert "ALPHA_RESEARCH" in alpha_docs
    assert "AggregateCoverageReceipt" in alpha_docs
    assert "DATA_INFRA_G0`-only" in alpha_docs
    assert "per-symbol and aggregate common-calendar coverage" not in alpha_docs


def test_runtime_docs_require_typed_quantipy_gate_and_no_run_receipt() -> None:
    runtime_docs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            AGENT_CONFIG / "AGENTS.md",
            AGENT_CONFIG / "README.md",
            AUTORESEARCH,
            AGENT_CONFIG / "skills" / "codex-subagents" / "SKILL.md",
            PLAN,
        )
    )
    normalized = " ".join(runtime_docs.split())

    assert "fixed private" in normalized
    assert "quantipy_execution_not_started" in normalized
    assert "focused_tests_failed" in normalized
    assert "preflight_failed" not in normalized
    assert "expected `run.json` must be absent" in normalized
    assert "private identity-bound tombstone" in normalized
    assert "new deterministic commit-bound run id" in normalized.lower()
    assert "manifest parent" in normalized.lower()
    assert "8 MiB" in normalized
    assert "1 MiB per source file" in normalized
    assert "8 MiB for the notebook" in normalized
    assert "uv --directory /home/dev/repos/quantipy run --frozen --no-sync" in normalized
    assert "uv --directory <canonical-runtime-root> run --frozen --no-sync" in normalized
    assert "PYTHONDONTWRITEBYTECODE=1 quantipy experiment" not in normalized
    assert "scripts/run-long-task.sh" in normalized
    assert "expected_artifact_path" in normalized
    assert "direct foreground" in normalized.lower()
    assert "verifier" in normalized.lower() and "attestation" in normalized.lower()
    assert "detached run directory" in normalized.lower()
    assert "worker attestation" in normalized.lower()
    assert "hash" in normalized.lower() and "not sufficient by itself" in normalized.lower()
    assert "schema-v5 `status.json`" in normalized
    assert "mode 0400" in normalized
    assert "mode 0500" in normalized
    assert "bounded 64 KiB retained log tail" in normalized
    assert "truthful truncation metadata" in normalized
    assert "malicious same-UID process" in normalized
    assert "outside the local control-plane threat model" in normalized
    assert "chmod 0700 <exact-detached-run-dir>" in normalized
    assert "exit 0 iff `run.success=true`" in normalized
    assert "exit 1 iff `run.success=false`" in normalized
    assert "ordinary `process_error`" in normalized
    assert "exit 2+" in normalized
    assert "/home/dev/.openclaw/autoresearch/quantipy-experiment-runs" in normalized
    assert "mode-0700 non-symlink directory" in normalized
    assert "Generated `__pycache__` directories, `*.pyc`" in normalized
    assert "never substitutes" in normalized


def test_runtime_docs_distinguish_process_success_from_research_validity() -> None:
    runtime_paths = (AGENT_CONFIG / "AGENTS.md", AGENT_CONFIG / "README.md", AUTORESEARCH, PLAN)

    for path in runtime_paths:
        text = " ".join(path.read_text(encoding="utf-8").split())

        assert "Process success is not research validity" in text
        assert "successful execution with anomalous or missing alpha evidence is BUG_SIGNAL" in text
        assert "TEST_FAILURE remains invalid after a successful Quantipy run" in text
        assert (
            "TEST_FAILURE/BUG_SIGNAL runtime evidence may use detached failure/exit 1 only"
            not in text
        )


def test_autoresearch_directive_distinguishes_contested_methodology_discards() -> None:
    skill = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split()).lower()

    assert "bug_signal discards are contested-pending-methodology-fix" in skill
    assert "one hardened revisit" in skill
    assert "clean negatives stay permanently burned" in skill


def test_autoresearch_directive_uses_relaxed_horizon_and_activity_rules() -> None:
    raw_skill = AUTORESEARCH.read_text(encoding="utf-8")
    skill = " ".join(raw_skill.split()).lower()
    plan = " ".join(PLAN.read_text(encoding="utf-8").split()).lower()

    assert "version: 8.17.2" in skill
    assert "any holding period from minutes up to a full trading session" in skill
    assert "every position must be flat by the session close" in skill
    assert "overnight carry is forbidden" in skill
    assert "regular-hours 1-minute data" in skill
    assert "fixed set of five liquid etfs" not in skill
    assert "reuse the same five symbols with no member substitution" not in skill
    assert "each iteration's consensus must declare a `universe_plan`" in skill
    assert "screen criteria, ranking criterion, as-of date, and resulting member count" in skill
    assert "selection may never use out-of-sample returns" in skill
    assert "any performance metric" in skill
    assert (
        "at least one trade per day on average aggregated across the pre-registered panel" in skill
    )
    assert "five-etf panel" not in skill
    assert "trades per day per instrument" in skill
    assert "below 1.0 trades/day on average misses the activity requirement" in skill
    assert "trading friction, not signal absence, has been the binding constraint" in skill
    assert (
        "implementation reveals the approved brief requires a data or runtime contract the "
        "platform does not provide" in skill
    )
    assert "submit a `final_decision` with `infra_blocked`" in skill
    assert "`reviewer_verdict=not_run`" in skill
    assert "`memory_write_required=false`" in skill
    assert "`infra_rationale` naming the exact missing contract" in skill
    for bootstrap_name in ("AGENTS.md", "SOUL.md", "README.md"):
        bootstrap = " ".join((AGENT_CONFIG / bootstrap_name).read_text(encoding="utf-8").split())
        assert "gated `ALPHA_RESEARCH` implementation" in bootstrap
        assert "fresh next iteration" in bootstrap
    assert "context, not an instruction to prefer long holds" in skill
    assert (
        "proposals should leave headroom above this floor rather than targeting it exactly" in skill
    )
    assert "scalping" not in skill
    assert "short-holding-period" not in skill
    assert (
        "`materially_new_evidence` naming the horizon change as the substantive difference" in skill
    )
    assert "a trivially rescaled rerun of a burned family is not novel" in skill
    assert "at least 2 trades per day" not in skill
    assert "scalping" not in plan
    assert "short-holding-period" not in plan
    assert "at least 2 trades per day" not in plan


def test_autoresearch_docs_describe_sentiment_tables_and_forbid_ticker_picks() -> None:
    normalized = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split()).lower()
    sentiment = normalized.split("### reddit sentiment tables", 1)[1].split(
        "the implementation worker derives and prewarms", 1
    )[0]

    assert "daily_ticker_summary" in sentiment
    assert "not truncated" in sentiment
    assert "completed analyzed posts" in sentiment
    assert "rank" in sentiment and "ordinal by mention count" in sentiment
    assert "tradinguniverse" in sentiment
    assert "top-n truncated" in sentiment
    assert "default of 20 per day" in sentiment
    assert "absence is informative" in sentiment
    assert "explicit low-attention state" in sentiment
    assert "only a fraction of scraped in-window posts have been analyzed" in sentiment
    assert "pipeline coverage rather than market attention" in sentiment
    assert "conditional-sentiment feature" in sentiment
    assert "mention-volume feature" in sentiment
    assert "one value per ticker per subreddit per date" in sentiment
    assert "cannot time an intraday entry by itself" in sentiment
    assert "measure per-instrument coverage and mention density" in sentiment

    for ticker in ("spy", "qqq", "iwm", "tlt", "tqqq"):
        assert ticker not in sentiment


def test_autoresearch_decision_gate_uses_oos_metric_and_activity_floor() -> None:
    raw_skill = AUTORESEARCH.read_text(encoding="utf-8")
    normalized_skill = " ".join(raw_skill.split()).lower()
    section8 = raw_skill.split("## 8. Decide And Log", 1)[1].split(
        "After a `DATA_INFRA_G0` implementation", 1
    )[0]
    normalized = " ".join(section8.split()).lower()

    assert "`oos_sharpe_net` is the canonical choice" in normalized
    assert "is_walk_forward_sharpe_net" in section8
    assert "never a valid decision metric" in normalized
    assert (
        "average activity below the campaign floor of 1.0 trades/day over the oos window: "
        "discard regardless of sharpe, drawdown, or reviewer verdict" in normalized
    )
    assert "use the reviewer's recommended metric only for" not in normalized_skill
    assert "use is walk-forward sharpe instead" not in normalized_skill


def test_autoresearch_docs_require_feasibility_telemetry_as_reporting_duty() -> None:
    skill = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split())

    assert "encoded_feature_columns" in skill
    assert "calibration_fit_seconds" in skill
    assert "projected_model_seconds" in skill
    assert "These are reporting duties, not admission gates" in skill
    assert "The feasibility stage must not reject on `encoded_feature_columns`," in skill


def test_autoresearch_docs_pin_stage_summary_budget_and_compaction_contract() -> None:
    skill = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split())

    assert "stage summaries are limited to 4096 characters" in skill
    assert "serialized summary length <= 3800 locally" in skill
    assert "top-K by |magnitude|" in skill
    assert "name K" in skill
    assert "~6 significant figures" in skill
    assert "stripped prefixes" in skill
    assert "scalar comparators" in skill
    assert "bulk detail in the run artifact" in skill
    assert (
        "Experiment packages are built ONLY in the persisted experiment workspace, never in the "
        "authoritative quantipy worktree." in skill
    )


def test_autoresearch_docs_describe_gpu_model_classes_and_tiered_timeouts() -> None:
    skill = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split())

    assert "one NVIDIA RTX 3080 with 10 GiB VRAM" in skill
    assert "peak VRAM under 8 GiB" in skill
    assert "Determinism is mandatory and is the hard constraint." in skill
    assert "gradient-boosted trees (`xgboost`, `lightgbm`)" in skill
    assert "neural sequence models (`torch`)" in skill
    assert "torch.use_deterministic_algorithms(True)" in skill
    assert "CUBLAS_WORKSPACE_CONFIG=:4096:8" in skill
    assert (
        "timeout_seconds = min(max(3 * projected_model_seconds + pre_model_seconds, 1800), 43200)"
        in skill
    )
    assert "default 28800 seconds for `gpu`/`mixed`" in skill
    assert (
        "timeout_seconds = min(max(3 * projected_model_seconds + pre_model_seconds, 1800), 21600)"
        in skill
    )
    assert "default 14400 seconds for `cpu`/`none`" in skill


def test_autoresearch_docs_require_visible_pm_acknowledgements_for_child_completions() -> None:
    skill = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split())
    tools = " ".join((AGENT_CONFIG / "TOOLS.md").read_text(encoding="utf-8").split())

    for text in (skill, tools):
        assert "completion-required" in text and "child handoff" in text
        assert "non-empty normal assistant acknowledgement" in text
        assert "while waiting for remaining required children" in text or (
            "even while other required children are still pending" in text
        )
        assert "[TASK:progress]" not in text


def test_autoresearch_docs_forbid_silent_wait_patterns_for_required_children() -> None:
    skill = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split())
    tools = " ".join((AGENT_CONFIG / "TOOLS.md").read_text(encoding="utf-8").split())

    for text in (skill, tools):
        assert "`sessions_yield`, `NO_REPLY`, `ANNOUNCE_SKIP`, or a tool-only turn" in text
        assert "Do not silently wait" in text or "must not use `sessions_yield`" in text
        assert (
            "PM config denies the OpenClaw tools `sessions_spawn`, `sessions_yield`, "
            "`agents_list`, `sessions_list`, and `sessions_history`" in text
        )
        assert "internal PM transcript replies" in text
        assert "Do not use the message tool to send autonomous updates to G2" in text or (
            "Do not substitute the message tool or send an autonomous update to G2" in text
        )
        assert "persist the authoritative artifact" in text
        assert "non-empty completion summary" in text


def test_autoresearch_docs_treat_delivery_failure_as_a_hard_blocker() -> None:
    skill = " ".join(AUTORESEARCH.read_text(encoding="utf-8").split())

    assert "OpenClaw `2026.7.1-2` with `@openclaw/codex` `2026.7.1-1`" in skill
    assert "native Codex `spawn_agent`" in skill
    assert "forbids substituting OpenClaw `sessions_spawn`" in skill
    assert "Treat any attempt to use `sessions_spawn` as a hard infrastructure blocker" in skill
