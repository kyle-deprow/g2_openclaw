"""Fail-closed filesystem primitives used by the deployment transaction."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from collections.abc import Sequence


def path_exists_or_symlink(path: str) -> bool:
    """Mirror bash ``[[ -e path || -L path ]]``."""

    return os.path.exists(path) or os.path.islink(path)


def path_owned_by_effective_user(path: str) -> bool:
    """Mirror bash ``[[ -O path ]]`` using the effective uid and followed stat."""

    try:
        return os.stat(path).st_uid == os.geteuid()
    except OSError:
        return False


def _lstat_or_missing(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def guard_no_hardlinked_regular_file(path: str, context: str) -> None:
    """Reject regular destination files with external hard-link aliases."""

    path_stat = _lstat_or_missing(path)
    if path_stat is None or stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        return
    try:
        link_count = os.lstat(path).st_nlink
    except OSError:
        # ``stat -c %h`` is used without a fallback in the shell guard.  A
        # failed command substitution therefore produces an empty value, and
        # the arithmetic comparison simply does not reject the path.
        return
    if link_count > 1:
        raise RuntimeError(
            f"ERROR: Destination path is a hard-linked regular file while {context}: {path} "
            f"(link count {link_count}).\n"
            "       Refusing before mutation to avoid modifying external hard-link aliases."
        )


def guard_destination_path_chain(path: str, context: str) -> None:
    """Reject unsafe components in an absolute destination path."""

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
        path_stat = _lstat_or_missing(current)
        if path_stat is not None and stat.S_ISLNK(path_stat.st_mode):
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
        guard_no_hardlinked_regular_file(current, context)


def _dirname(path: str) -> str:
    """Return the GNU ``dirname`` result for the path forms we accept."""

    if not path:
        return "."
    stripped = path.rstrip("/")
    if not stripped:
        return "/"
    return os.path.dirname(stripped) or "."


def guard_destination_parent_path_chain(path: str, context: str) -> None:
    """Guard all ancestors of *path*, leaving its final component untouched."""

    if not path:
        raise RuntimeError(
            f"ERROR: Empty destination path while {context}; refusing before mutation."
        )
    guard_destination_path_chain(_dirname(path), context)


def _run(command: list[str]) -> int:
    try:
        return subprocess.run(command, check=False).returncode
    except OSError:
        print(
            f"ERROR: Failed to execute {command[0]}; refusing before mutation.",
            file=sys.stderr,
        )
        return 1


def _run_guarded(command: list[str]) -> int:
    return 0 if _run(command) == 0 else 1


def guarded_mkdir_p(destination_path: str, context: str) -> int:
    guard_destination_path_chain(destination_path, context)
    status = _run_guarded(["mkdir", "-p", destination_path])
    if status != 0:
        return status
    guard_destination_path_chain(destination_path, context)
    return 0


def copy_path_topology(source_path: str, destination_path: str) -> int:
    return _run(["cp", "-aT", "--", source_path, destination_path])


def guarded_copy_path_topology(source_path: str, destination_path: str, context: str) -> int:
    guard_destination_path_chain(destination_path, context)
    if copy_path_topology(source_path, destination_path) != 0:
        return 1
    guard_destination_path_chain(destination_path, context)
    return 0


def guarded_copy_path_topology_preserving_final_symlink_topology(
    source_path: str, destination_path: str, context: str
) -> int:
    guard_destination_parent_path_chain(destination_path, context)
    if copy_path_topology(source_path, destination_path) != 0:
        return 1
    guard_destination_parent_path_chain(destination_path, context)
    return 0


def guarded_cp_file(source_path: str, destination_path: str, context: str) -> int:
    guard_destination_path_chain(destination_path, context)
    if os.path.isdir(destination_path) and not os.path.islink(destination_path):
        raise RuntimeError(
            f"ERROR: Destination path is an existing directory while {context}: "
            f"{destination_path}\n"
            "       Refusing before cp to avoid source-to-destination-directory behavior."
        )
    if _run_guarded(["cp", source_path, destination_path]) != 0:
        return 1
    guard_destination_path_chain(destination_path, context)
    return 0


def guarded_chmod(mode: str, destination_path: str, context: str) -> int:
    guard_destination_path_chain(destination_path, context)
    if _run_guarded(["chmod", mode, destination_path]) != 0:
        return 1
    guard_destination_path_chain(destination_path, context)
    return 0


def guarded_chmod_reference(reference_path: str, destination_path: str, context: str) -> int:
    guard_destination_path_chain(destination_path, context)
    if _run_guarded(["chmod", f"--reference={reference_path}", destination_path]) != 0:
        return 1
    guard_destination_path_chain(destination_path, context)
    return 0


def guarded_rm_rf(destination_path: str, context: str) -> int:
    guard_destination_path_chain(destination_path, context)
    return _run(["rm", "-rf", "--", destination_path])


def guarded_rm_f(destination_path: str, context: str) -> int:
    guard_destination_path_chain(destination_path, context)
    return _run(["rm", "-f", "--", destination_path])


def guarded_rm(destination_path: str, context: str) -> int:
    guard_destination_path_chain(destination_path, context)
    return _run(["rm", "--", destination_path])


def guarded_rmdir(destination_path: str, context: str) -> int:
    guard_destination_path_chain(destination_path, context)
    return _run(["rmdir", destination_path])


def guarded_mv_replace(
    source_path: str, destination_path: str, context: str, options: Sequence[str] = ()
) -> int:
    guard_destination_path_chain(destination_path, context)
    if _run(["mv", *options, source_path, destination_path]) != 0:
        return 1
    guard_destination_path_chain(destination_path, context)
    return 0


def guarded_mv_replace_preserving_final_symlink_topology(
    source_path: str,
    destination_path: str,
    context: str,
    options: Sequence[str] = (),
) -> int:
    guard_destination_parent_path_chain(destination_path, context)
    if _run(["mv", *options, source_path, destination_path]) != 0:
        return 1
    guard_destination_parent_path_chain(destination_path, context)
    return 0


def collect_find_results_null(
    output_path: str, scan_root: str, context: str, find_arguments: Sequence[str]
) -> int:
    try:
        with open(output_path, "wb") as output:
            completed = subprocess.run(
                ["find", scan_root, *find_arguments, "-print0"],
                check=False,
                stdout=output,
            )
    except OSError:
        print(
            f"ERROR: Failed to collect managed scan results from {scan_root} while {context};"
            " refusing before mutation.",
            file=sys.stderr,
        )
        completed = None
    if completed is not None and completed.returncode == 0:
        return 0

    print(
        f"ERROR: Failed to scan {scan_root} while {context}; "
        "refusing to continue with partial results.",
        file=sys.stderr,
    )
    try:
        cleanup_status = guarded_rm_f(
            output_path, f"removing failed managed scan output {output_path}"
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        cleanup_status = 1
    if cleanup_status != 0:
        print(f"ERROR: Failed to remove failed managed scan output {output_path}.", file=sys.stderr)
        print(f"Managed scan output preserved at {output_path}", file=sys.stderr)
    return 1


def restore_path_topology_from_backup(
    backup_path: str, destination_path: str, restore_stage: str
) -> int:
    try:
        try:
            status = guarded_rm_rf(restore_stage, f"clearing staged restore path {restore_stage}")
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            status = 1
        if status != 0:
            raise RuntimeError(
                f"ERROR: Failed to clear staged restore path {restore_stage} during rollback."
            )

        try:
            status = guarded_copy_path_topology(
                backup_path,
                restore_stage,
                f"staging rollback restore for {destination_path}",
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            status = 1
        if status != 0:
            raise RuntimeError(
                f"ERROR: Failed to stage backup {backup_path} for rollback to {destination_path}.\n"
                "       Original artifact path left intact; recoverable backup preserved at "
                f"{backup_path}."
            )

        destination_parent = _dirname(destination_path)
        try:
            status = guarded_mkdir_p(
                destination_parent,
                f"recreating parent directory {destination_parent} during rollback",
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            status = 1
        if status != 0:
            raise RuntimeError(
                f"ERROR: Failed to recreate parent directory {destination_parent} during "
                "rollback.\n"
                f"       staged restore copy preserved at {restore_stage}."
            )

        try:
            status = guarded_rm_rf(
                destination_path,
                f"removing changed path {destination_path} during rollback",
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            status = 1
        if status != 0:
            raise RuntimeError(
                f"ERROR: Failed to remove changed path {destination_path} during rollback.\n"
                f"       staged restore copy preserved at {restore_stage}."
            )

        try:
            status = guarded_mv_replace(
                restore_stage,
                destination_path,
                f"restoring {destination_path} from rollback stage",
                ("-T",),
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            status = 1
        if status != 0:
            raise RuntimeError(
                f"ERROR: Failed to replace {destination_path} with staged restore "
                f"{restore_stage}.\n"
                "       Recoverable backup preserved at "
                f"{backup_path}; staged restore copy preserved at {restore_stage}."
            )
    except RuntimeError:
        raise
    return 0


def _guarded_unlink(path: str, context: str) -> int:
    guard_destination_path_chain(path, context)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise RuntimeError(
            f"ERROR: Failed to remove path while {context}: {path}\n"
            "       Refusing before mutation."
        ) from exc
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_path_context(name: str) -> argparse.ArgumentParser:
        command = subparsers.add_parser(name)
        command.add_argument("path")
        command.add_argument("context")
        return command

    subparsers.add_parser("path-exists-or-symlink").add_argument("path")
    subparsers.add_parser("path-owned-by-effective-user").add_argument("path")
    add_path_context("guard-no-hardlinked-regular-file")
    add_path_context("guard-destination-path-chain")
    add_path_context("guard-destination-parent-path-chain")
    add_path_context("guarded-mkdir-p")

    copy_parser = subparsers.add_parser("copy-path-topology")
    copy_parser.add_argument("source_path")
    copy_parser.add_argument("destination_path")

    for name in (
        "guarded-copy-path-topology",
        "guarded-copy-path-topology-preserving-final-symlink-topology",
        "guarded-cp-file",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("source_path")
        command.add_argument("destination_path")
        command.add_argument("context")

    chmod_parser = subparsers.add_parser("guarded-chmod")
    chmod_parser.add_argument("mode")
    chmod_parser.add_argument("destination_path")
    chmod_parser.add_argument("context")

    chmod_ref_parser = subparsers.add_parser("guarded-chmod-reference")
    chmod_ref_parser.add_argument("reference_path")
    chmod_ref_parser.add_argument("destination_path")
    chmod_ref_parser.add_argument("context")

    find_parser = subparsers.add_parser("collect-find-results-null")
    find_parser.add_argument("output_path")
    find_parser.add_argument("scan_root")
    find_parser.add_argument("context")
    find_parser.add_argument("find_arguments", nargs=argparse.REMAINDER)

    for name in ("guarded-rm-rf", "guarded-rm-f", "guarded-rm", "guarded-rmdir"):
        add_path_context(name)

    for name in ("guarded-mv-replace", "guarded-mv-replace-preserving-final-symlink-topology"):
        command = subparsers.add_parser(name)
        command.add_argument("source_path")
        command.add_argument("destination_path")
        command.add_argument("context")
        command.add_argument("options", nargs=argparse.REMAINDER)

    restore_parser = subparsers.add_parser("restore-path-topology-from-backup")
    restore_parser.add_argument("backup_path")
    restore_parser.add_argument("destination_path")
    restore_parser.add_argument("restore_stage")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        command = args.command
        if command == "path-exists-or-symlink":
            return int(not path_exists_or_symlink(args.path))
        if command == "path-owned-by-effective-user":
            return int(not path_owned_by_effective_user(args.path))
        if command == "guard-no-hardlinked-regular-file":
            guard_no_hardlinked_regular_file(args.path, args.context)
            return 0
        if command == "guard-destination-path-chain":
            guard_destination_path_chain(args.path, args.context)
            return 0
        if command == "guard-destination-parent-path-chain":
            guard_destination_parent_path_chain(args.path, args.context)
            return 0
        if command == "guarded-mkdir-p":
            return guarded_mkdir_p(args.path, args.context)
        if command == "copy-path-topology":
            return copy_path_topology(args.source_path, args.destination_path)
        if command == "guarded-copy-path-topology":
            return guarded_copy_path_topology(args.source_path, args.destination_path, args.context)
        if command == "guarded-copy-path-topology-preserving-final-symlink-topology":
            return guarded_copy_path_topology_preserving_final_symlink_topology(
                args.source_path, args.destination_path, args.context
            )
        if command == "guarded-cp-file":
            return guarded_cp_file(args.source_path, args.destination_path, args.context)
        if command == "guarded-chmod":
            return guarded_chmod(args.mode, args.destination_path, args.context)
        if command == "guarded-chmod-reference":
            return guarded_chmod_reference(args.reference_path, args.destination_path, args.context)
        if command == "collect-find-results-null":
            return collect_find_results_null(
                args.output_path, args.scan_root, args.context, args.find_arguments
            )
        if command == "guarded-rm-rf":
            return guarded_rm_rf(args.path, args.context)
        if command == "guarded-rm-f":
            return guarded_rm_f(args.path, args.context)
        if command == "guarded-rm":
            return guarded_rm(args.path, args.context)
        if command == "guarded-rmdir":
            return guarded_rmdir(args.path, args.context)
        if command == "guarded-mv-replace":
            return guarded_mv_replace(
                args.source_path, args.destination_path, args.context, args.options
            )
        if command == "guarded-mv-replace-preserving-final-symlink-topology":
            return guarded_mv_replace_preserving_final_symlink_topology(
                args.source_path, args.destination_path, args.context, args.options
            )
        return restore_path_topology_from_backup(
            args.backup_path, args.destination_path, args.restore_stage
        )
    except OSError:
        print(
            "ERROR: Filesystem operation failed while executing a deployment guard.\n"
            "       Refusing before mutation.",
            file=sys.stderr,
        )
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
