"""File identity and content measurements used by deployment guards."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from collections.abc import Sequence


def file_sha256(path: str) -> str:
    """Return the SHA-256 digest of the bytes read through *path*."""

    try:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise RuntimeError(
            f"ERROR: Could not read guarded file while calculating sha256: {path}\n"
            "       Refusing before mutation."
        ) from exc


def file_bytes(path: str) -> int:
    """Return the byte count reported for a regular deployment file."""

    try:
        with open(path, "rb") as source:
            path_stat = os.fstat(source.fileno())
            if not stat.S_ISREG(path_stat.st_mode):
                raise OSError("not a regular file")
            return path_stat.st_size
    except OSError as exc:
        raise RuntimeError(
            f"ERROR: Could not read guarded file while measuring bytes: {path}\n"
            "       Refusing before mutation."
        ) from exc


def _lstat_identity(path: str) -> str:
    path_stat = os.lstat(path)
    return f"{path_stat.st_dev}:{path_stat.st_ino}:{path_stat.st_mode:x}:{path_stat.st_nlink}"


def _symlink_target(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return "<unreadable>"


def guarded_regular_file_identity(path: str, context: str) -> str:
    """Capture the exact GNU ``stat %d:%i:%f:%h`` identity of a regular file."""

    try:
        path_stat = os.lstat(path)
    except OSError:
        path_stat = None

    if path_stat is not None and stat.S_ISLNK(path_stat.st_mode):
        raise RuntimeError(
            f"ERROR: Guarded file is a symlink while {context}: {path} -> "
            f"{_symlink_target(path)}\n"
            "       Refusing to trust followed bytes for guarded config publication."
        )
    if path_stat is None or not stat.S_ISREG(path_stat.st_mode):
        raise RuntimeError(
            f"ERROR: Guarded file is not a regular file while {context}: {path}\n"
            "       Refusing to trust followed bytes for guarded config publication."
        )
    try:
        identity = _lstat_identity(path)
    except OSError as exc:
        raise RuntimeError(
            f"ERROR: Could not capture lstat identity while {context}: {path}"
        ) from exc
    nlink = int(identity.rsplit(":", 1)[1])
    if nlink != 1:
        raise RuntimeError(
            f"ERROR: Guarded file is hard-linked while {context}: {path} (link count {nlink}).\n"
            "       Refusing to trust followed bytes for guarded config publication."
        )
    return identity


def verify_guarded_regular_file_identity_unchanged(
    path: str, expected_identity: str, context: str
) -> None:
    """Re-capture and compare a guarded file's complete lstat identity."""

    current_identity = guarded_regular_file_identity(path, context)
    if current_identity != expected_identity:
        raise RuntimeError(
            f"ERROR: Guarded file identity/topology changed during {context}: {path}.\n"
            f"       expected lstat {expected_identity}; got {current_identity}."
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("file-sha256", "file-bytes"):
        command = subparsers.add_parser(name)
        command.add_argument("path")

    identity_parser = subparsers.add_parser("guarded-regular-file-identity")
    identity_parser.add_argument("path")
    identity_parser.add_argument("context")

    verify_parser = subparsers.add_parser("verify-guarded-regular-file-identity-unchanged")
    verify_parser.add_argument("path")
    verify_parser.add_argument("expected_identity")
    verify_parser.add_argument("context")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "file-sha256":
            print(file_sha256(args.path))
        elif args.command == "file-bytes":
            sys.stdout.write(str(file_bytes(args.path)))
        elif args.command == "guarded-regular-file-identity":
            print(guarded_regular_file_identity(args.path, args.context))
        else:
            verify_guarded_regular_file_identity_unchanged(
                args.path, args.expected_identity, args.context
            )
    except OSError:
        print(
            "ERROR: Filesystem operation failed while inspecting guarded file.\n"
            "       Refusing before mutation.",
            file=sys.stderr,
        )
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
