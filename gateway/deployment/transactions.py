"""Journal-backed filesystem transactions used by the OpenClaw push script."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from .guarded_fs import (
    guard_destination_path_chain,
    guarded_copy_path_topology,
    guarded_copy_path_topology_preserving_final_symlink_topology,
    guarded_mv_replace,
    guarded_mv_replace_preserving_final_symlink_topology,
    guarded_rm_f,
    guarded_rm_rf,
    guarded_rmdir,
    path_exists_or_symlink,
    restore_path_topology_from_backup,
)

JOURNAL_NAME = "transaction.json"
JOURNAL_VERSION = 1
UNIT_KIND = "unit"
ARTIFACT_KIND = "artifact"
PRESENT = "present"
ABSENT = "absent"
FAILED = "failed"
VALID_STATES = frozenset({PRESENT, ABSENT, FAILED})
ACTIVE = "active"
FINALIZED = "finalized"
ROLLED_BACK = "rolled_back"
VALID_LIFECYCLES = frozenset({ACTIVE, FINALIZED, ROLLED_BACK})
ARTIFACT_SYSTEMD_MARKER = "__G2_ARTIFACT_RESTORED_SYSTEMD__"


class JournalError(RuntimeError):
    """Raised when a transaction journal cannot be trusted."""


class SilentFailure(RuntimeError):
    """An operation already emitted its contract diagnostics."""


@dataclass(frozen=True)
class TransactionJournal:
    """Validated state persisted in one recovery directory."""

    kind: str
    backup_dir: str
    paths: tuple[str, ...]
    states: tuple[str, ...]
    lifecycle: str


def _journal_path(backup_dir: str) -> str:
    return os.path.join(backup_dir, JOURNAL_NAME)


def _guard_recovery_dir(backup_dir: str, action: str) -> None:
    try:
        guard_destination_path_chain(backup_dir, action)
    except RuntimeError as exc:
        raise JournalError(str(exc)) from exc
    if not os.path.isdir(backup_dir):
        raise JournalError(
            f"ERROR: Transaction recovery directory is missing while {action}: {backup_dir}"
        )


def _new_journal(kind: str, backup_dir: str, paths: Sequence[str]) -> TransactionJournal:
    return TransactionJournal(kind, backup_dir, tuple(paths), tuple(FAILED for _ in paths), ACTIVE)


def _journal_payload(journal: TransactionJournal) -> dict[str, object]:
    return {
        "version": JOURNAL_VERSION,
        "kind": journal.kind,
        "backup_dir": journal.backup_dir,
        "paths": list(journal.paths),
        "states": list(journal.states),
        "lifecycle": journal.lifecycle,
    }


def _as_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise JournalError(f"journal field {field!r} is not a string")
    return value


def _as_string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise JournalError(f"journal field {field!r} is not a string list")
    return tuple(cast(str, item) for item in value)


def _read_journal(backup_dir: str, kind: str) -> TransactionJournal:
    _guard_recovery_dir(backup_dir, f"reading managed {kind} transaction journal")
    journal_path = _journal_path(backup_dir)
    if not path_exists_or_symlink(journal_path):
        raise JournalError(
            f"ERROR: Managed OpenClaw {kind} transaction journal is missing: {journal_path}; "
            "refusing to trust transaction state."
        )
    try:
        guard_destination_path_chain(journal_path, f"reading managed {kind} transaction journal")
        with open(journal_path, encoding="utf-8") as source:
            payload = cast(object, json.load(source))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise JournalError(
            f"ERROR: Managed OpenClaw {kind} transaction journal is corrupt: {journal_path}; "
            "refusing to trust transaction state."
        ) from exc
    except RuntimeError as exc:
        raise JournalError(str(exc)) from exc

    try:
        if not isinstance(payload, dict):
            raise JournalError("journal root is not an object")
        if payload.get("version") != JOURNAL_VERSION:
            raise JournalError("unsupported journal version")
        journal_kind = _as_string(payload.get("kind"), "kind")
        journal_backup_dir = _as_string(payload.get("backup_dir"), "backup_dir")
        paths = _as_string_list(payload.get("paths"), "paths")
        states = _as_string_list(payload.get("states"), "states")
        lifecycle = _as_string(payload.get("lifecycle"), "lifecycle")
        if journal_kind != kind:
            raise JournalError("journal kind does not match the requested transaction")
        if journal_backup_dir != backup_dir:
            raise JournalError("journal recovery directory does not match the requested path")
        if len(paths) != len(states):
            raise JournalError("journal paths and states have different lengths")
        if any(state not in VALID_STATES for state in states):
            raise JournalError("journal contains an unknown snapshot state")
        if lifecycle not in VALID_LIFECYCLES:
            raise JournalError("journal contains an unknown lifecycle state")
    except JournalError as exc:
        journal_error = (
            f"ERROR: Managed OpenClaw {kind} transaction journal is corrupt: {journal_path}; "
            "refusing to trust transaction state."
        )
        raise JournalError(journal_error) from exc
    return TransactionJournal(journal_kind, journal_backup_dir, paths, states, lifecycle)


def _write_journal(journal: TransactionJournal, *, creating: bool = False) -> None:
    _guard_recovery_dir(journal.backup_dir, f"writing managed {journal.kind} transaction journal")
    journal_path = _journal_path(journal.backup_dir)
    if creating and path_exists_or_symlink(journal_path):
        raise JournalError(
            f"ERROR: Managed OpenClaw {journal.kind} transaction journal already exists: "
            f"{journal_path}; refusing to overwrite transaction state."
        )
    temporary_path = ""
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{JOURNAL_NAME}.", dir=journal.backup_dir
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(_journal_payload(journal), destination, sort_keys=True, separators=(",", ":"))
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        if (
            guarded_mv_replace(
                temporary_path,
                journal_path,
                f"publishing managed {journal.kind} transaction journal {journal_path}",
                ("-T",),
            )
            != 0
        ):
            raise JournalError(f"ERROR: Failed to write transaction journal: {journal_path}")
        temporary_path = ""
    except OSError as exc:
        raise JournalError(f"ERROR: Failed to write transaction journal: {journal_path}") from exc
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _update_state(journal: TransactionJournal, index: int, state: str) -> TransactionJournal:
    states = list(journal.states)
    states[index] = state
    updated = TransactionJournal(
        journal.kind, journal.backup_dir, journal.paths, tuple(states), journal.lifecycle
    )
    _write_journal(updated)
    return updated


def _require_active(journal: TransactionJournal, operation: str) -> None:
    if journal.lifecycle != ACTIVE:
        raise JournalError(
            f"ERROR: Managed {journal.kind} transaction is already {journal.lifecycle}; "
            f"refusing {operation}."
        )


def _with_lifecycle(journal: TransactionJournal, lifecycle: str) -> TransactionJournal:
    updated = TransactionJournal(
        journal.kind, journal.backup_dir, journal.paths, journal.states, lifecycle
    )
    _write_journal(updated)
    return updated


def begin_transaction(kind: str, backup_dir: str, paths: Sequence[str]) -> bool:
    """Create a new fail-closed journal and snapshot all paths."""

    _guard_recovery_dir(backup_dir, f"beginning managed {kind} transaction")
    _write_journal(_new_journal(kind, backup_dir, paths), creating=True)
    return True


def begin_artifact_transaction(backup_dir: str) -> None:
    """Create the artifact journal without taking a snapshot."""

    _guard_recovery_dir(backup_dir, "beginning managed OpenClaw artifact transaction")
    _write_journal(_new_journal(ARTIFACT_KIND, backup_dir, ()), creating=True)


def _snapshot_path(journal: TransactionJournal, index: int, path: str) -> str:
    backup_path = os.path.join(journal.backup_dir, str(index))
    if path_exists_or_symlink(path):
        if os.path.islink(path):
            try:
                target = os.readlink(path)
            except OSError:
                target = "<unreadable>"
            if journal.kind == UNIT_KIND:
                print(
                    f"ERROR: Managed systemd file {path} is a symlink to {target}; "
                    "this publication path cannot preserve symlink topology safely.",
                    file=sys.stderr,
                )
                print("       Refusing before mutating managed systemd files.", file=sys.stderr)
            else:
                print(
                    f"ERROR: Managed OpenClaw artifact {path} is a symlink to {target}; "
                    "this publication path cannot preserve symlink topology safely.",
                    file=sys.stderr,
                )
                print("       Refusing before mutating the managed artifact path.", file=sys.stderr)
            print(_preserved_message(journal), file=sys.stderr)
            return FAILED
        if journal.kind == UNIT_KIND and not os.path.isfile(path):
            print(
                f"ERROR: Managed systemd file {path} exists but is not a regular file; "
                "refusing before mutation.",
                file=sys.stderr,
            )
            print(_preserved_message(journal), file=sys.stderr)
            return FAILED
        try:
            guard_destination_path_chain(path, _snapshot_context(journal, path))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            print(_preserved_message(journal), file=sys.stderr)
            return FAILED
        try:
            status = guarded_copy_path_topology(path, backup_path, _snapshot_context(journal, path))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            status = 1
        if status != 0:
            if journal.kind == UNIT_KIND:
                print(
                    f"ERROR: Failed to snapshot managed systemd file {path} to {backup_path}.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"ERROR: Failed to snapshot managed OpenClaw artifact {path} to {backup_path}.",
                    file=sys.stderr,
                )
            print(_preserved_message(journal), file=sys.stderr)
            return FAILED
        return PRESENT
    return ABSENT


def snapshot_artifact_path(backup_dir: str, path: str) -> None:
    """Append one deduplicated artifact snapshot to an existing journal."""

    journal = _read_journal(backup_dir, ARTIFACT_KIND)
    _require_active(journal, "snapshot")
    try:
        index = journal.paths.index(path)
    except ValueError:
        index = len(journal.paths)
        journal = TransactionJournal(
            journal.kind,
            journal.backup_dir,
            (*journal.paths, path),
            (*journal.states, FAILED),
            journal.lifecycle,
        )
        _write_journal(journal)
    else:
        if journal.states[index] == FAILED:
            raise SilentFailure
        return
    state = _snapshot_path(journal, index, path)
    _update_state(journal, index, state)
    if state == FAILED:
        raise SilentFailure


def snapshot_unit_path(backup_dir: str, path: str) -> None:
    """Snapshot one predeclared managed systemd path."""

    journal = _read_journal(backup_dir, UNIT_KIND)
    _require_active(journal, "snapshot")
    try:
        index = journal.paths.index(path)
    except ValueError as exc:
        raise JournalError(
            "ERROR: requested managed systemd path is not in the transaction journal"
        ) from exc
    if journal.states[index] != FAILED:
        return
    state = _snapshot_path(journal, index, path)
    _update_state(journal, index, state)
    if state == FAILED:
        raise SilentFailure


def _preserved_message(journal: TransactionJournal) -> str:
    if journal.kind == UNIT_KIND:
        return f"Managed systemd recovery directory preserved at {journal.backup_dir}"
    return f"Managed OpenClaw artifact recovery directory preserved at {journal.backup_dir}"


def _snapshot_context(journal: TransactionJournal, path: str) -> str:
    if journal.kind == UNIT_KIND:
        return f"snapshotting managed systemd file {path}"
    return f"snapshotting managed OpenClaw artifact {path}"


def _is_systemd_artifact(path: str, systemd_user_dir: str, gateway_service_name: str) -> bool:
    service_path = os.path.join(systemd_user_dir, gateway_service_name)
    dropin_dir = f"{service_path}.d"
    return path in (service_path, dropin_dir) or path.startswith(f"{dropin_dir}/")


def _run_daemon_reload() -> bool:
    return subprocess.run(["systemctl", "--user", "daemon-reload"], check=False).returncode == 0


def rollback_unit_transaction(backup_dir: str) -> None:
    """Restore all managed unit paths and reload the user manager."""

    journal = _read_journal(backup_dir, UNIT_KIND)
    _require_active(journal, "rollback")
    print("Restoring managed systemd files after failed publication.", file=sys.stderr)
    transaction_failed = False
    for index, path in enumerate(journal.paths):
        backup_path = os.path.join(journal.backup_dir, str(index))
        state = journal.states[index]
        if state == FAILED:
            print(
                f"ERROR: Skipping rollback for {path}; its managed systemd snapshot did not "
                "complete.",
                file=sys.stderr,
            )
            transaction_failed = True
            continue
        if state == PRESENT:
            try:
                status = restore_path_topology_from_backup(
                    backup_path, path, os.path.join(journal.backup_dir, f"restore.{index}")
                )
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                status = 1
            if status != 0:
                print(f"ERROR: Failed to restore managed systemd file {path}.", file=sys.stderr)
                transaction_failed = True
        else:
            try:
                status = guarded_rm_f(
                    path, f"removing newly installed managed systemd file {path} during rollback"
                )
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                status = 1
            if status != 0:
                print(
                    f"ERROR: Failed to remove newly installed managed systemd file {path}.",
                    file=sys.stderr,
                )
                transaction_failed = True
    if not _run_daemon_reload():
        print(
            "ERROR: Failed to reload user systemd units after managed-file rollback.",
            file=sys.stderr,
        )
        transaction_failed = True
    if transaction_failed:
        print(_preserved_message(journal), file=sys.stderr)
        raise SilentFailure
    _with_lifecycle(journal, ROLLED_BACK)


def rollback_artifact_transaction(
    backup_dir: str,
    systemd_user_dir: str | None = None,
    gateway_service_name: str | None = None,
) -> bool:
    """Restore artifact paths in reverse order and report systemd restoration."""

    journal = _read_journal(backup_dir, ARTIFACT_KIND)
    _require_active(journal, "rollback")
    if systemd_user_dir is None:
        systemd_user_dir = os.environ["SYSTEMD_USER_DIR"]
    if gateway_service_name is None:
        gateway_service_name = os.environ["GATEWAY_SERVICE_NAME"]
    print("Restoring managed OpenClaw artifacts after failed publication.", file=sys.stderr)
    transaction_failed = False
    restored_systemd = False
    for index in range(len(journal.paths) - 1, -1, -1):
        path = journal.paths[index]
        backup_path = os.path.join(journal.backup_dir, str(index))
        restore_stage = os.path.join(journal.backup_dir, f"restore.{index}")
        state = journal.states[index]
        if state == FAILED:
            print(
                f"ERROR: Skipping rollback for {path}; its managed artifact snapshot did not "
                "complete.",
                file=sys.stderr,
            )
            transaction_failed = True
            continue
        if state == PRESENT:
            if not path_exists_or_symlink(backup_path):
                print(
                    f"ERROR: Managed artifact backup is missing for {path}: {backup_path}",
                    file=sys.stderr,
                )
                transaction_failed = True
                continue
            if _is_systemd_artifact(path, systemd_user_dir, gateway_service_name):
                restored_systemd = True
            try:
                status = restore_path_topology_from_backup(backup_path, path, restore_stage)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                status = 1
            if status != 0:
                print(f"ERROR: Failed to restore managed artifact {path}.", file=sys.stderr)
                transaction_failed = True
        else:
            if _is_systemd_artifact(path, systemd_user_dir, gateway_service_name):
                restored_systemd = True
            try:
                status = guarded_rm_rf(
                    path, f"removing newly installed managed artifact {path} during rollback"
                )
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                status = 1
            if status != 0:
                print(
                    f"ERROR: Failed to remove newly installed managed artifact {path} during "
                    "rollback.",
                    file=sys.stderr,
                )
                transaction_failed = True
    if restored_systemd:
        print(ARTIFACT_SYSTEMD_MARKER)
    if transaction_failed:
        print(_preserved_message(journal), file=sys.stderr)
        raise SilentFailure
    _with_lifecycle(journal, ROLLED_BACK)
    return restored_systemd


def _finalize_transaction(backup_dir: str, kind: str) -> None:
    if not backup_dir:
        if kind == UNIT_KIND:
            raise JournalError(
                "ERROR: Managed systemd transaction was not armed at deployment commit."
            )
        raise JournalError(
            "ERROR: Managed OpenClaw artifact transaction was not armed at deployment commit."
        )
    try:
        journal = _read_journal(backup_dir, kind)
    except JournalError:
        if not os.path.isdir(backup_dir):
            if kind == UNIT_KIND:
                raise JournalError(
                    "ERROR: Managed systemd backup directory is missing at deployment commit: "
                    f"{backup_dir}"
                ) from None
            raise JournalError(
                "ERROR: Managed OpenClaw artifact backup directory is missing at deployment "
                "commit: "
                f"{backup_dir}"
            ) from None
        raise
    _require_active(journal, "finalize")
    for index, path in enumerate(journal.paths):
        backup_path = os.path.join(journal.backup_dir, str(index))
        state = journal.states[index]
        if state == PRESENT:
            if not path_exists_or_symlink(backup_path):
                if kind == UNIT_KIND:
                    raise JournalError(
                        "ERROR: Managed systemd backup component is missing at deployment commit: "
                        f"{backup_path}"
                    )
                else:
                    raise JournalError(
                        "ERROR: Managed OpenClaw artifact backup component is missing at "
                        "deployment "
                        f"commit: {backup_path}"
                    )
        elif state == FAILED:
            if kind == UNIT_KIND:
                raise JournalError(
                    "ERROR: Managed systemd snapshot did not complete before deployment commit: "
                    f"{path}"
                )
            else:
                raise JournalError(
                    "ERROR: Managed OpenClaw artifact snapshot did not complete before deployment "
                    f"commit: {path}"
                )
        elif state != ABSENT:
            label = "systemd" if kind == UNIT_KIND else "OpenClaw artifact"
            raise JournalError(
                f"ERROR: Managed {label} snapshot state is invalid for {path}: {state}",
            )
    _with_lifecycle(journal, FINALIZED)


def finalize_unit_transaction(backup_dir: str) -> None:
    _finalize_transaction(backup_dir, UNIT_KIND)


def finalize_artifact_transaction(backup_dir: str) -> None:
    _finalize_transaction(backup_dir, ARTIFACT_KIND)


def cleanup_transaction(backup_dir: str, kind: str, phase: str) -> None:
    """Remove one transaction recovery directory with contract messages."""

    base_context = (
        f"removing managed systemd backup directory {backup_dir}"
        if kind == UNIT_KIND
        else f"removing managed OpenClaw artifact backup directory {backup_dir}"
    )
    context = base_context if phase == "committed" else f"{base_context} after rollback"
    try:
        status = guarded_rm_rf(
            backup_dir,
            context,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        status = 1
    if status == 0:
        return
    if kind == UNIT_KIND:
        print(
            f"ERROR: Failed to remove managed systemd backup directory {backup_dir}"
            f"{' after rollback.' if phase == 'rollback' else '.'}",
            file=sys.stderr,
        )
        print(f"Managed systemd recovery directory preserved at {backup_dir}", file=sys.stderr)
    else:
        print(
            f"ERROR: Failed to remove managed OpenClaw artifact backup directory {backup_dir}"
            f"{' after rollback.' if phase == 'rollback' else '.'}",
            file=sys.stderr,
        )
        print(
            f"Managed OpenClaw artifact recovery directory preserved at {backup_dir}",
            file=sys.stderr,
        )
    raise SilentFailure


def restore_local_config(backup: str, local_config: str, push_home: str) -> None:
    """Restore the local config through the guarded staged replacement path."""

    try:
        guard_destination_path_chain(
            push_home,
            f"creating local OpenClaw config rollback staging directory under {push_home}",
        )
        restore_stage_dir = tempfile.mkdtemp(prefix=".openclaw.rollback.", dir=push_home)
        guard_destination_path_chain(
            restore_stage_dir,
            f"created local OpenClaw config rollback staging directory {restore_stage_dir}",
        )
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, RuntimeError):
            print(str(exc), file=sys.stderr)
        _raise_local_restore_failure()
    restore_stage = os.path.join(restore_stage_dir, "openclaw.json")
    try:
        status = guarded_copy_path_topology_preserving_final_symlink_topology(
            backup,
            restore_stage,
            f"staging local OpenClaw config rollback file {restore_stage}",
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        status = 1
    if status != 0:
        print(
            f"ERROR: Failed to stage backup {backup} for rollback to {local_config}.",
            file=sys.stderr,
        )
        try:
            cleanup_status = guarded_rm_rf(
                restore_stage_dir,
                "removing local OpenClaw config rollback staging directory "
                f"{restore_stage_dir} after stage failure",
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            cleanup_status = 1
        if cleanup_status != 0:
            print(
                "ERROR: Failed to remove local OpenClaw config rollback staging directory "
                f"{restore_stage_dir} after stage failure.",
                file=sys.stderr,
            )
        _raise_local_restore_failure()
    try:
        status = guarded_mv_replace_preserving_final_symlink_topology(
            restore_stage,
            local_config,
            f"restoring local OpenClaw config {local_config} from rollback stage",
            ("-T", "-f"),
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        status = 1
    if status != 0:
        print(
            f"ERROR: Failed to atomically restore backup {backup} to {local_config} during "
            "rollback.",
            file=sys.stderr,
        )
        print(
            f"       Recoverable backup preserved at {backup}; staged rollback file preserved at "
            f"{restore_stage}.",
            file=sys.stderr,
        )
        _raise_local_restore_failure()
    try:
        status = guarded_rmdir(
            restore_stage_dir,
            "removing local OpenClaw config rollback staging directory "
            f"{restore_stage_dir} after restoring {local_config}",
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        status = 1
    if status != 0:
        print(
            "ERROR: Failed to remove local OpenClaw config rollback staging directory "
            f"{restore_stage_dir} after restoring {local_config}.",
            file=sys.stderr,
        )
        print(
            f"       Recoverable backup preserved at {backup}; rollback staging directory "
            f"preserved at {restore_stage_dir}.",
            file=sys.stderr,
        )
        _raise_local_restore_failure()


def _raise_local_restore_failure() -> None:
    raise SilentFailure


def validate_local_config(armed: bool, backup: str, local_config: str) -> None:
    if not armed:
        raise RuntimeError(
            "ERROR: Local OpenClaw config rollback was not armed at deployment commit."
        )
    if not backup or not path_exists_or_symlink(backup):
        raise RuntimeError(
            "ERROR: Local OpenClaw config backup is missing at deployment commit: "
            f"{backup or '<unset>'}"
        )
    if not os.path.isfile(local_config):
        raise RuntimeError(
            f"ERROR: Local OpenClaw config is missing at deployment commit: {local_config}"
        )


def final_systemd_reload_after_artifact_rollback(restored_systemd: bool) -> None:
    if not restored_systemd:
        return
    if not _run_daemon_reload():
        raise RuntimeError(
            "ERROR: Failed final user systemd daemon-reload after managed artifact rollback "
            "restored systemd files."
        )


def report_retained_recovery_paths(
    rollback_armed: bool,
    backup: str,
    unit_backup_dir: str,
    artifact_backup_dir: str,
    repo_config_preflight_dir: str,
) -> int:
    if rollback_armed and backup and path_exists_or_symlink(backup):
        print(f"Local OpenClaw config recoverable backup preserved at {backup}", file=sys.stderr)
    if unit_backup_dir and os.path.isdir(unit_backup_dir):
        print(f"Managed systemd recovery directory preserved at {unit_backup_dir}", file=sys.stderr)
    if artifact_backup_dir and os.path.isdir(artifact_backup_dir):
        print(
            f"Managed OpenClaw artifact recovery directory preserved at {artifact_backup_dir}",
            file=sys.stderr,
        )
    if repo_config_preflight_dir and os.path.isdir(repo_config_preflight_dir):
        print(
            f"Guarded repo OpenClaw config copy preserved at {repo_config_preflight_dir}",
            file=sys.stderr,
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for kind in (UNIT_KIND, ARTIFACT_KIND):
        begin_parser = subparsers.add_parser(f"begin-{kind}-tx")
        begin_parser.add_argument("backup_dir")
        begin_parser.add_argument("paths", nargs="*")
        snapshot_parser = subparsers.add_parser(f"snapshot-{kind}")
        snapshot_parser.add_argument("backup_dir")
        snapshot_parser.add_argument("path")
        rollback_parser = subparsers.add_parser(f"rollback-{kind}-tx")
        rollback_parser.add_argument("backup_dir")
        finalize_parser = subparsers.add_parser(f"finalize-{kind}-tx")
        finalize_parser.add_argument("backup_dir")

    cleanup_parser = subparsers.add_parser("cleanup-tx")
    cleanup_parser.add_argument("kind", choices=(UNIT_KIND, ARTIFACT_KIND))
    cleanup_parser.add_argument("phase")
    cleanup_parser.add_argument("backup_dir")

    reload_parser = subparsers.add_parser("final-systemd-reload-after-artifact-rollback")
    reload_parser.add_argument("restored_systemd", choices=("0", "1"))

    restore_parser = subparsers.add_parser("restore-local-config")
    restore_parser.add_argument("backup")
    restore_parser.add_argument("local_config")
    restore_parser.add_argument("push_home")

    validate_parser = subparsers.add_parser("validate-local-config")
    validate_parser.add_argument("armed", choices=("0", "1"))
    validate_parser.add_argument("backup")
    validate_parser.add_argument("local_config")

    report_parser = subparsers.add_parser("report-retained-recovery-paths")
    report_parser.add_argument("rollback_armed", choices=("0", "1"))
    report_parser.add_argument("backup")
    report_parser.add_argument("unit_backup_dir")
    report_parser.add_argument("artifact_backup_dir")
    report_parser.add_argument("repo_config_preflight_dir")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        command = args.command
        if command == "begin-unit-tx":
            return int(not begin_transaction(UNIT_KIND, args.backup_dir, args.paths))
        elif command == "begin-artifact-tx":
            begin_artifact_transaction(args.backup_dir)
        elif command == "snapshot-unit":
            snapshot_unit_path(args.backup_dir, args.path)
        elif command == "snapshot-artifact":
            snapshot_artifact_path(args.backup_dir, args.path)
        elif command == "rollback-unit-tx":
            rollback_unit_transaction(args.backup_dir)
        elif command == "rollback-artifact-tx":
            rollback_artifact_transaction(args.backup_dir)
        elif command == "finalize-unit-tx":
            finalize_unit_transaction(args.backup_dir)
        elif command == "finalize-artifact-tx":
            finalize_artifact_transaction(args.backup_dir)
        elif command == "cleanup-tx":
            cleanup_transaction(args.backup_dir, args.kind, args.phase)
        elif command == "final-systemd-reload-after-artifact-rollback":
            final_systemd_reload_after_artifact_rollback(args.restored_systemd == "1")
        elif command == "restore-local-config":
            restore_local_config(args.backup, args.local_config, args.push_home)
        elif command == "validate-local-config":
            validate_local_config(args.armed == "1", args.backup, args.local_config)
        elif command == "report-retained-recovery-paths":
            return report_retained_recovery_paths(
                args.rollback_armed == "1",
                args.backup,
                args.unit_backup_dir,
                args.artifact_backup_dir,
                args.repo_config_preflight_dir,
            )
        else:
            return 0
    except (JournalError, RuntimeError, OSError, KeyError) as exc:
        if isinstance(exc, SilentFailure):
            return 1
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
