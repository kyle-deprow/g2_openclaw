#!/usr/bin/env python3
"""Fail-closed launch profile for the read-only Claude research reviewer.

The SDK owns the stream-json protocol.  This launcher only validates the SDK's
argv, applies the fixed reviewer policy, filters the inherited environment,
and replaces itself with the installed Claude CLI.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from json import JSONDecodeError
from typing import Final, cast

CLI_PATH = "/home/dev/.local/bin/claude"
MODEL = "claude-opus-5"
EFFORT = "high"
TOOLS = "Read,Glob,Grep"

EXIT_USAGE = 64
EXIT_MODEL = 65
EXIT_FORBIDDEN = 66
EXIT_CLI = 67

_EMPTY_MCP_CONFIG: Final = '{"mcpServers":{}}'
_CANONICAL_PERMISSION_MODE: Final = "dontAsk"
_ALLOWED_TOOL_NAMES: Final = frozenset(("Read", "Glob", "Grep"))
_SETTING_SOURCES: Final = frozenset(("user", "project", "local"))

_PASSTHROUGH_FLAGS: Final = frozenset(
    (
        "--print",
        "--input-format",
        "--output-format",
        "--verbose",
        "--permission-prompt-tool",
        "--include-partial-messages",
        "--replay-user-messages",
        "--max-turns",
        "--session-id",
        "--resume",
        "--continue",
        "--fork-session",
        "--no-session-persistence",
        "--append-system-prompt",
        "--system-prompt",
        "--max-budget-usd",
        "--permission-prompts",
    )
)
_PASSTHROUGH_VALUE_FLAGS: Final = frozenset(
    (
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
    )
)
_CONTROLLED_VALUE_FLAGS: Final = frozenset(
    (
        "--model",
        "--effort",
        "--tools",
        "--allowedTools",
        "--allowed-tools",
        "--setting-sources",
        "--permission-mode",
        "--mcp-config",
    )
)
_CONTROLLED_BOOLEAN_FLAGS: Final = frozenset(
    (
        "--strict-mcp-config",
        "--safe-mode",
        "--restricted",
        "--disable-slash-commands",
    )
)
_ALLOW_CAPABILITY_FLAG: Final = "--allow-dangerously-skip-permissions"
_FORBIDDEN_FLAGS: Final = frozenset(
    (
        "--dangerously-skip-permissions",
        "--settings",
        "--add-dir",
        "--agents",
        "--agent",
        "--mcp-debug",
        "--debug",
        "--debug-file",
    )
)
_DISALLOWED_TOOL_FLAGS: Final = frozenset(("--disallowedTools", "--disallowed-tools"))
_UNKNOWN_OPTION: Final = "<unknown-option>"

_ENV_PREFIXES_TO_DROP: Final = ("ANTHROPIC_",)
_ENV_KEYS_TO_DROP: Final = frozenset(
    (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_EXECUTABLE",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_SAFE_MODE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "BASH_ENV",
        "ENV",
    )
)


class _ProfileError(Exception):
    """An expected fail-closed validation error."""

    def __init__(self, code: int, flag: str) -> None:
        self.code = code
        self.flag = flag
        super().__init__(flag)


def _error(code: int, flag: str) -> _ProfileError:
    return _ProfileError(code, flag)


def _split_option(token: str) -> tuple[str, str | None, bool]:
    """Return option name, inline value, and whether the value was inline."""

    if token.startswith("--") and "=" in token:
        name, value = token.split("=", 1)
        return name, value, True
    return token, None, False


def _validate_tools(value: str, flag: str) -> None:
    if value == "default" or value == "":
        return
    parts = [part.strip() for part in value.split(",")]
    if any(not part for part in parts):
        raise _error(EXIT_FORBIDDEN, flag)
    names = set(parts)
    if not names.issubset(_ALLOWED_TOOL_NAMES):
        raise _error(EXIT_FORBIDDEN, flag)


def _validate_setting_sources(value: str, flag: str) -> None:
    if value == "":
        return
    sources = value.split(",")
    if not sources or any(source.strip() not in _SETTING_SOURCES for source in sources):
        raise _error(EXIT_FORBIDDEN, flag)


def _validate_mcp_config(value: str, flag: str) -> None:
    try:
        parsed: object = cast(object, json.loads(value))
    except (JSONDecodeError, TypeError):
        raise _error(EXIT_FORBIDDEN, flag) from None

    if not isinstance(parsed, dict) or set(parsed) != {"mcpServers"}:
        raise _error(EXIT_FORBIDDEN, flag)
    servers = parsed.get("mcpServers")
    if not isinstance(servers, dict) or servers:
        raise _error(EXIT_FORBIDDEN, flag)


def _validate_passthrough_value(flag: str, value: str) -> None:
    if value.startswith("-"):
        raise _error(EXIT_USAGE, flag)
    if flag in {"--input-format", "--output-format"} and value != "stream-json":
        raise _error(EXIT_FORBIDDEN, flag)
    if flag == "--permission-prompt-tool" and value != "stdio":
        raise _error(EXIT_FORBIDDEN, flag)
    if flag == "--permission-prompts" and value not in {"host", "none"}:
        raise _error(EXIT_FORBIDDEN, flag)


def _canonical_args() -> list[str]:
    return [
        "--model",
        MODEL,
        "--effort",
        EFFORT,
        "--tools",
        TOOLS,
        "--allowedTools",
        TOOLS,
        "--permission-mode",
        _CANONICAL_PERMISSION_MODE,
        "--setting-sources=",
        "--strict-mcp-config",
        "--mcp-config",
        _EMPTY_MCP_CONFIG,
        "--safe-mode",
        "--restricted",
        "--disable-slash-commands",
    ]


def _normalize(argv: Sequence[str]) -> tuple[list[str], tuple[str, ...]]:
    """Validate SDK arguments and return normalized args plus stripped flags."""

    forwarded: list[str] = []
    index = 0
    stripped_flags: list[str] = []

    while index < len(argv):
        token = argv[index]
        index += 1

        if token == "--":
            raise _error(EXIT_USAGE, "--")
        if token == "-p":
            forwarded.append(token)
            continue
        if not token.startswith("-"):
            # Prompts and subcommands are deliberately never accepted here;
            # the ACP SDK sends prompts through stdin as stream-json frames.
            raise _error(EXIT_USAGE, _UNKNOWN_OPTION)
        if not token.startswith("--"):
            raise _error(EXIT_USAGE, _UNKNOWN_OPTION)

        name, inline_value, has_inline_value = _split_option(token)
        if name in _FORBIDDEN_FLAGS:
            raise _error(EXIT_FORBIDDEN, name)
        if "fallback" in name:
            raise _error(EXIT_FORBIDDEN, _UNKNOWN_OPTION)
        if name == _ALLOW_CAPABILITY_FLAG:
            if has_inline_value:
                raise _error(EXIT_USAGE, name)
            stripped_flags.append(name)
            continue
        if name in _CONTROLLED_BOOLEAN_FLAGS:
            if has_inline_value:
                raise _error(EXIT_USAGE, name)
            continue

        needs_value = name in _PASSTHROUGH_VALUE_FLAGS or name in _CONTROLLED_VALUE_FLAGS
        if name in _PASSTHROUGH_FLAGS and not needs_value:
            if has_inline_value:
                raise _error(EXIT_USAGE, name)
            forwarded.append(token)
            continue
        if name in _DISALLOWED_TOOL_FLAGS:
            if has_inline_value:
                value = cast(str, inline_value)
            elif index < len(argv):
                value = argv[index]
                index += 1
            else:
                raise _error(EXIT_USAGE, name)
            if value.startswith("-"):
                raise _error(EXIT_FORBIDDEN, name)
            # Disallowed entries are denials, never executable settings. Keep
            # the SDK's safe denials, including AskUserQuestion.
            forwarded.extend(("--disallowedTools", value))
            continue
        if not needs_value:
            raise _error(EXIT_USAGE, _UNKNOWN_OPTION)

        if has_inline_value:
            value = cast(str, inline_value)
        elif index < len(argv):
            value = argv[index]
            index += 1
        else:
            raise _error(EXIT_USAGE, name)

        if name in _PASSTHROUGH_VALUE_FLAGS:
            _validate_passthrough_value(name, value)
            if has_inline_value:
                forwarded.append(token)
            else:
                forwarded.extend((name, value))
            continue
        if name == "--model":
            if value != MODEL:
                raise _error(EXIT_MODEL, name)
            continue
        if name == "--effort":
            if value != EFFORT:
                raise _error(EXIT_MODEL, name)
            continue
        if name in {"--tools", "--allowedTools", "--allowed-tools"}:
            _validate_tools(value, name)
            continue
        if name == "--setting-sources":
            _validate_setting_sources(value, name)
            continue
        if name == "--permission-mode":
            if value not in {"default", "manual", "dontAsk", "plan"}:
                raise _error(EXIT_FORBIDDEN, name)
            continue
        if name == "--mcp-config":
            _validate_mcp_config(value, name)
            continue

        # This is unreachable while the option tables remain exhaustive.
        raise _error(EXIT_USAGE, _UNKNOWN_OPTION)

    result = forwarded + _canonical_args()
    return result, tuple(dict.fromkeys(stripped_flags))


def normalize(argv: Sequence[str]) -> list[str]:
    """Validate SDK arguments and return the fixed reviewer argument vector."""

    normalized, _ = _normalize(argv)
    return normalized


def filter_env(env: Mapping[str, str]) -> dict[str, str]:
    """Remove alternate-provider, executable-injection, and unsafe env keys."""

    return {
        key: value
        for key, value in env.items()
        if not key.startswith(_ENV_PREFIXES_TO_DROP) and key not in _ENV_KEYS_TO_DROP
    }


def _write_error(error: _ProfileError) -> None:
    sys.stderr.write(f"reviewer-cli: error code={error.code} flag={error.flag}\n")


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    execve: Callable[..., object] = os.execve,
) -> int:
    """Exec the pinned CLI, returning a policy/launch code only on failure."""

    actual_argv = tuple(sys.argv[1:] if argv is None else argv)
    try:
        normalized, stripped_flags = _normalize(actual_argv)
        if (
            not os.path.isabs(CLI_PATH)
            or not os.path.isfile(CLI_PATH)
            or not os.access(CLI_PATH, os.X_OK)
        ):
            raise _error(EXIT_CLI, "CLI_PATH")
    except _ProfileError as error:
        _write_error(error)
        return error.code

    filtered_env = filter_env(os.environ if env is None else env)
    # This line intentionally contains only fixed policy values and flag names.
    stripped = ",".join(stripped_flags) if stripped_flags else "none"
    sys.stderr.write(
        "reviewer-cli: exec model=claude-opus-5 effort=high "
        f"tools=Read,Glob,Grep stripped={stripped}\n"
    )
    sys.stderr.flush()
    try:
        execve(CLI_PATH, [CLI_PATH, *normalized], filtered_env)
    except OSError:
        # The status line has already been flushed and is the launch failure
        # record too; never append a second line that could expose exception
        # details from an injected or raced execve target.
        return EXIT_CLI
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
