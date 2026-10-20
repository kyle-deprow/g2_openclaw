"""Render persona/bootstrap Markdown with agent-profile and deployment placeholders."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from gateway.agent_profiles import AgentProfile, AgentProfileError, load_profile

RUNTIME_TOKEN = "@RUNTIME_SECTION@"
RUNTIME_DIRNAME = "runtime"
VAR_KEYS: tuple[str, ...] = (
    "RESEARCH_V2_ROOT",
    "HYPOTHESIS_WORKTREES_ROOT",
    "GATEWAY_CLI",
    "REPO_ROOT",
)
_ROLE_TOKENS: tuple[tuple[str, str], ...] = (
    ("OWNER", "owner"),
    ("IMPLEMENTER", "implementer"),
    ("RUNNER", "runner"),
    ("REVIEWER", "reviewer"),
)
_TOKEN = re.compile(r"@([A-Z][A-Z0-9_]*)@")
_FILE_MODE = 0o644


class RenderError(ValueError):
    """Raised for render or validation failures (exit code 2)."""


def profile_placeholders(profile: AgentProfile) -> dict[str, str]:
    values = {"AGENT_PROFILE": profile.name, "AGENT_RUNTIME": profile.runtime}
    for token, role in _ROLE_TOKENS:
        spec = profile.role(role)
        values[f"{token}_MODEL"] = spec.model_ref
        values[f"{token}_EFFORT"] = spec.effort or "default"
    return values


def validate_variables(variables: Mapping[str, str]) -> dict[str, str]:
    checked: dict[str, str] = {}
    for key, value in variables.items():
        if key not in VAR_KEYS:
            raise RenderError(f"unknown variable {key!r}; allowed: {list(VAR_KEYS)}")
        if not value.startswith("/"):
            raise RenderError(f"variable {key} must be an absolute path, got {value!r}")
        if "\n" in value or "\r" in value or "@" in value:
            raise RenderError(f"variable {key} must not contain a newline or '@'")
        checked[key] = value
    return checked


def render_text(
    text: str,
    placeholders: Mapping[str, str],
    *,
    runtime_fragment: str | None,
    name: str,
) -> str:
    if RUNTIME_TOKEN in text:
        if runtime_fragment is None:
            raise RenderError(f"{name}: contains {RUNTIME_TOKEN} but no runtime fragment exists")
        text = text.replace(RUNTIME_TOKEN, runtime_fragment)

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in placeholders:
            raise RenderError(f"{name}: unresolved placeholder {match.group(0)}")
        return placeholders[key]

    # One pass: substituted values are never re-expanded.
    return _TOKEN.sub(repl, text)


def _select_files(src: Path, files: Sequence[str] | None) -> list[Path]:
    if files is None:
        return sorted(p for p in src.iterdir() if p.suffix == ".md" and _is_regular(p))
    selected: list[Path] = []
    for name in files:
        if not name or "/" in name or name in (".", "..") or "\x00" in name:
            raise RenderError(f"--files entry {name!r} must be a bare file name")
        path = src / name
        if not _is_regular(path):
            raise RenderError(f"{path}: not a regular file")
        selected.append(path)
    return selected


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(target: Path, content: str) -> None:
    if target.is_symlink():
        raise RenderError(f"{target}: refusing to write through a symlink")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, _FILE_MODE)
        os.replace(tmp_name, target)
        _fsync_dir(target.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def render_tree(
    profile: AgentProfile,
    src: Path,
    dst: Path,
    variables: Mapping[str, str],
    files: Sequence[str] | None = None,
) -> list[Path]:
    """Render ``src`` Markdown files into ``dst``; return the written paths."""
    if not src.is_dir():
        raise RenderError(f"source directory {src} does not exist")
    if dst.is_symlink():
        raise RenderError(f"{dst}: destination directory is a symlink")
    placeholders = {**profile_placeholders(profile), **validate_variables(variables)}
    fragment_path = src / RUNTIME_DIRNAME / f"{profile.runtime}.md"
    fragment: str | None = None
    if _is_regular(fragment_path):
        try:
            fragment = fragment_path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise RenderError(f"{fragment_path}: cannot read: {exc}") from exc
    rendered: list[tuple[Path, str]] = []
    for path in _select_files(src, files):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise RenderError(f"{path}: cannot read: {exc}") from exc
        rendered.append(
            (
                dst / path.name,
                render_text(text, placeholders, runtime_fragment=fragment, name=str(path)),
            )
        )
    dst.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for target, content in rendered:
        _write_atomic(target, content)
        written.append(target)
    return written


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(64)


def _parse_var(raw: str) -> tuple[str, str]:
    key, sep, value = raw.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"--var expects KEY=VALUE, got {raw!r}")
    return key, value


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m gateway.deployment.persona_render")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    render = sub.add_parser("render")
    render.add_argument("--profile", required=True)
    render.add_argument("--src", required=True, type=Path)
    render.add_argument("--dst", required=True, type=Path)
    render.add_argument("--var", action="append", default=[], type=_parse_var)
    render.add_argument("--files", help="comma-separated file names (default: every *.md)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    variables: dict[str, str] = {}
    for key, value in args.var:
        if key in variables:
            print(f"ERROR: duplicate --var {key}", file=sys.stderr)
            return 2
        variables[key] = value
    files = None if args.files is None else args.files.split(",")
    if files is not None and any(not f for f in files):
        parser.error("--files must be a comma-separated list of non-empty file names")
    try:
        profile = load_profile(args.profile)
        written = render_tree(profile, args.src, args.dst, variables, files)
    except (AgentProfileError, RenderError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
