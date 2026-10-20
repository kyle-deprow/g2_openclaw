"""Owner wake prompt text is rendered from the active agent profile."""

from __future__ import annotations

from pathlib import Path

import pytest
from gateway.agent_profiles import load_profile, write_deployed_profile
from gateway.research.contracts import HypothesisSpec
from gateway.research.store import ResearchStore
from gateway.research.wake import compose_wake

from tests.gateway.research.conftest import freeze_with_probe
from tests.gateway.research.native_review_fixtures import _setup as fixture_setup

CLAUDE = load_profile("claude")
CODEX = load_profile("codex")


@pytest.fixture
def claude_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "claude-state"
    (state / "state").mkdir(parents=True)
    write_deployed_profile(CLAUDE, state)
    monkeypatch.setenv("RESEARCH_CORE_DATABASE", str(state / "state" / "openclaw.sqlite"))
    return state


def _message(campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path) -> str:
    store, _source, _hypothesis, _attempt_id, _bundle = fixture_setup(campaign, tmp_path)
    plan = compose_wake(store)
    assert plan is not None
    return plan.message


def test_claude_implemented_wake_spawns_the_profile_reviewer(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path, claude_state: Path
) -> None:
    message = _message(campaign, tmp_path)
    reviewer = CLAUDE.role("reviewer")
    assert "mcp__openclaw__sessions_spawn" in message
    assert "agentId=research-reviewer" in message
    assert f"model={reviewer.model_ref}" in message
    assert f"thinking={reviewer.effort}" in message
    assert "sessions_yield" in message
    assert f"--openclaw-state-dir {claude_state}" in message
    assert "--codex-state-database" not in message
    assert "spawn_agent" not in message
    assert "gpt-5.6" not in message
    # The reviewer's spawn arguments are passed back verbatim, never retyped or extended.
    assert "returned `spawn_arguments` object verbatim" in message
    assert "agentId, model, thinking, cwd, taskName, task; do not edit or add keys" in message
    assert "mcp__openclaw__sessions_yield" in message
    assert "unique taskName" not in message


def test_claude_implemented_wake_without_core_database_is_unavailable(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claude_state: Path,
) -> None:
    monkeypatch.delenv("RESEARCH_CORE_DATABASE")
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(claude_state))
    message = _message(campaign, tmp_path)
    assert "is unavailable" in message
    assert "do not invoke sessions_spawn" in message
    assert "--openclaw-state-dir" not in message


def test_claude_opened_wake_dispatches_the_implementer(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path, claude_state: Path
) -> None:
    store, source, hypothesis = campaign
    freeze_with_probe(store, hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    plan = compose_wake(store)
    assert plan is not None
    assert plan.state == "OPENED"
    implementer = CLAUDE.role("implementer")
    assert "agentId=research-implementer" in plan.message
    assert f"model={implementer.model_ref}" in plan.message
    assert f"thinking={implementer.effort}" in plan.message
    assert f"cwd={attempt.worktree_path}" in plan.message
    assert "sessions_yield" in plan.message


def test_claude_runner_spawn_text_uses_the_profile_runner() -> None:
    from gateway.research import wake

    text = wake._claude_spawn_text(CLAUDE, "runner", "research-runner", "/wt")
    assert "agentId=research-runner" in text
    assert f"model={CLAUDE.role('runner').model_ref}" in text
    assert f"thinking={CLAUDE.role('runner').effort}" in text


def test_codex_wake_text_is_unchanged(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "no-deployed-profile"))
    message = _message(campaign, tmp_path)
    reviewer = CODEX.role("reviewer")
    assert f"model={reviewer.model_id} effort={reviewer.effort}" in message
    assert "spawn_agent" in message
    assert "sessions_spawn" not in message
