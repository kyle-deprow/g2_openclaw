"""White-box tests for the systemd environment decoder."""

from __future__ import annotations

from pathlib import Path

import pytest
from gateway.deployment.systemd_env import (
    SystemdEnvironmentDecodeError,
    _rewrite_environment_line,
    _rewrite_systemd_environment_file,
    decode_systemd_show_environment_node_options,
    decode_systemd_show_environment_word,
)


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        ("", ""),
        ("''", ""),
        ("plain-._~/:@%+=,A0", "plain-._~/:@%+=,A0"),
        ("'two words = stays quoted'", "two words = stays quoted"),
        (r"$'line\nnext\ttab\rreturn'", "line\nnext\ttab\rreturn"),
        (
            r"$'quote=\' double=\" backslash=\\ question=\?'",
            "quote=' double=\" backslash=\\ question=?",
        ),
        (r"$'hex=\x41 octal=\101'", "hex=A octal=A"),
        (r"$'controls=\b\f\v\a escape=\e'", "controls=\b\f\v\a escape=\x1b"),
        (r"$'C-style=\E'", "C-style=\x1b"),
    ],
)
def test_decode_systemd_show_environment_word_matches_bash_tables(
    encoded: str, expected: str
) -> None:
    assert decode_systemd_show_environment_word(encoded) == expected


@pytest.mark.parametrize(
    "encoded",
    [
        r"$'unterminated",
        r"$'trailing\'",
        r"$'unknown=\q'",
        r"$'zero=\0'",
        r"$'zero=\x00'",
        r"$'badhex=\x0G'",
        r"$'hexrun=\x414'",
        r"$'shortoctal=\12'",
        r"$'octal-overflow=\400'",
        r"$'octal-overflow=\777'",
        r"$'a'b",
        "a value with spaces",
        "'embedded'quote'",
    ],
)
def test_decode_systemd_show_environment_word_rejects_ambiguous_tables(encoded: str) -> None:
    with pytest.raises(SystemdEnvironmentDecodeError):
        decode_systemd_show_environment_word(encoded)


@pytest.mark.parametrize(
    ("manager_env", "expected_present", "expected_value"),
    [
        ("OTHER=1\nANOTHER=two", False, ""),
        ("NODE_OPTIONS=", True, ""),
        (
            "OTHER=ignored\nNODE_OPTIONS='--require /tmp/azure-api-version-preload.cjs'\n",
            True,
            "--require /tmp/azure-api-version-preload.cjs",
        ),
        (
            (
                r"NODE_OPTIONS=$'--require\t/tmp/azure-api-version-preload.cjs\n"
                r"quote=\' double=\" backslash=\\'"
            ),
            True,
            "--require\t/tmp/azure-api-version-preload.cjs\nquote=' double=\" backslash=\\",
        ),
    ],
)
def test_decode_systemd_show_environment_node_options_tables(
    manager_env: str, expected_present: bool, expected_value: str
) -> None:
    assert decode_systemd_show_environment_node_options(manager_env) == (
        expected_present,
        expected_value,
    )


def test_decode_systemd_show_environment_node_options_rejects_multiple_assignments() -> None:
    with pytest.raises(
        SystemdEnvironmentDecodeError,
        match="multiple NODE_OPTIONS assignments; refusing ambiguous cleanup",
    ):
        decode_systemd_show_environment_node_options("NODE_OPTIONS=one\nNODE_OPTIONS=two")


@pytest.mark.parametrize(
    ("encoded", "expected_bytes"),
    [
        (r"$'\xC3'", b"\xc3"),
        (r"$'\377'", b"\xff"),
        (r"$'\xc3\xa9'", b"\xc3\xa9"),
    ],
)
def test_decode_systemd_show_environment_word_preserves_raw_high_bytes(
    encoded: str, expected_bytes: bytes
) -> None:
    decoded = decode_systemd_show_environment_word(encoded)
    assert decoded.encode("utf-8", errors="surrogateescape") == expected_bytes


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            'Environment="NODE_OPTIONS=--require /tmp/azure-api-version-preload.cjs"',
            ("", False),
        ),
        (
            'Environment="KEEP=1" "NODE_OPTIONS=--require /tmp/azure-api-version-preload.cjs"',
            ('Environment="KEEP=1"', False),
        ),
        (
            'Environment="NODE_OPTIONS=--require /tmp/azure-api-version-preload.cjs" "KEEP=1"',
            ('Environment="KEEP=1"', False),
        ),
    ],
)
def test_rewrite_environment_line_table_preserves_empty_assignment_deletion(
    line: str, expected: tuple[str, bool]
) -> None:
    assert _rewrite_environment_line(line, "azure-api-version-preload.cjs") == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            '[Service]\nEnvironment="NODE_OPTIONS=--require /tmp/azure-api-version-preload.cjs"\n',
            b"[Service]\n",
        ),
        (
            '[Service]\nEnvironment="NODE_OPTIONS=--require '
            '/tmp/azure-api-version-preload.cjs"\nEnvironment=KEEP=1\n',
            b"[Service]\nEnvironment=KEEP=1\n",
        ),
    ],
)
def test_rewrite_systemd_environment_file_table_deletes_empty_environment_lines(
    tmp_path: Path, content: str, expected: bytes
) -> None:
    path = tmp_path / "gateway.service"
    path.write_text(content, encoding="utf-8")
    assert _rewrite_systemd_environment_file(str(path), "azure-api-version-preload.cjs") == expected
