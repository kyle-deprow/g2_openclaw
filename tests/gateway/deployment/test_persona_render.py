from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

import pytest
from gateway.agent_profiles import load_profile
from gateway.deployment import persona_render
from gateway.deployment.persona_render import RenderError, main, render_tree

AP5_CONFIG = Path("/home/dev/repos/g2_openclaw-ap5/gateway/agent_config")
VARS = {
    "RESEARCH_V2_ROOT": "/srv/research",
    "HYPOTHESIS_WORKTREES_ROOT": "/srv/worktrees",
    "GATEWAY_CLI": "/repo/.venv/bin/gateway-cli",
    "REPO_ROOT": "/repo",
}
VAR_ARGS = [f"--var={k}={v}" for k, v in VARS.items()]


@pytest.fixture
def src(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "runtime").mkdir(parents=True)
    (root / "TOOLS.md").write_text(
        "profile @AGENT_PROFILE@ runtime @AGENT_RUNTIME@\n"
        "owner @OWNER_MODEL@ @OWNER_EFFORT@; impl @IMPLEMENTER_MODEL@ @IMPLEMENTER_EFFORT@\n"
        "runner @RUNNER_MODEL@ @RUNNER_EFFORT@; rev @REVIEWER_MODEL@ @REVIEWER_EFFORT@\n"
        "root @RESEARCH_V2_ROOT@ wt @HYPOTHESIS_WORKTREES_ROOT@\n@RUNTIME_SECTION@\n",
        encoding="utf-8",
    )
    (root / "SOUL.md").write_text("plain\n", encoding="utf-8")
    (root / "notes.txt").write_text("@BOGUS@\n", encoding="utf-8")
    (root / "runtime" / "codex.md").write_text("codex: @GATEWAY_CLI@ research\n", encoding="utf-8")
    (root / "runtime" / "claude-cli.md").write_text("claude: @GATEWAY_CLI@ x\n", encoding="utf-8")
    return root


def test_renders_codex(src: Path, tmp_path: Path) -> None:
    dst = tmp_path / "dst"
    written = render_tree(load_profile("codex"), src, dst, VARS)
    assert sorted(p.name for p in written) == ["SOUL.md", "TOOLS.md"]
    text = (dst / "TOOLS.md").read_text(encoding="utf-8")
    assert "profile codex runtime codex" in text
    assert "openai/gpt-6-astra high" in text
    assert "openai/gpt-5.6-sol xhigh" in text
    assert "codex: /repo/.venv/bin/gateway-cli research" in text
    assert "root /srv/research wt /srv/worktrees" in text
    assert stat.S_IMODE((dst / "TOOLS.md").stat().st_mode) == 0o644
    assert not (dst / "notes.txt").exists()
    assert not (dst / "runtime").exists()


def test_renders_claude(src: Path, tmp_path: Path) -> None:
    dst = tmp_path / "dst"
    render_tree(load_profile("claude"), src, dst, VARS)
    text = (dst / "TOOLS.md").read_text(encoding="utf-8")
    assert "anthropic/claude-opus-5-5 high" in text
    assert "claude: /repo/.venv/bin/gateway-cli x" in text


def test_leftover_token_names_file_and_token(src: Path, tmp_path: Path) -> None:
    (src / "SOUL.md").write_text("hi @NOPE_X@\n", encoding="utf-8")
    with pytest.raises(RenderError, match=r"SOUL\.md.*@NOPE_X@"):
        render_tree(load_profile("codex"), src, tmp_path / "dst", VARS)
    assert not (tmp_path / "dst").exists()


def test_missing_var_is_leftover(src: Path, tmp_path: Path) -> None:
    partial = {k: v for k, v in VARS.items() if k != "GATEWAY_CLI"}
    with pytest.raises(RenderError, match="@GATEWAY_CLI@"):
        render_tree(load_profile("codex"), src, tmp_path / "dst", partial)


def test_runtime_section_without_fragment(src: Path, tmp_path: Path) -> None:
    (src / "runtime" / "codex.md").unlink()
    with pytest.raises(RenderError, match="RUNTIME_SECTION"):
        render_tree(load_profile("codex"), src, tmp_path / "dst", VARS)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NOPE", "/x"),
        ("REPO_ROOT", "relative"),
        ("REPO_ROOT", "/a\nb"),
        ("REPO_ROOT", "/a@b"),
    ],
)
def test_bad_variables(src: Path, tmp_path: Path, key: str, value: str) -> None:
    with pytest.raises(RenderError):
        render_tree(load_profile("codex"), src, tmp_path / "dst", {**VARS, key: value})


def test_files_selection(src: Path, tmp_path: Path) -> None:
    dst = tmp_path / "dst"
    written = render_tree(load_profile("codex"), src, dst, VARS, files=["SOUL.md"])
    assert [p.name for p in written] == ["SOUL.md"]
    with pytest.raises(RenderError):
        render_tree(load_profile("codex"), src, dst, VARS, files=["../x.md"])
    with pytest.raises(RenderError):
        render_tree(load_profile("codex"), src, dst, VARS, files=["missing.md"])


def test_refuses_symlink_target(src: Path, tmp_path: Path) -> None:
    dst = tmp_path / "dst"
    dst.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    os.symlink(victim, dst / "SOUL.md")
    with pytest.raises(RenderError, match="symlink"):
        render_tree(load_profile("codex"), src, dst, VARS)
    assert victim.read_text(encoding="utf-8") == "keep"


def test_cli_success_and_errors(
    src: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dst = tmp_path / "dst"
    args = ["render", "--profile", "claude", "--src", str(src), "--dst", str(dst), *VAR_ARGS]
    assert main(args) == 0
    assert (dst / "SOUL.md").exists()
    assert main([*args, "--var=BOGUS=/x"]) == 2
    assert main([*args[:2], "nonesuch", *args[3:]]) == 2
    with pytest.raises(SystemExit) as exc:
        main(["render"])
    assert exc.value.code == 64
    assert "ERROR" in capsys.readouterr().err


def test_cli_files_flag(src: Path, tmp_path: Path) -> None:
    dst = tmp_path / "dst"
    args = ["render", "--profile", "codex", "--src", str(src), "--dst", str(dst), *VAR_ARGS]
    assert main([*args, "--files", "SOUL.md"]) == 0
    assert [p.name for p in dst.iterdir()] == ["SOUL.md"]


@pytest.mark.parametrize("profile_name", ["codex", "claude"])
@pytest.mark.skipif(not AP5_CONFIG.is_dir(), reason="AP5 worktree sources not present")
def test_ap5_sources_render_fully(profile_name: str, tmp_path: Path) -> None:
    work = tmp_path / "config"
    shutil.copytree(AP5_CONFIG, work)
    profile = load_profile(profile_name)
    count = 0
    for directory in [work, *sorted(p for p in work.iterdir() if p.is_dir())]:
        if not any(p.suffix == ".md" for p in directory.iterdir() if p.is_file()):
            continue
        out = tmp_path / "out" / directory.relative_to(work)
        written = render_tree(profile, directory, out, VARS)
        for path in written:
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"@[A-Z][A-Z0-9_]*@", text), path
            count += 1
    assert count > 5
    tools = (tmp_path / "out" / "research-orchestrator" / "TOOLS.md").read_text(encoding="utf-8")
    assert "@RUNTIME_SECTION@" not in tools
    assert "/repo/.venv/bin/gateway-cli" in tools


def test_missing_digit_variable_is_error(src: Path, tmp_path: Path) -> None:
    partial = {k: v for k, v in VARS.items() if k != "RESEARCH_V2_ROOT"}
    with pytest.raises(RenderError, match="@RESEARCH_V2_ROOT@"):
        render_tree(load_profile("codex"), src, tmp_path / "dst", partial)


def test_values_are_not_reexpanded(src: Path, tmp_path: Path) -> None:
    # '@' is rejected in variable values, so exercise the single-pass rule via render_text.
    out = persona_render.render_text(
        "a @X@ b @Y@",
        {"X": "@Y@", "Y": "done"},
        runtime_fragment=None,
        name="t",
    )
    assert out == "a @Y@ b done"


def test_fragment_tokens_with_digits_resolved(src: Path, tmp_path: Path) -> None:
    (src / "runtime" / "codex.md").write_text("root @RESEARCH_V2_ROOT@\n", encoding="utf-8")
    dst = tmp_path / "dst"
    render_tree(load_profile("codex"), src, dst, VARS)
    assert "root /srv/research" in (dst / "TOOLS.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("files", ["", ",", "SOUL.md,"])
def test_cli_empty_files_is_usage_error(src: Path, tmp_path: Path, files: str) -> None:
    args = ["render", "--profile", "codex", "--src", str(src), "--dst", str(tmp_path / "d")]
    with pytest.raises(SystemExit) as exc:
        main([*args, *VAR_ARGS, "--files", files])
    assert exc.value.code == 64
