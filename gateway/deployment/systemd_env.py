"""Systemd environment decoding and stale Azure preload cleanup."""

from __future__ import annotations

import argparse
import base64
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

STATE_MARKER = "__G2_SYSTEMD_ENV_STATE__"


class SystemdEnvironmentDecodeError(ValueError):
    """Raised when systemd's encoded environment word is ambiguous."""


def _append_escape_byte(parts: list[str], code: int) -> None:
    parts.append(chr(code if code < 0x80 else 0xDC00 + code))


def decode_systemd_show_environment_word(encoded: str) -> str:
    """Decode one word emitted by ``systemctl show-environment``.

    The accepted forms and escape restrictions intentionally mirror the bash
    decoder.  In particular, NUL escapes and an additional hexadecimal digit
    after ``\\xHH`` are rejected rather than guessed.
    """

    if encoded.startswith("$'"):
        decoded: list[str] = []
        index = 2
        length = len(encoded)
        while index < length:
            char = encoded[index]
            if char == "'":
                index += 1
                if index != length:
                    raise SystemdEnvironmentDecodeError
                return "".join(decoded)
            if char == "\\":
                index += 1
                if index >= length:
                    raise SystemdEnvironmentDecodeError
                next_char = encoded[index]
                if next_char == "n":
                    decoded.append("\n")
                elif next_char == "t":
                    decoded.append("\t")
                elif next_char == "r":
                    decoded.append("\r")
                elif next_char == "b":
                    decoded.append("\b")
                elif next_char == "f":
                    decoded.append("\f")
                elif next_char == "v":
                    decoded.append("\v")
                elif next_char == "a":
                    decoded.append("\a")
                elif next_char in ("e", "E"):
                    decoded.append("\x1b")
                elif next_char in ("\\", "'", '"', "?"):
                    decoded.append(next_char)
                elif next_char == "x":
                    if index + 2 >= length:
                        raise SystemdEnvironmentDecodeError
                    escape_digits = encoded[index + 1 : index + 3]
                    if re.fullmatch(r"[0-9a-fA-F]{2}", escape_digits) is None:
                        raise SystemdEnvironmentDecodeError
                    if index + 3 < length and encoded[index + 3] in "0123456789abcdefABCDEF":
                        raise SystemdEnvironmentDecodeError
                    code = int(escape_digits, 16)
                    if code == 0:
                        raise SystemdEnvironmentDecodeError
                    _append_escape_byte(decoded, code)
                    index += 2
                elif next_char in "01234567":
                    if index + 2 >= length:
                        raise SystemdEnvironmentDecodeError
                    escape_digits = encoded[index : index + 3]
                    if re.fullmatch(r"[0-7]{3}", escape_digits) is None:
                        raise SystemdEnvironmentDecodeError
                    code = int(escape_digits, 8)
                    if code == 0 or code > 255:
                        raise SystemdEnvironmentDecodeError
                    _append_escape_byte(decoded, code)
                    index += 2
                else:
                    raise SystemdEnvironmentDecodeError
            else:
                decoded.append(char)
            index += 1
        raise SystemdEnvironmentDecodeError

    if len(encoded) >= 2 and encoded.startswith("'") and encoded.endswith("'"):
        plain_decoded = encoded[1:-1]
        if "'" in plain_decoded:
            raise SystemdEnvironmentDecodeError
        return plain_decoded

    if re.fullmatch(r"[-._~/:@%+=,A-Za-z0-9]*", encoded) is not None:
        return encoded
    raise SystemdEnvironmentDecodeError


def decode_systemd_show_environment_node_options(manager_env: str) -> tuple[bool, str]:
    """Find and decode the unique NODE_OPTIONS assignment in manager output."""

    present = False
    value = ""
    # Bash command substitution strips trailing newlines before the here-string
    # used by the original helper.  split('\n') retains all other characters,
    # including a possible carriage return.
    for manager_line in manager_env.split("\n"):
        if not manager_line.startswith("NODE_OPTIONS="):
            continue
        if present:
            raise SystemdEnvironmentDecodeError(
                "ERROR: systemd user manager emitted multiple NODE_OPTIONS assignments; "
                "refusing ambiguous cleanup."
            )
        present = True
        try:
            decoded = decode_systemd_show_environment_word(manager_line[len("NODE_OPTIONS=") :])
        except SystemdEnvironmentDecodeError as exc:
            raise SystemdEnvironmentDecodeError(
                "ERROR: Could not safely decode systemd user manager NODE_OPTIONS assignment; "
                "refusing ambiguous cleanup."
            ) from exc
        # The bash helper stores the decoder through command substitution.
        value = decoded.rstrip("\n")
    return present, value


def _read_systemd_manager_environment() -> str:
    completed = subprocess.run(
        ["systemctl", "--user", "show-environment"],
        check=False,
        stdout=subprocess.PIPE,
        text=True,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "ERROR: Could not inspect systemd user manager environment for stale Azure "
            "NODE_OPTIONS."
        )
    return completed.stdout.rstrip("\n")


def _emit_manager_state(value: str) -> None:
    encoded = base64.b64encode(value.encode("utf-8", errors="surrogateescape")).decode("ascii")
    print(f"{STATE_MARKER}\t1\t1\t{encoded}", flush=True)


def _unset_manager_node_options() -> None:
    completed = subprocess.run(
        ["systemctl", "--user", "unset-environment", "NODE_OPTIONS"],
        check=False,
        stdout=subprocess.PIPE,
        text=True,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, end="", file=sys.stderr)
        raise RuntimeError(
            "ERROR: Failed to unset stale Azure NODE_OPTIONS from systemd user manager environment."
        )


def _verify_manager_node_options_stale_preload_absent(stale_pattern: str) -> None:
    try:
        manager_env = _read_systemd_manager_environment()
    except RuntimeError:
        raise RuntimeError(
            "ERROR: Could not re-check systemd user manager environment after unsetting "
            "NODE_OPTIONS."
        ) from None
    try:
        present, value = decode_systemd_show_environment_node_options(manager_env)
    except SystemdEnvironmentDecodeError as exc:
        print(str(exc), file=sys.stderr)
        raise RuntimeError from exc
    if present and stale_pattern in value:
        raise RuntimeError(
            "ERROR: systemd user manager still exposes stale Azure NODE_OPTIONS after "
            "unset-environment; refusing to continue."
        )


def _guard_no_hardlinked_regular_file(path: str, context: str) -> None:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        return
    if path_stat.st_nlink > 1:
        raise RuntimeError(
            f"ERROR: Destination path is a hard-linked regular file while {context}: {path} "
            f"(link count {path_stat.st_nlink}).\n"
            "       Refusing before mutation to avoid modifying external hard-link aliases."
        )


def guard_destination_path_chain(path: str, context: str) -> None:
    if not path:
        raise RuntimeError(
            f"ERROR: Empty destination path while {context}; refusing before mutation."
        )
    if not os.path.isabs(path):
        raise RuntimeError(
            f"ERROR: Destination path is not absolute while {context}: {path}\n"
            "       Refusing before mutation."
        )
    current = ""
    for component in path[1:].split("/"):
        if not component or component == ".":
            continue
        if component == "..":
            raise RuntimeError(
                f"ERROR: Destination path contains '..' while {context}: {path}\n"
                "       Refusing before mutation."
            )
        current += f"/{component}"
        if os.path.islink(current):
            try:
                target = os.readlink(current)
            except OSError:
                target = "<unreadable>"
            raise RuntimeError(
                f"ERROR: Destination path chain contains symlink while {context}: "
                f"{current} -> {target}\n"
                f"       Full destination: {path}\n"
                "       Refusing before mutation to avoid following nested symlinks outside "
                "the managed root."
            )
        _guard_no_hardlinked_regular_file(current, context)


def _guarded_unlink(path: str, context: str) -> None:
    guard_destination_path_chain(path, context)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return


_C_LOCALE_WHITESPACE = frozenset(" \t\n\v\f\r")


def _is_c_locale_whitespace(char: str) -> bool:
    return char in _C_LOCALE_WHITESPACE


def _is_ignored_continuation_line(line: str) -> bool:
    position = 0
    while position < len(line) and _is_c_locale_whitespace(line[position]):
        position += 1
    return position == len(line) or line[position] in "#;"


def _read_environment_token(rest: str, start: int) -> tuple[str, str, int, bool]:
    position = start
    quote = ""
    token_raw = ""
    token_value = ""
    while position < len(rest):
        char = rest[position]
        if not quote and _is_c_locale_whitespace(char):
            break
        token_raw += char
        if quote:
            if char == "\\":
                if position < len(rest) - 1:
                    next_char = rest[position + 1]
                    token_raw += next_char
                    token_value += next_char
                    position += 2
                    continue
            elif char == quote:
                quote = ""
                position += 1
                continue
            else:
                token_value += char
        elif char in ('"', "'"):
            quote = char
        elif char == "\\":
            if position < len(rest) - 1:
                next_char = rest[position + 1]
                token_raw += next_char
                token_value += next_char
                position += 2
                continue
        else:
            token_value += char
        position += 1
    return token_raw, token_value, position, not quote and bool(token_raw)


def _rewrite_environment_line(line: str, stale_pattern: str) -> tuple[str | None, bool]:
    prefix_length = 0
    while prefix_length < len(line) and _is_c_locale_whitespace(line[prefix_length]):
        prefix_length += 1
    if not line.startswith("Environment=", prefix_length):
        return None, False
    prefix_length += len("Environment=")
    prefix = line[:prefix_length]
    rest = line[prefix_length:]
    output = prefix
    kept = False
    removed = False
    position = 0
    while position < len(rest):
        whitespace_start = position
        while position < len(rest) and _is_c_locale_whitespace(rest[position]):
            position += 1
        whitespace = rest[whitespace_start:position]
        if position >= len(rest):
            break
        token_raw, token_value, next_position, ok = _read_environment_token(rest, position)
        if not ok:
            return None, stale_pattern in line
        if token_value.startswith("NODE_OPTIONS=") and stale_pattern in token_value:
            removed = True
        elif not kept:
            output += token_raw
            kept = True
        else:
            output += whitespace + token_raw
        position = next_position
    if not removed:
        return None, False
    return (output if kept else ""), False


def _rewrite_systemd_environment_file(path: str, stale_pattern: str) -> bytes | None:
    content = Path(path).read_bytes().decode("utf-8", errors="surrogateescape")
    records = content.split("\n")
    if content.endswith("\n"):
        records.pop()
    block: list[str] = []
    logical = ""
    ignored_block: list[str] = []
    output: list[str] = []
    changed = False

    def flush_block() -> None:
        nonlocal block, logical, ignored_block, changed
        rewritten, parse_error = _rewrite_environment_line(logical, stale_pattern)
        if parse_error:
            raise ValueError
        if rewritten is not None:
            changed = True
            if rewritten:
                output.append(rewritten)
            output.extend(ignored_block)
        else:
            output.extend(block)
        block = []
        logical = ""
        ignored_block = []

    for line in records:
        if logical and _is_ignored_continuation_line(line):
            block.append(line)
            ignored_block.append(line)
            continue
        if not logical and _is_ignored_continuation_line(line):
            output.append(line)
            continue
        continued = line.endswith("\\")
        part = line[:-1] if continued else line
        block.append(line)
        logical = part if not logical else f"{logical} {part}"
        if not continued:
            flush_block()
    if block:
        raise ValueError
    if not changed:
        return None
    return ("\n".join(output) + "\n").encode("utf-8", errors="surrogateescape")


def _find_dropins(dropin_dir: str, scan_context: str) -> list[str]:
    temp_dir = os.environ.get("TMPDIR", "/tmp")
    guard_destination_path_chain(
        temp_dir,
        f"creating managed systemd scan output in {temp_dir}",
    )
    descriptor, output_path = tempfile.mkstemp(
        prefix="push-openclaw-systemd-dropins.",
        dir=temp_dir,
    )
    os.close(descriptor)
    try:
        guard_destination_path_chain(
            output_path, f"creating managed systemd scan output {output_path}"
        )
        with open(output_path, "wb") as output_file:
            completed = subprocess.run(
                ["find", dropin_dir, "-maxdepth", "1", "-type", "f", "-name", "*.conf", "-print0"],
                check=False,
                stdout=output_file,
            )
        if completed.returncode != 0:
            _guarded_unlink(output_path, f"removing failed managed scan output {output_path}")
            raise RuntimeError(
                f"ERROR: Failed to scan {dropin_dir} while {scan_context}; refusing to continue "
                "with partial results."
            )
        data = Path(output_path).read_bytes()
        return [os.fsdecode(item) for item in data.split(b"\0") if item]
    finally:
        if os.path.lexists(output_path):
            _guarded_unlink(output_path, f"removing managed systemd scan output {output_path}")


def _rewrite_candidate(path: str, stale_pattern: str) -> bool:
    context = f"rewriting managed systemd environment file {path}"
    guard_destination_path_chain(path, context)
    temp_context = f"creating temporary managed systemd rewrite file for {path}"
    guard_destination_path_chain(os.path.dirname(path), temp_context)
    descriptor, temp_path = tempfile.mkstemp(prefix=f"{path}.")
    os.close(descriptor)
    guard_destination_path_chain(
        temp_path, f"creating temporary managed systemd rewrite file {temp_path}"
    )
    try:
        try:
            rewritten = _rewrite_systemd_environment_file(path, stale_pattern)
        except (OSError, ValueError):
            try:
                _guarded_unlink(
                    temp_path, f"removing failed temporary systemd rewrite file {temp_path}"
                )
            finally:
                raise RuntimeError(
                    "ERROR: Failed to inspect "
                    f"{path} for stale Azure NODE_OPTIONS assignment lines."
                )
        if rewritten is None:
            _guarded_unlink(
                temp_path, f"removing unchanged temporary systemd rewrite file {temp_path}"
            )
            return False
        Path(temp_path).write_bytes(rewritten)
        try:
            reference_mode = stat.S_IMODE(os.stat(path).st_mode)
            os.chmod(temp_path, reference_mode)
        except OSError:
            _guarded_unlink(
                temp_path,
                f"removing temporary systemd rewrite file {temp_path} after chmod failure",
            )
            raise RuntimeError(
                f"ERROR: Failed to preserve permissions while rewriting {path}."
            ) from None
        guard_destination_path_chain(
            path, f"publishing rewritten managed systemd environment file {path}"
        )
        completed = subprocess.run(["mv", temp_path, path], check=False)
        if completed.returncode != 0:
            if os.path.lexists(temp_path):
                _guarded_unlink(
                    temp_path,
                    f"removing temporary systemd rewrite file {temp_path} after publish failure",
                )
            raise RuntimeError(
                f"ERROR: Failed to rewrite stale Azure NODE_OPTIONS assignment lines in {path}."
            )
        guard_destination_path_chain(
            path, f"publishing rewritten managed systemd environment file {path}"
        )
        return True
    except RuntimeError:
        raise
    finally:
        if os.path.lexists(temp_path):
            _guarded_unlink(temp_path, f"removing temporary systemd rewrite file {temp_path}")


def remove_stale_azure_node_options_for_codex(
    service_path: str,
    dropin_dir: str,
    stale_pattern: str,
) -> int:
    """Unset manager state and remove stale NODE_OPTIONS assignments."""

    changed = False
    manager_env = _read_systemd_manager_environment()
    try:
        present, value = decode_systemd_show_environment_node_options(manager_env)
    except SystemdEnvironmentDecodeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if present and stale_pattern in value:
        _emit_manager_state(value)
        try:
            _unset_manager_node_options()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        try:
            _verify_manager_node_options_stale_preload_absent(stale_pattern)
        except RuntimeError as exc:
            if str(exc):
                print(str(exc), file=sys.stderr)
            return 1
        changed = True
        print("Unset stale Azure NODE_OPTIONS from systemd user manager environment")

    candidates: list[str] = []
    if os.path.isfile(service_path):
        candidates.append(service_path)
    if os.path.isdir(dropin_dir):
        guard_destination_path_chain(
            dropin_dir,
            f"scanning managed systemd drop-in directory {dropin_dir}",
        )
        candidates.extend(
            _find_dropins(dropin_dir, f"scanning managed systemd drop-in directory {dropin_dir}")
        )

    for path in candidates:
        if _rewrite_candidate(path, stale_pattern):
            changed = True
            print(f"Removed stale Azure NODE_OPTIONS assignment line from {path}")

    if changed:
        completed = subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    decode_word_parser = subparsers.add_parser("decode-word")
    decode_word_parser.add_argument("encoded")
    decode_node_parser = subparsers.add_parser("decode-node-options")
    decode_node_parser.add_argument("manager_env")
    verify_parser = subparsers.add_parser("verify-node-options")
    verify_parser.add_argument("stale_pattern")
    remove_parser = subparsers.add_parser("remove-stale-azure-node-options")
    remove_parser.add_argument("service_path")
    remove_parser.add_argument("dropin_dir")
    remove_parser.add_argument("stale_pattern")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "decode-word":
        try:
            print(decode_systemd_show_environment_word(args.encoded), end="")
        except SystemdEnvironmentDecodeError:
            return 1
        return 0
    if args.command == "decode-node-options":
        try:
            present, value = decode_systemd_show_environment_node_options(args.manager_env)
        except SystemdEnvironmentDecodeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        encoded = base64.b64encode(value.encode("utf-8", errors="surrogateescape")).decode("ascii")
        print(f"{int(present)}\t{encoded}")
        return 0
    if args.command == "verify-node-options":
        try:
            _verify_manager_node_options_stale_preload_absent(args.stale_pattern)
        except RuntimeError as exc:
            if str(exc):
                print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.command == "remove-stale-azure-node-options":
        try:
            return remove_stale_azure_node_options_for_codex(
                args.service_path,
                args.dropin_dir,
                args.stale_pattern,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
