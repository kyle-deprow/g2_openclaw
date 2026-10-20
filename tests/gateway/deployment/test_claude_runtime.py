"""Tests for the dedicated Claude Code settings renderer and validator."""

from __future__ import annotations

import copy
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from gateway import agent_profiles
from gateway.deployment import claude_runtime as cr
from gateway.research.store import ResearchStore

Mutator = Callable[[dict[str, Any]], object]


def _vars(tmp_path: Path, **extra: str) -> dict[str, str]:
    base = {
        "HOME": str(tmp_path / "home"),
        "RESEARCH_V2_ROOT": str(tmp_path / "state" / "research-v2"),
        "REPO_ROOT": str(tmp_path / "repo"),
        "OPERATOR_ENV_FILE": str(tmp_path / "op.env"),
    }
    base.update(extra)
    return base


def _argv(tmp_path: Path, profile: str = "claude", **extra: str) -> list[str]:
    args = ["install", "--state-dir", str(tmp_path / "state"), "--profile", profile]
    for key, value in _vars(tmp_path, **extra).items():
        args += ["--var", f"{key}={value}"]
    return args


def _install(tmp_path: Path, **extra: str) -> Path:
    assert cr.main(_argv(tmp_path, **extra)) == 0
    return agent_profiles.claude_config_dir(tmp_path / "state") / "settings.json"


def test_install_renders_valid_settings_with_modes(tmp_path: Path) -> None:
    target = _install(tmp_path)
    assert cr.validate_settings_file(target) == []
    expected = cr.resolve_vars(tmp_path / "state", _vars(tmp_path))
    assert cr.validate_settings_file(target, expected_vars=expected) == []
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    text = target.read_text()
    assert "@" not in text
    assert [p.name for p in target.parent.iterdir()] == ["settings.json"]
    sandbox = json.loads(text)["sandbox"]
    worktrees = str(tmp_path / "state" / "research-v2" / "hypothesis-worktrees")
    assert sandbox["filesystem"]["allowWrite"] == [worktrees]
    assert sandbox["excludedCommands"] == [f"{tmp_path}/repo/.venv/bin/gateway-cli research *"]


def test_protected_names_match_the_real_store_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    store = ResearchStore(root)
    try:
        created = {p.name for p in root.iterdir() if p.name.startswith("state.sqlite")}
        assert created
        assert created <= set(cr.STORE_FILENAMES)
        assert cr.STORE_FILENAME == "state.sqlite3"
    finally:
        store.close()
    deny = json.loads(cr.TEMPLATE_PATH.read_text())["sandbox"]["filesystem"]["denyWrite"]
    for name in cr.STORE_FILENAMES:
        assert f"@RESEARCH_V2_ROOT@/{name}" in deny


def test_template_denies_contain_no_wildcards_in_sandbox_lists() -> None:
    sandbox = json.loads(cr.TEMPLATE_PATH.read_text())["sandbox"]["filesystem"]
    for entry in sandbox["allowWrite"] + sandbox["denyWrite"]:
        assert not set(entry) & set("*?[")


def test_install_is_idempotent_and_replaces_atomically(tmp_path: Path) -> None:
    target = _install(tmp_path)
    target.write_text("{}")
    assert _install(tmp_path) == target
    assert cr.validate_settings_file(target) == []


def test_discovers_other_roots_and_workspaces_but_not_owner(tmp_path: Path) -> None:
    state = tmp_path / "state"
    for name in (
        "research-v2",
        "research-v3",
        "workspace",
        "workspace-reviewer",
        "workspace-research-orchestrator",
    ):
        (state / name).mkdir(parents=True)
    data = json.loads(_install(tmp_path).read_text())
    deny = data["sandbox"]["filesystem"]["denyWrite"]
    assert str(state / "research-v3") in deny
    assert str(state / "research-v2") not in deny
    assert str(state / "workspace-reviewer") in deny
    assert str(state / "workspace") in deny  # literal entry, present even when discovered
    assert str(state / "workspace-research-orchestrator") not in deny
    assert f"Edit(/{state}/research-v3/**)" in data["permissions"]["deny"]


EXTRA_EDIT_RULES = (
    "mempalace-readonly-server.py",
    "copilot_headers_preload.py",
    "openclaw.json.last-good",
    "openclaw.json.pre-allow-probe",
    "openclaw.json.bak*",
    "plugins/**",
    "npm/**",
    "skills/**",
    "plugin-skills/**",
)
CHILD_WORKSPACES = (
    "workspace",
    "workspace-research-implementer",
    "workspace-research-runner",
    "workspace-research-reviewer",
)


def test_extra_state_files_have_edit_deny_rules(tmp_path: Path) -> None:
    deny = json.loads(_install(tmp_path).read_text())["permissions"]["deny"]
    state = tmp_path / "state"
    for item in EXTRA_EDIT_RULES:
        assert f"Edit(/{state}/{item})" in deny, item


def test_child_workspaces_are_literal_even_when_absent(tmp_path: Path) -> None:
    data = json.loads(_install(tmp_path).read_text())
    state = tmp_path / "state"
    sandbox_deny = data["sandbox"]["filesystem"]["denyWrite"]
    for name in CHILD_WORKSPACES:
        assert str(state / name) in sandbox_deny
        assert f"Edit(/{state}/{name}/**)" in data["permissions"]["deny"]
        assert sandbox_deny.count(str(state / name)) == 1


def _settings_path(tmp_path: Path) -> Path:
    return agent_profiles.claude_config_dir(tmp_path / "state") / "settings.json"


def test_validate_detects_new_workspace_and_root_drift(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "workspace-research-orchestrator").mkdir(parents=True)
    target = _install(tmp_path)
    assert cr.validate_settings_file(target) == []
    (state / "workspace-newcomer").mkdir()
    problems = cr.validate_settings_file(target)
    assert any("workspace-newcomer" in p and "denyWrite" in p for p in problems)
    assert any("workspace-newcomer" in p and "Edit" in p for p in problems)
    (state / "workspace-newcomer").rmdir()
    (state / "research-v9").mkdir()
    assert any("research-v9" in p for p in cr.validate_settings_file(target))
    (state / "research-v9").rmdir()
    assert cr.validate_settings_file(target) == []


def test_validate_drift_skips_active_and_owner_and_files(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "research-v2").mkdir(parents=True)
    (state / "workspace-research-orchestrator").mkdir()
    (state / "workspace-note").write_text("a file, not a workspace dir")
    assert cr.validate_settings_file(_install(tmp_path)) == []


def test_drift_compares_roots_by_realpath(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "research-v2").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(state / "research-v2")
    target = _install(tmp_path, RESEARCH_V2_ROOT=str(alias))
    deny = json.loads(target.read_text())["sandbox"]["filesystem"]["denyWrite"]
    assert str(state / "research-v2") not in deny  # active root, reached through the alias
    assert cr.validate_settings_file(target) == []
    (state / "research-v3").mkdir()
    os.symlink(state / "research-v3", state / "research-v4")
    problems = cr.validate_settings_file(target)
    assert any("research-v4" in p for p in problems)


def test_explicit_lists_override_discovery(tmp_path: Path) -> None:
    other = tmp_path / "x"
    data = json.loads(_install(tmp_path, OTHER_RESEARCH_ROOTS=f"{other}/a,{other}/b").read_text())
    deny = data["sandbox"]["filesystem"]["denyWrite"]
    assert f"{other}/a" in deny
    assert f"{other}/b" in deny


def test_codex_profile_is_a_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cr.main(_argv(tmp_path, profile="codex")) == 0
    assert "nothing to install" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_unknown_profile_missing_and_unknown_vars(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cr.main(_argv(tmp_path, profile="nope")) == 2
    state = tmp_path / "s"
    base = ["install", "--state-dir", str(state), "--profile", "claude"]
    assert cr.main([*base, "--var", "REPO_ROOT=/r"]) == 2
    assert "missing required" in capsys.readouterr().err
    assert cr.main(_argv(tmp_path, BOGUS="/x")) == 2
    assert "unknown --var" in capsys.readouterr().err
    assert not state.exists()


@pytest.mark.parametrize(
    "bad", ["relative/path", "/", "/a/../b", "/a@b", "/a*b", "/a?b", "/a[b", "/a(b", "/a)b"]
)
def test_bad_path_vars_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(cr.ClaudeRuntimeError):
        cr.resolve_vars(tmp_path, _vars(tmp_path, HOME=bad))


def test_leftover_placeholder_refused(tmp_path: Path) -> None:
    template = tmp_path / "t.json"
    template.write_text('{"a": ["@NOPE@/x"]}')
    with pytest.raises(cr.ClaudeRuntimeError, match="@NOPE@"):
        cr.render_settings({"HOME": "/h"}, template)


def test_repo_template_uses_only_known_vars() -> None:
    text = cr.TEMPLATE_PATH.read_text()
    json.loads(text)
    assert set(cr._PLACEHOLDER.findall(text)) <= {f"@{key}@" for key in cr.KNOWN_VARS}


def _good(tmp_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_install(tmp_path).read_text())
    return data


MUTATIONS: dict[str, Mutator] = {
    "sandbox off": lambda d: d["sandbox"].update(enabled=False),
    "unsandboxed retry": lambda d: d["sandbox"].update(allowUnsandboxedCommands=True),
    "no failIfUnavailable": lambda d: d["sandbox"].update(failIfUnavailable=False),
    "empty allowWrite": lambda d: d["sandbox"]["filesystem"].update(allowWrite=[]),
    "allowWrite /": lambda d: d["sandbox"]["filesystem"].update(allowWrite=["/"]),
    "allowWrite /home": lambda d: d["sandbox"]["filesystem"].update(allowWrite=["/home"]),
    "allowWrite home": lambda d: d["sandbox"]["filesystem"].update(
        allowWrite=[os.path.expanduser("~")]
    ),
    "allowWrite wildcard": lambda d: d["sandbox"]["filesystem"]["allowWrite"].append("/a/b/*"),
    "denyWrite wildcard": lambda d: d["sandbox"]["filesystem"]["denyWrite"].append("/a/b?"),
    "denyWrite empty": lambda d: d["sandbox"]["filesystem"].update(denyWrite=[]),
    "denyWrite lacks store": lambda d: d["sandbox"]["filesystem"].update(
        denyWrite=[e for e in d["sandbox"]["filesystem"]["denyWrite"] if "sqlite3-wal" not in e]
    ),
    "unix sockets": lambda d: d["sandbox"].update(network={"allowAllUnixSockets": True}),
    "weaker nested": lambda d: d["sandbox"].update(enableWeakerNestedSandbox=True),
    "weaker network": lambda d: d["sandbox"].update(enableWeakerNetworkIsolation=True),
    "filesystem disabled": lambda d: d["sandbox"]["filesystem"].update(disabled=True),
    "extra excluded": lambda d: d["sandbox"]["excludedCommands"].append("git *"),
    "no excluded": lambda d: d["sandbox"].update(excludedCommands=[]),
    "excluded exact": lambda d: d["sandbox"].update(
        excludedCommands=["/r/.venv/bin/gateway-cli research"]
    ),
    "excluded relative": lambda d: d["sandbox"].update(excludedCommands=["gateway-cli research *"]),
    "hooks": lambda d: d.update(disableAllHooks=False),
    "memory": lambda d: d.update(autoMemoryEnabled=True),
    "plugin": lambda d: d.update(enabledPlugins={"x@y": True}),
    "plugins list": lambda d: d.update(enabledPlugins=[]),
    "mcp": lambda d: d.update(mcpServers={"a": {}}),
    "mcp approve": lambda d: d.update(enableAllProjectMcpServers=True),
    "bypass": lambda d: d["permissions"].update(defaultMode="bypassPermissions"),
    "additional dirs": lambda d: d["permissions"].update(additionalDirectories=["/x"]),
    "deny cleared": lambda d: d["permissions"]["deny"].clear(),
    "deny wrong type": lambda d: d["permissions"].update(deny="x"),
    "write rule": lambda d: d["permissions"]["deny"].append("Write(//x/**)"),
    "no ssh deny": lambda d: d["permissions"].update(
        deny=[r for r in d["permissions"]["deny"] if ".ssh" not in r]
    ),
    "placeholder": lambda d: d["sandbox"]["filesystem"]["allowWrite"].append("@HOME@"),
    "sandbox wrong type": lambda d: d.update(sandbox=[]),
    "permissions wrong type": lambda d: d.update(permissions=[]),
    "filesystem wrong type": lambda d: d["sandbox"].update(filesystem="x"),
    "network wrong type": lambda d: d["sandbox"].update(network=[]),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_validator_flags_regression(tmp_path: Path, name: str) -> None:
    data = _good(tmp_path)
    assert cr.validate_settings_data(data) == []
    mutated = copy.deepcopy(data)
    MUTATIONS[name](mutated)
    assert cr.validate_settings_data(mutated)


def test_validator_non_object_and_expected_values(tmp_path: Path) -> None:
    assert cr.validate_settings_data([]) == ["settings must be a JSON object"]
    target = _install(tmp_path)
    other = cr.resolve_vars(tmp_path / "state", _vars(tmp_path, REPO_ROOT=str(tmp_path / "other")))
    problems = cr.validate_settings_file(target, expected_vars=other)
    assert any("differ" in p for p in problems)


def test_validate_file_errors_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "cannot read" in cr.validate_settings_file(tmp_path / "missing.json")[0]
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert "not valid JSON" in cr.validate_settings_file(bad)[0]
    assert cr.main(["validate", "--settings", str(bad)]) == 1
    assert "not valid JSON" in capsys.readouterr().err
    good = _install(tmp_path)
    assert cr.main(["validate", "--settings", str(good)]) == 0
    expect = ["validate", "--settings", str(good), "--state-dir", str(tmp_path / "state")]
    for key, value in _vars(tmp_path).items():
        expect += ["--var", f"{key}={value}"]
    assert cr.main(expect) == 0


def test_install_refuses_symlink_and_regular_file_config_dir(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config_dir = agent_profiles.claude_config_dir(state)
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", config_dir)
    with pytest.raises(cr.ClaudeRuntimeError, match="symlink"):
        cr.install(state, "claude", _vars(tmp_path))
    config_dir.unlink()
    config_dir.write_text("file")
    with pytest.raises(cr.ClaudeRuntimeError, match="not a directory"):
        cr.install(state, "claude", _vars(tmp_path))


def test_install_creates_parents_private(tmp_path: Path) -> None:
    state = tmp_path / "a" / "b" / "state"
    assert cr.install(state, "claude", _vars(tmp_path)) is not None
    for directory in (state, tmp_path / "a" / "b", agent_profiles.claude_config_dir(state)):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_dropin_matches_config_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert cr.check_dropin(home / ".openclaw", home=home) == []
    assert cr.check_dropin(tmp_path / "other", home=home)
    assert cr.DROPIN_PATH.read_text() == (
        "[Service]\nEnvironment=CLAUDE_CONFIG_DIR=%h/.openclaw/claude-code\n"
    )
