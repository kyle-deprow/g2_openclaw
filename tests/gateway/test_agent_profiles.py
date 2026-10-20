"""Tests for the agent provider profile core."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from gateway import agent_profiles as ap
from gateway.agent_profiles import AgentProfileError
from gateway.deployment.config_merge import (
    NATIVE_LUNA_MODEL_REF,
    NATIVE_SOL_MODEL_REF,
    _provider_selection,
    orchestrator_model_primary,
)
from gateway.research.readiness import NATIVE_CAPABILITY_EFFORT, NATIVE_CAPABILITY_MODEL
from gateway.research.review_evidence import REVIEW_EFFORT, REVIEW_MODEL

REPO = Path(__file__).resolve().parents[2]


def _doc() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(ap.DEFAULT_PROFILES_PATH.read_text(encoding="utf-8"))
    return document


def _write(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_repo_file_codex_matches_hardcoded_constants() -> None:
    codex = ap.load_profiles()["codex"]
    assert codex.runtime == "codex" and codex.provider == "openai"
    assert codex.role("implementer").model_ref == NATIVE_LUNA_MODEL_REF
    assert codex.role("runner").model_ref == NATIVE_LUNA_MODEL_REF
    assert codex.role("reviewer").model_ref == NATIVE_SOL_MODEL_REF
    assert codex.role("runner").model_id == NATIVE_CAPABILITY_MODEL
    assert codex.role("runner").effort == NATIVE_CAPABILITY_EFFORT
    assert codex.role("reviewer").model_id == REVIEW_MODEL
    assert codex.role("reviewer").effort == REVIEW_EFFORT
    assert codex.role("owner").model_ref == "openai/gpt-6-astra"
    assert codex.role("main").model_ref == "openai/gpt-5.4"
    assert codex.role("implementer").service_tier == "fast"
    assert codex.role("main").service_tier is None
    assert set(codex.roles) == set(ap.ROLE_NAMES)
    assert ap.load_profiles()["claude"].runtime == "claude-cli"


def test_codex_profile_matches_live_sources() -> None:
    codex = ap.load_profile("codex")
    openclaw = json.loads((REPO / "gateway/openclaw_config/openclaw.json").read_text("utf-8"))
    entries = openclaw["agents"]["entries"]
    assert codex.role("main").model_ref == _provider_selection("codex", "", "")[0]
    assert codex.role("owner").model_ref == orchestrator_model_primary(openclaw)
    assert codex.role("main").effort == entries["main"]["thinkingDefault"]
    assert codex.role("owner").effort == entries["research-orchestrator"]["thinkingDefault"]
    for role, stem in (
        ("implementer", "implementer"),
        ("runner", "experiment_runner"),
        ("reviewer", "reviewer"),
    ):
        cfg = tomllib.loads((REPO / f".codex/agent-configs/{stem}.toml").read_text("utf-8"))
        spec = codex.role(role)
        assert spec.model_ref == f"openai/{cfg['model']}"
        assert spec.effort == cfg["model_reasoning_effort"]
        assert spec.service_tier == cfg["service_tier"]


def test_role_spec_parts_and_unknown_role() -> None:
    spec = ap.load_profile("codex").role("main")
    assert (spec.provider, spec.model_id) == ("openai", "gpt-5.4")
    with pytest.raises(AgentProfileError, match="unknown role"):
        ap.load_profile("codex").role("nope")


def test_sha_and_json_canonical() -> None:
    profile = ap.load_profile("claude")
    assert " " not in profile.to_json()
    assert len(profile.sha256) == 64
    assert profile.sha256 != ap.load_profile("codex").sha256


def _mutate_cases() -> list[tuple[str, Any, str]]:
    def contract(d: dict[str, Any]) -> None:
        d["contract"] = "bad"

    def top_unknown(d: dict[str, Any]) -> None:
        d["extra"] = 1

    def top_missing(d: dict[str, Any]) -> None:
        del d["default_profile"]

    def prof_unknown(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["extra"] = 1

    def prof_missing(d: dict[str, Any]) -> None:
        del d["profiles"]["codex"]["provider"]

    def role_unknown(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["roles"]["main"]["extra"] = 1

    def role_missing_key(d: dict[str, Any]) -> None:
        del d["profiles"]["codex"]["roles"]["main"]["model"]

    def role_missing(d: dict[str, Any]) -> None:
        del d["profiles"]["codex"]["roles"]["runner"]

    def role_extra(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["roles"]["extra"] = {"model": "openai/x"}

    def bad_ref(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["roles"]["main"]["model"] = "gpt-5.4"

    def ws_ref(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["roles"]["main"]["model"] = "openai/gpt 5"

    def empty_part(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["roles"]["main"]["model"] = "openai/"

    def provider_mismatch(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["roles"]["main"]["model"] = "anthropic/claude-x"

    def bad_effort(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["roles"]["main"]["effort"] = "turbo"

    def tier_claude(d: dict[str, Any]) -> None:
        d["profiles"]["claude"]["roles"]["main"]["service_tier"] = "fast"

    def bad_runtime(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["runtime"] = "docker"

    def bad_pair(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["provider"] = "anthropic"

    def bad_default(d: dict[str, Any]) -> None:
        d["default_profile"] = "ghost"

    def newline_ref(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["roles"]["main"]["model"] = "openai/gpt-5.4\n"

    def bad_name(d: dict[str, Any]) -> None:
        d["profiles"]["Bad Name"] = d["profiles"]["codex"]

    def bad_tier(d: dict[str, Any]) -> None:
        d["profiles"]["codex"]["roles"]["runner"]["service_tier"] = "Fast Lane"

    cases = [
        (newline_ref, "provider/model"),
        (bad_name, "name must match"),
        (bad_tier, "service_tier must match"),
        (contract, "contract"),
        (top_unknown, "unknown keys"),
        (top_missing, "missing keys"),
        (prof_unknown, "unknown keys"),
        (prof_missing, "missing keys"),
        (role_unknown, "unknown keys"),
        (role_missing_key, "missing keys"),
        (role_missing, "exactly the roles"),
        (role_extra, "exactly the roles"),
        (bad_ref, "provider/model"),
        (ws_ref, "provider/model"),
        (empty_part, "provider/model"),
        (provider_mismatch, "differs from profile provider"),
        (bad_effort, "effort"),
        (tier_claude, "service_tier is only allowed"),
        (bad_runtime, "runtime"),
        (bad_pair, "not valid for runtime"),
        (bad_default, "default_profile"),
    ]
    return [(fn.__name__, fn, msg) for fn, msg in cases]


@pytest.mark.parametrize(("name", "mutate", "message"), _mutate_cases())
def test_validation_errors(tmp_path: Path, name: str, mutate: Any, message: str) -> None:
    doc = copy.deepcopy(_doc())
    mutate(doc)
    with pytest.raises(AgentProfileError, match=message):
        ap.load_profiles(_write(tmp_path, doc))


def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(AgentProfileError, match="cannot read"):
        ap.load_profiles(tmp_path / "missing.json")


def test_resolution_order(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# c\n\nOTHER=1\nG2_AGENT_PROFILE=claude\n", encoding="utf-8")
    assert ap.resolve_profile_name({"G2_AGENT_PROFILE": "codex"}, env_file) == "codex"
    assert ap.resolve_profile_name({}, env_file) == "claude"
    assert ap.resolve_profile_name({"G2_AGENT_PROFILE": ""}, env_file) == "claude"
    assert ap.resolve_profile_name({}, tmp_path / "absent") == "codex"


@pytest.mark.parametrize(
    "line",
    [
        'G2_AGENT_PROFILE="claude"',
        "G2_AGENT_PROFILE='claude'",
        "export G2_AGENT_PROFILE = claude  ",
    ],
)
def test_env_file_quotes(tmp_path: Path, line: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"# G2_AGENT_PROFILE=codex\n{line}\n", encoding="utf-8")
    assert ap.resolve_profile_name({}, env_file) == "claude"


def test_env_file_inline_comment_is_not_stripped(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("G2_AGENT_PROFILE=claude   # x\n", encoding="utf-8")
    name = ap.resolve_profile_name({}, env_file)
    assert name == "claude   # x"
    with pytest.raises(AgentProfileError, match="unknown profile"):
        ap.load_profile(env={}, env_file=env_file)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('G2_AGENT_PROFILE="claude\n', "unterminated"),
        ("G2_AGENT_PROFILE='claude\n", "unterminated"),
        ("G2_AGENT_PROFILE=claude\nG2_AGENT_PROFILE=codex\n", "duplicate"),
        ("G2_AGENT_PROFILE=claude\r\n", "carriage"),
    ],
)
def test_env_file_errors(tmp_path: Path, text: str, message: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_bytes(text.encode())
    with pytest.raises(AgentProfileError, match=message):
        ap.resolve_profile_name({}, env_file)


def test_env_example_line_is_bare() -> None:
    example = REPO / "gateway/openclaw_config/.env.example"
    assert ap.resolve_profile_name({}, example) == "codex"


def test_load_profile_unknown_name() -> None:
    with pytest.raises(AgentProfileError, match="unknown profile"):
        ap.load_profile("ghost")
    assert (
        ap.load_profile(env={"G2_AGENT_PROFILE": "claude"}, env_file=Path("/nonexistent")).name
        == "claude"
    )


def test_deployed_round_trip_atomic(tmp_path: Path) -> None:
    profile = ap.load_profile("claude")
    path = ap.write_deployed_profile(profile, tmp_path)
    assert path == tmp_path / ap.DEPLOYED_PROFILE_FILENAME
    assert sorted(p.name for p in tmp_path.iterdir()) == [ap.DEPLOYED_PROFILE_FILENAME]
    assert path.stat().st_mode & 0o777 == 0o644
    assert ap.read_deployed_profile(tmp_path) == profile
    assert ap.active_runtime_profile(tmp_path).name == "claude"
    ap.write_deployed_profile(ap.load_profile("codex"), tmp_path)
    assert ap.read_deployed_profile(tmp_path) == ap.load_profile("codex")
    assert len(list(tmp_path.iterdir())) == 1


def test_deployed_tampered_and_malformed(tmp_path: Path) -> None:
    path = ap.write_deployed_profile(ap.load_profile("claude"), tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["definition"]["roles"]["main"]["model"] = "anthropic/other"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(AgentProfileError, match="sha256"):
        ap.read_deployed_profile(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AgentProfileError):
        ap.read_deployed_profile(tmp_path)


def test_missing_deployed_falls_back_to_codex(tmp_path: Path) -> None:
    assert ap.read_deployed_profile(tmp_path) is None
    assert ap.active_runtime_profile(tmp_path).name == "codex"


def test_deployed_path_uses_state_dir_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    assert ap.deployed_profile_path() == tmp_path / ap.DEPLOYED_PROFILE_FILENAME


def _cli(*args: str, env_profile: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "G2_AGENT_PROFILE"}
    if env_profile:
        env["G2_AGENT_PROFILE"] = env_profile
    return subprocess.run(
        [sys.executable, "-m", "gateway.agent_profiles", *args],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        check=False,
    )


def test_cli_show_get_resolve() -> None:
    res = _cli("show", "--profile", "codex")
    assert res.returncode == 0
    assert "reviewer openai/gpt-5.6-sol xhigh fast" in res.stdout.splitlines()
    assert "main openai/gpt-5.4 high -" in res.stdout.splitlines()
    as_json = _cli("show", "--profile", "claude", "--json")
    assert as_json.stdout.strip() == ap.load_profile("claude").to_json()
    assert _cli("get", "owner", "model_id", "--profile", "codex").stdout == "gpt-6-astra\n"
    assert _cli("get", "main", "service_tier", "--profile", "codex").stdout == "\n"
    assert _cli("get", "main", "provider", "--profile", "claude").stdout == "anthropic\n"
    assert _cli("get", "main", "effort", "--profile", "claude").stdout == "medium\n"
    assert _cli("get", "main", "model", "--profile", "claude").stdout == (
        "anthropic/claude-sonnet-5-5\n"
    )
    assert _cli("resolve", env_profile="claude").stdout == "claude\n"


def test_cli_write_deployed_and_active(tmp_path: Path) -> None:
    res = _cli("write-deployed", "--profile", "claude", "--state-dir", str(tmp_path))
    assert res.returncode == 0
    assert res.stdout.strip() == str(tmp_path / ap.DEPLOYED_PROFILE_FILENAME)
    active = _cli("active", "--state-dir", str(tmp_path), "--json")
    assert active.stdout.strip() == ap.load_profile("claude").to_json()
    fresh = _cli("active", "--state-dir", str(tmp_path / "none"))
    assert fresh.returncode == 0 and "openai/gpt-5.6-luna" in fresh.stdout


def test_cli_exit_codes() -> None:
    bad = _cli("show", "--profile", "ghost")
    assert bad.returncode == 2 and "unknown profile" in bad.stderr
    assert _cli("get", "nope", "model", "--profile", "codex").returncode == 2
    assert _cli().returncode == 64
    assert _cli("bogus").returncode == 64
    assert _cli("get", "main", "badfield").returncode == 64


def test_profile_hashable() -> None:
    a, b = ap.load_profile("codex"), ap.load_profile("codex")
    assert a == b and {a: 1}[b] == 1


def _bad_deployed(tmp_path: Path, kind: str) -> None:
    path = ap.write_deployed_profile(ap.load_profile("claude"), tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if kind == "malformed":
        path.write_text("{nope", encoding="utf-8")
        return
    if kind == "sha":
        doc["profile_sha256"] = "0" * 64
    elif kind == "contract":
        doc["contract"] = "wrong"
    elif kind == "name":
        doc["definition"]["name"] = "codex"
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize("kind", ["malformed", "sha", "contract", "name"])
def test_active_runtime_profile_raises_on_bad_deployed(tmp_path: Path, kind: str) -> None:
    _bad_deployed(tmp_path, kind)
    with pytest.raises(AgentProfileError):
        ap.active_runtime_profile(tmp_path)
    res = _cli("active", "--state-dir", str(tmp_path))
    assert res.returncode == 2 and "Traceback" not in res.stderr


def test_dangling_symlink_raises(tmp_path: Path) -> None:
    (tmp_path / ap.DEPLOYED_PROFILE_FILENAME).symlink_to(tmp_path / "nowhere")
    with pytest.raises(AgentProfileError, match="dangling"):
        ap.read_deployed_profile(tmp_path)


def test_unreadable_deployed_path_is_wrapped(tmp_path: Path) -> None:
    (tmp_path / ap.DEPLOYED_PROFILE_FILENAME).mkdir()
    with pytest.raises(AgentProfileError, match="cannot read"):
        ap.read_deployed_profile(tmp_path)


def test_write_failure_is_wrapped(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(AgentProfileError, match="cannot write"):
        ap.write_deployed_profile(ap.load_profile("codex"), blocker / "sub")
    res = _cli("write-deployed", "--profile", "codex", "--state-dir", str(blocker / "sub"))
    assert res.returncode == 2 and "Traceback" not in res.stderr


def test_state_dir_must_be_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AgentProfileError, match="absolute"):
        ap.deployed_profile_path(Path("relative/dir"))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", "rel")
    with pytest.raises(AgentProfileError, match="absolute"):
        ap.deployed_profile_path()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", "~/x")
    assert ap.deployed_profile_path() == Path.home() / "x" / ap.DEPLOYED_PROFILE_FILENAME


def test_cli_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("G2_AGENT_PROFILE=claude\n", encoding="utf-8")
    assert _cli("resolve", "--env-file", str(env_file)).stdout == "claude\n"
    assert _cli("get", "main", "provider", "--env-file", str(env_file)).stdout == "anthropic\n"
    assert "anthropic/" in _cli("show", "--env-file", str(env_file)).stdout


def test_claude_config_dir_is_sibling_of_deployed_profile(tmp_path: Path) -> None:
    assert ap.claude_config_dir(tmp_path) == tmp_path / "claude-code"
    assert ap.claude_config_dir(tmp_path).parent == ap.deployed_profile_path(tmp_path).parent
