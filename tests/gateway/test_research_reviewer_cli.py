"""Tests for the preparatory, fail-closed research reviewer launcher."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "research-reviewer-cli.py"


class ReviewerModule(Protocol):
    CLI_PATH: str
    EXIT_CLI: int
    EXIT_FORBIDDEN: int
    EXIT_MODEL: int
    EXIT_USAGE: int
    MODEL: str
    EFFORT: str
    TOOLS: str

    def normalize(self, argv: Sequence[str]) -> list[str]: ...

    def filter_env(self, env: Mapping[str, str]) -> dict[str, str]: ...

    def main(
        self,
        argv: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        execve: object = os.execve,
    ) -> int: ...


class ProfileError(Protocol):
    code: int
    flag: str


@pytest.fixture()
def reviewer() -> ReviewerModule:
    spec = importlib.util.spec_from_file_location("research_reviewer_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(ReviewerModule, module)


def _option_values(argv: Sequence[str], option: str) -> list[str]:
    return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == option]


def _write_executable(path: Path, contents: str = "#!/bin/sh\nexit 0\n") -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def test_defaults_are_single_canonical_policy_values(reviewer: ReviewerModule) -> None:
    normalized = reviewer.normalize([])

    assert _option_values(normalized, "--model") == [reviewer.MODEL]
    assert _option_values(normalized, "--effort") == [reviewer.EFFORT]
    assert _option_values(normalized, "--tools") == [reviewer.TOOLS]
    assert _option_values(normalized, "--allowedTools") == [reviewer.TOOLS]
    assert _option_values(normalized, "--permission-mode") == ["dontAsk"]
    assert normalized.count("--setting-sources=") == 1
    assert normalized.count("--strict-mcp-config") == 1
    assert normalized.count("--safe-mode") == 1
    assert normalized.count("--restricted") == 1
    assert normalized.count("--disable-slash-commands") == 1
    assert _option_values(normalized, "--mcp-config") == ['{"mcpServers":{}}']


def test_passthrough_flags_keep_order_and_both_value_forms(
    reviewer: ReviewerModule,
) -> None:
    incoming = [
        "--print",
        "--input-format=stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-prompt-tool",
        "stdio",
        "--include-partial-messages",
        "--replay-user-messages",
        "--max-turns=3",
        "--session-id",
        "session-1",
        "--resume=session-1",
        "--continue",
        "--fork-session",
        "--no-session-persistence",
        "--append-system-prompt=hello",
        "--system-prompt",
        "review",
        "--max-budget-usd=1.25",
        "--permission-prompts",
        "none",
    ]

    normalized = reviewer.normalize(incoming)

    assert normalized[: len(incoming)] == incoming
    assert normalized[len(incoming) :] == reviewer.normalize([])


@pytest.mark.parametrize(
    "argv",
    [
        ["--replay-user-messages", ""],
        ["--replay-user-messages="],
    ],
)
def test_replay_user_messages_consumes_only_known_sdk_empty_value(
    reviewer: ReviewerModule,
    argv: list[str],
) -> None:
    normalized = reviewer.normalize(argv)

    assert normalized[0] == "--replay-user-messages"
    assert "" not in normalized


def test_replay_user_messages_rejects_nonempty_values(
    reviewer: ReviewerModule,
) -> None:
    for argv in (["--replay-user-messages", "enabled"], ["--replay-user-messages=enabled"]):
        with pytest.raises(Exception) as raised:
            reviewer.normalize(argv)
        error = cast(ProfileError, raised.value)
        assert error.code == reviewer.EXIT_USAGE
        assert error.flag == "--replay-user-messages"


def test_exact_acp_adapter_argv_normalizes_without_empty_positional(
    reviewer: ReviewerModule,
) -> None:
    adapter_argv = [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--tools",
        "default",
        "--disallowedTools",
        "AskUserQuestion",
        "--setting-sources",
        "project,local",
        "--include-partial-messages",
        "--replay-user-messages",
        "",
        "--session-id",
        "123e4567-e89b-12d3-a456-426614174000",
        "--model",
        "claude-opus-5",
    ]

    normalized = reviewer.normalize(adapter_argv)

    assert normalized == [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--disallowedTools",
        "AskUserQuestion",
        "--include-partial-messages",
        "--replay-user-messages",
        "--session-id",
        "123e4567-e89b-12d3-a456-426614174000",
        "--model",
        "claude-opus-5",
        "--effort",
        "high",
        "--tools",
        "Read,Glob,Grep",
        "--allowedTools",
        "Read,Glob,Grep",
        "--permission-mode",
        "dontAsk",
        "--setting-sources=",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--safe-mode",
        "--restricted",
        "--disable-slash-commands",
    ]


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--model", "claude-opus-5"),
        ("--effort", "high"),
        ("--tools", "Read,Glob"),
        ("--allowedTools", "Read,Glob"),
        ("--allowed-tools", "Read,Glob"),
        ("--setting-sources", "user,project,local"),
        ("--permission-mode", "manual"),
        ("--mcp-config", '{"mcpServers":{}}'),
    ],
)
def test_each_controlled_value_form_is_accepted(
    reviewer: ReviewerModule,
    flag: str,
    value: str,
) -> None:
    assert reviewer.normalize([f"{flag}={value}"])
    assert reviewer.normalize([flag, value])


def test_duplicate_controls_collapse_and_allow_capability_is_stripped(
    reviewer: ReviewerModule,
) -> None:
    normalized = reviewer.normalize(
        [
            "--model",
            "claude-opus-5",
            "--model=claude-opus-5",
            "--effort=high",
            "--tools",
            "default",
            "--safe-mode",
            "--safe-mode",
            "--restricted",
            "--restricted",
            "--strict-mcp-config",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--disable-slash-commands",
            "--allow-dangerously-skip-permissions",
        ]
    )

    assert _option_values(normalized, "--model") == [reviewer.MODEL]
    assert _option_values(normalized, "--effort") == [reviewer.EFFORT]
    for flag in (
        "--safe-mode",
        "--restricted",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ):
        assert normalized.count(flag) == 1
    assert "--allow-dangerously-skip-permissions" not in normalized


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["--model", "claude-sonnet-5"], 65),
        (["--effort", "medium"], 65),
        (["--tools", "Read,Bash"], 66),
        (["--allowedTools", "Read,Bash"], 66),
        (["--setting-sources", "user,project,unknown"], 66),
        (["--permission-mode", "bypassPermissions"], 66),
        (["--permission-mode", "acceptEdits"], 66),
        (["--permission-mode", "auto"], 66),
        (["--dangerously-skip-permissions"], 66),
        (["--settings", "{}"], 66),
        (["--mcp-config", '{"mcpServers":{"x":{}}}'], 66),
        (["--mcp-config", "not-json"], 66),
        (["--disallowedTools", "--dangerously-skip-permissions"], 66),
        (["--add-dir", "/tmp"], 66),
        (["--agents", "{}"], 66),
        (["--agent", "reviewer"], 66),
        (["--mcp-debug"], 66),
        (["--debug"], 66),
        (["--debug-file", "/tmp/debug"], 66),
        (["--model-fallback", "sonnet"], 66),
        (["--unknown-fallback-option"], 66),
        (["--foo"], 64),
        (["-d"], 64),
        (["--"], 64),
        (["prompt"], 64),
    ],
)
def test_rejected_argv_is_fail_closed(
    reviewer: ReviewerModule,
    argv: list[str],
    code: int,
) -> None:
    with pytest.raises(Exception) as raised:
        reviewer.normalize(argv)

    error = cast(ProfileError, raised.value)
    assert error.code == code


@pytest.mark.parametrize(
    "argv",
    [
        ["--model", "claude-sonnet-5"],
        ["--effort", "medium"],
        ["--tools", "Read,Bash"],
        ["--permission-mode", "bypassPermissions"],
        ["--dangerously-skip-permissions"],
        ["--settings", "{}"],
        ["--foo"],
        ["-d"],
        ["prompt"],
    ],
)
def test_main_never_executes_for_rejected_argv(
    reviewer: ReviewerModule,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def fake_execve(path: str, values: Sequence[str], env: Mapping[str, str]) -> object:
        nonlocal calls
        calls += 1
        return None

    assert reviewer.main(argv=argv, env={}, execve=fake_execve) in {
        reviewer.EXIT_USAGE,
        reviewer.EXIT_MODEL,
        reviewer.EXIT_FORBIDDEN,
    }
    captured = capsys.readouterr()
    assert calls == 0
    assert captured.out == ""
    assert captured.err.count("\n") == 1


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["--unknown-secret-path"], 64),
        (["-r/secret"], 64),
        (["positional-secret"], 64),
        (["--unknown-fallback-secret"], 66),
    ],
)
def test_unknown_error_lines_use_static_option_marker(
    reviewer: ReviewerModule,
    argv: list[str],
    code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert reviewer.main(argv=argv, env={}, execve=lambda *args: None) == code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (f"reviewer-cli: error code={code} flag=<unknown-option>\n")
    assert all(value not in captured.err for value in argv)


@pytest.mark.parametrize(
    "flag",
    [
        "--input-format",
        "--output-format",
        "--permission-prompt-tool",
        "--max-turns",
        "--session-id",
        "--resume",
        "--append-system-prompt",
        "--system-prompt",
        "--max-budget-usd",
        "--permission-prompts",
        "--model",
        "--effort",
        "--tools",
        "--allowedTools",
        "--setting-sources",
        "--permission-mode",
        "--mcp-config",
        "--disallowedTools",
    ],
)
def test_value_taking_flags_require_values(reviewer: ReviewerModule, flag: str) -> None:
    with pytest.raises(Exception) as raised:
        reviewer.normalize([flag])

    error = cast(ProfileError, raised.value)
    assert error.code == reviewer.EXIT_USAGE
    assert error.flag == flag


@pytest.mark.parametrize(
    "argv",
    [
        ["--resume", "--input-format", "stream-json"],
        ["--resume=--input-format"],
        ["--session-id=-secret-session"],
        ["--input-format", "--output-format"],
        ["--output-format=--input-format"],
        ["--permission-prompt-tool", "--dangerously-skip-permissions"],
        ["--max-turns=-1"],
        ["--session-id", "-session"],
        ["--append-system-prompt=-prompt"],
        ["--system-prompt", "-prompt"],
        ["--max-budget-usd=-1"],
        ["--permission-prompts=-none"],
    ],
)
def test_passthrough_values_cannot_be_option_tokens(
    reviewer: ReviewerModule, argv: list[str]
) -> None:
    with pytest.raises(Exception) as raised:
        reviewer.normalize(argv)

    error = cast(ProfileError, raised.value)
    assert error.code == reviewer.EXIT_USAGE


@pytest.mark.parametrize(
    ("argv", "code", "flag"),
    [
        (["--safe-mode=true"], 64, "--safe-mode"),
        (["--restricted=false"], 64, "--restricted"),
        (["--strict-mcp-config=1"], 64, "--strict-mcp-config"),
        (["--disable-slash-commands=no"], 64, "--disable-slash-commands"),
        (["--allow-dangerously-skip-permissions=yes"], 64, "--allow-dangerously-skip-permissions"),
        (["--settings=/tmp/secret-settings.json"], 66, "--settings"),
    ],
)
def test_inline_boolean_and_forbidden_values_fail_without_echo(
    reviewer: ReviewerModule,
    argv: list[str],
    code: int,
    flag: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert reviewer.main(argv=argv, env={}, execve=lambda *args: None) == code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"reviewer-cli: error code={code} flag={flag}\n"
    assert argv[0] not in captured.err


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--input-format", "text"),
        ("--output-format", "json"),
        ("--permission-prompt-tool", "shell"),
        ("--permission-prompts", "ask"),
    ],
)
def test_transport_values_are_restricted(
    reviewer: ReviewerModule,
    flag: str,
    value: str,
) -> None:
    with pytest.raises(Exception) as raised:
        reviewer.normalize([flag, value])

    error = cast(ProfileError, raised.value)
    assert error.code == reviewer.EXIT_FORBIDDEN
    assert error.flag == flag


def test_sdk_disallowed_tool_denial_is_preserved(reviewer: ReviewerModule) -> None:
    normalized = reviewer.normalize(
        ["--disallowedTools", "AskUserQuestion", "--disallowed-tools=Bash"]
    )

    assert normalized[:4] == [
        "--disallowedTools",
        "AskUserQuestion",
        "--disallowedTools",
        "Bash",
    ]
    assert _option_values(normalized, "--allowedTools") == [reviewer.TOOLS]


def test_environment_filter_drops_provider_and_injection_keys(
    reviewer: ReviewerModule,
) -> None:
    environment = {
        "ANTHROPIC_API_KEY": "secretvalue",  # pragma: allowlist secret (synthetic fixture)
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "CLAUDE_CODE_EXECUTABLE": "/tmp/other",
        "CLAUDE_CONFIG_DIR": "/tmp/config",
        "CLAUDE_CODE_SAFE_MODE": "0",
        "AWS_ACCESS_KEY_ID": "id",
        "AWS_SECRET_ACCESS_KEY": "secret",  # pragma: allowlist secret (synthetic fixture)
        "AWS_SESSION_TOKEN": "token",
        "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/credentials",
        "NODE_OPTIONS": "--require evil",
        "NODE_PATH": "/tmp/modules",
        "PYTHONPATH": "/tmp/python",
        "PYTHONHOME": "/tmp/python-home",
        "LD_PRELOAD": "/tmp/preload.so",
        "LD_LIBRARY_PATH": "/tmp/lib",
        "BASH_ENV": "/tmp/bashrc",
        "ENV": "/tmp/envrc",
        "HOME": "/home/subscriber",
        "PATH": "/usr/bin",
        "USER": "subscriber",
        "UNRELATED": "kept",
    }

    filtered = reviewer.filter_env(environment)

    assert filtered == {
        "HOME": "/home/subscriber",
        "PATH": "/usr/bin",
        "USER": "subscriber",
        "UNRELATED": "kept",
    }


def test_main_execve_receives_pinned_path_argv_and_filtered_env(
    reviewer: ReviewerModule,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = tmp_path / "claude"
    _write_executable(cli)
    reviewer.CLI_PATH = str(cli)
    calls: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_execve(path: str, argv: Sequence[str], env: Mapping[str, str]) -> object:
        calls.append((path, list(argv), dict(env)))
        return None

    code = reviewer.main(
        argv=["--input-format", "stream-json", "--output-format=stream-json"],
        env={
            "HOME": "/home/subscriber",
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "secret",  # pragma: allowlist secret (synthetic fixture)
        },
        execve=fake_execve,
    )

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert captured.err == (
        "reviewer-cli: exec model=claude-opus-5 effort=high tools=Read,Glob,Grep stripped=none\n"
    )
    assert len(calls) == 1
    path, argv, environment = calls[0]
    assert path == str(cli)
    assert argv[0] == str(cli)
    assert "--input-format" in argv and "--output-format=stream-json" in argv
    assert "ANTHROPIC_API_KEY" not in environment
    assert environment["HOME"] == "/home/subscriber"


def test_main_status_names_stripped_capability_without_values(
    reviewer: ReviewerModule,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = tmp_path / "claude"
    _write_executable(cli)
    reviewer.CLI_PATH = str(cli)

    def fake_execve(path: str, argv: Sequence[str], env: Mapping[str, str]) -> object:
        return None

    assert (
        reviewer.main(
            argv=["--allow-dangerously-skip-permissions"],
            env={"HOME": "/home/subscriber"},
            execve=fake_execve,
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "reviewer-cli: exec model=claude-opus-5 effort=high "
        "tools=Read,Glob,Grep stripped=--allow-dangerously-skip-permissions\n"
    )


def test_main_rejects_before_exec_and_does_not_echo_values(
    reviewer: ReviewerModule,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def fake_execve(path: str, argv: Sequence[str], env: Mapping[str, str]) -> object:
        nonlocal calls
        calls += 1
        return None

    assert (
        reviewer.main(
            argv=["--tools", "Read,Bash", "--model", "claude-sonnet-5"],
            env={"ANTHROPIC_API_KEY": "secretvalue"},  # pragma: allowlist secret
            execve=fake_execve,
        )
        == reviewer.EXIT_FORBIDDEN
    )
    captured = capsys.readouterr()
    assert calls == 0
    assert captured.out == ""
    assert captured.err == "reviewer-cli: error code=66 flag=--tools\n"
    assert "secretvalue" not in captured.err
    assert "Read,Bash" not in captured.err


def test_main_reports_missing_cli_without_exec(
    reviewer: ReviewerModule,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reviewer.CLI_PATH = str(tmp_path / "missing-claude")
    calls = 0

    def fake_execve(path: str, argv: Sequence[str], env: Mapping[str, str]) -> object:
        nonlocal calls
        calls += 1
        return None

    assert reviewer.main(argv=[], env={}, execve=fake_execve) == reviewer.EXIT_CLI
    captured = capsys.readouterr()
    assert calls == 0
    assert captured.out == ""
    assert captured.err == "reviewer-cli: error code=67 flag=CLI_PATH\n"


def test_main_execve_failure_returns_cli_code_without_second_stderr_line(
    reviewer: ReviewerModule,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = tmp_path / "claude"
    _write_executable(cli)
    reviewer.CLI_PATH = str(cli)

    def failing_execve(path: str, argv: Sequence[str], env: Mapping[str, str]) -> object:
        raise OSError("details must not be printed")

    assert reviewer.main(argv=[], env={}, execve=failing_execve) == reviewer.EXIT_CLI
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "reviewer-cli: exec model=claude-opus-5 effort=high tools=Read,Glob,Grep stripped=none\n"
    )


def test_subprocess_exec_preserves_binary_stdin_byte_for_byte(tmp_path: Path) -> None:
    cli = tmp_path / "fake-claude"
    expected_argv = [
        "--input-format",
        "stream-json",
        "--output-format=stream-json",
        "--model",
        "claude-opus-5",
        "--effort",
        "high",
        "--tools",
        "Read,Glob,Grep",
        "--allowedTools",
        "Read,Glob,Grep",
        "--permission-mode",
        "dontAsk",
        "--setting-sources=",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--safe-mode",
        "--restricted",
        "--disable-slash-commands",
    ]
    _write_executable(
        cli,
        f"#!{sys.executable}\n"
        "import sys\n"
        f"expected = {expected_argv!r}\n"
        "if sys.argv[1:] != expected:\n"
        "    raise SystemExit(19)\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
    )
    payload = bytes((index * 37) % 256 for index in range(65536)) + b'\x00{"partial"'
    helper = (
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('reviewer', {str(SCRIPT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "assert spec.loader is not None\n"
        "spec.loader.exec_module(module)\n"
        f"module.CLI_PATH = {str(cli)!r}\n"
        "raise SystemExit(module.main(argv=['--input-format', 'stream-json', "
        "'--output-format=stream-json']))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", helper],
        input=payload,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == payload
    assert result.stderr == (
        b"reviewer-cli: exec model=claude-opus-5 effort=high tools=Read,Glob,Grep stripped=none\n"
    )
