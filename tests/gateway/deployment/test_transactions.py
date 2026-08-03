"""Fail-closed tests for deployment transaction journals."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from gateway.deployment import transactions

REPO_ROOT = Path(__file__).resolve().parents[3]
TRANSACTIONS_MODULE = "gateway.deployment.transactions"


def _run_transactions(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", TRANSACTIONS_MODULE, *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
    )


@pytest.mark.parametrize("kind", ["unit", "artifact"])
def test_begin_transaction_refuses_to_overwrite_existing_journal(tmp_path: Path, kind: str) -> None:
    recovery_dir = tmp_path / "recovery"
    recovery_dir.mkdir()
    journal = recovery_dir / "transaction.json"
    original = {"sentinel": kind}
    journal.write_text(json.dumps(original), encoding="utf-8")

    result = _run_transactions(
        [f"begin-{kind}-tx", "--", str(recovery_dir), str(tmp_path / "managed")]
    )

    assert result.returncode == 1
    assert "transaction journal already exists" in result.stderr
    assert json.loads(journal.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("kind", ["unit", "artifact"])
@pytest.mark.parametrize("journal_state", ["missing", "corrupt"])
def test_finalize_transaction_refuses_missing_or_corrupt_journal(
    tmp_path: Path, kind: str, journal_state: str
) -> None:
    recovery_dir = tmp_path / "recovery"
    recovery_dir.mkdir()
    journal = recovery_dir / "transaction.json"
    if journal_state == "corrupt":
        journal.write_text("{not-json", encoding="utf-8")

    result = _run_transactions([f"finalize-{kind}-tx", "--", str(recovery_dir)])

    assert result.returncode == 1
    assert "transaction journal" in result.stderr
    assert "refusing" in result.stderr


def _journal_payload(recovery_dir: Path) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((recovery_dir / "transaction.json").read_text(encoding="utf-8")),
    )


def test_snapshot_state_transitions_are_persisted(tmp_path: Path) -> None:
    recovery_dir = tmp_path / "unit-recovery"
    recovery_dir.mkdir()
    present = tmp_path / "present.conf"
    absent = tmp_path / "absent.conf"
    present.write_text("before\n", encoding="utf-8")

    transactions.begin_transaction(
        transactions.UNIT_KIND, str(recovery_dir), [str(present), str(absent)]
    )
    assert _journal_payload(recovery_dir)["lifecycle"] == transactions.ACTIVE
    assert _journal_payload(recovery_dir)["states"] == [transactions.FAILED, transactions.FAILED]

    transactions.snapshot_unit_path(str(recovery_dir), str(present))
    transactions.snapshot_unit_path(str(recovery_dir), str(absent))

    payload = _journal_payload(recovery_dir)
    assert payload["states"] == [transactions.PRESENT, transactions.ABSENT]
    assert (recovery_dir / "0").read_text(encoding="utf-8") == "before\n"


def test_artifact_snapshot_appends_and_deduplicates_paths(tmp_path: Path) -> None:
    recovery_dir = tmp_path / "artifact-recovery"
    recovery_dir.mkdir()
    artifact = tmp_path / "artifact"
    artifact.write_text("before\n", encoding="utf-8")

    transactions.begin_artifact_transaction(str(recovery_dir))
    transactions.snapshot_artifact_path(str(recovery_dir), str(artifact))
    transactions.snapshot_artifact_path(str(recovery_dir), str(artifact))

    payload = _journal_payload(recovery_dir)
    assert payload["paths"] == [str(artifact)]
    assert payload["states"] == [transactions.PRESENT]


def test_unit_rollback_restores_in_order_then_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery_dir = tmp_path / "recovery"
    recovery_dir.mkdir()
    first = tmp_path / "first.conf"
    second = tmp_path / "second.conf"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    transactions.begin_transaction(
        transactions.UNIT_KIND, str(recovery_dir), [str(first), str(second)]
    )
    transactions.snapshot_unit_path(str(recovery_dir), str(first))
    transactions.snapshot_unit_path(str(recovery_dir), str(second))
    events: list[str] = []

    def restore(backup: str, destination: str, stage: str) -> int:
        del backup, stage
        events.append(destination)
        return 0

    monkeypatch.setattr(transactions, "restore_path_topology_from_backup", restore)

    def reload() -> bool:
        events.append("reload")
        return True

    monkeypatch.setattr(transactions, "_run_daemon_reload", reload)

    transactions.rollback_unit_transaction(str(recovery_dir))

    assert events == [str(first), str(second), "reload"]
    assert _journal_payload(recovery_dir)["lifecycle"] == transactions.ROLLED_BACK


def test_artifact_rollback_restores_in_reverse_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery_dir = tmp_path / "recovery"
    recovery_dir.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    transactions.begin_artifact_transaction(str(recovery_dir))
    transactions.snapshot_artifact_path(str(recovery_dir), str(first))
    transactions.snapshot_artifact_path(str(recovery_dir), str(second))
    restored: list[str] = []

    def restore(backup: str, destination: str, stage: str) -> int:
        del backup, stage
        restored.append(destination)
        return 0

    monkeypatch.setattr(transactions, "restore_path_topology_from_backup", restore)

    assert (
        transactions.rollback_artifact_transaction(
            str(recovery_dir), str(tmp_path / "systemd"), "gateway.service"
        )
        is False
    )
    assert restored == [str(second), str(first)]
    assert _journal_payload(recovery_dir)["lifecycle"] == transactions.ROLLED_BACK


def test_restore_local_config_uses_noninteractive_mv_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / "backup.json"
    local_config = tmp_path / "openclaw.json"
    backup.write_text("backup\n", encoding="utf-8")
    local_config.write_text("published\n", encoding="utf-8")
    captured_options: list[tuple[str, ...]] = []

    def move(source: str, destination: str, context: str, options: tuple[str, ...]) -> int:
        del context
        captured_options.append(options)
        os.replace(source, destination)
        return 0

    monkeypatch.setattr(
        transactions,
        "guarded_mv_replace_preserving_final_symlink_topology",
        move,
    )

    transactions.restore_local_config(str(backup), str(local_config), str(tmp_path))

    assert captured_options == [("-T", "-f")]
    assert local_config.read_text(encoding="utf-8") == "backup\n"


@pytest.mark.parametrize("kind", [transactions.UNIT_KIND, transactions.ARTIFACT_KIND])
@pytest.mark.parametrize("phase", ["committed", "rollback"])
def test_cleanup_transaction_reports_phase_specific_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    phase: str,
) -> None:
    recovery_dir = tmp_path / f"{kind}-{phase}"
    recovery_dir.mkdir()
    monkeypatch.setattr(transactions, "guarded_rm_rf", lambda path, context: 1)

    with pytest.raises(transactions.SilentFailure):
        transactions.cleanup_transaction(str(recovery_dir), kind, phase)

    error = capsys.readouterr().err
    expected = " after rollback." if phase == "rollback" else "."
    assert "ERROR: Failed to remove managed" in error
    assert f"{recovery_dir}{expected}" in error


def test_report_retained_recovery_paths_returns_zero_and_reports_all_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = tmp_path / "openclaw.json.bak"
    backup.write_text("backup\n", encoding="utf-8")
    unit_dir = tmp_path / "unit-recovery"
    artifact_dir = tmp_path / "artifact-recovery"
    preflight_dir = tmp_path / "preflight"
    unit_dir.mkdir()
    artifact_dir.mkdir()
    preflight_dir.mkdir()

    assert (
        transactions.report_retained_recovery_paths(
            True,
            str(backup),
            str(unit_dir),
            str(artifact_dir),
            str(preflight_dir),
        )
        == 0
    )
    error = capsys.readouterr().err
    assert f"Local OpenClaw config recoverable backup preserved at {backup}" in error
    assert f"Managed systemd recovery directory preserved at {unit_dir}" in error
    assert f"Managed OpenClaw artifact recovery directory preserved at {artifact_dir}" in error
    assert f"Guarded repo OpenClaw config copy preserved at {preflight_dir}" in error


def test_snapshot_not_in_journal_refusal_is_loud(tmp_path: Path) -> None:
    recovery_dir = tmp_path / "recovery"
    recovery_dir.mkdir()
    transactions.begin_transaction(transactions.UNIT_KIND, str(recovery_dir), [])

    with pytest.raises(transactions.JournalError, match=r"^ERROR: "):
        transactions.snapshot_unit_path(str(recovery_dir), str(tmp_path / "not-journaled"))


def test_finalize_unit_reports_missing_backup_directory_exactly(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(
        transactions.JournalError,
        match=(
            rf"^ERROR: Managed systemd backup directory is missing at deployment commit: "
            rf"{missing}$"
        ),
    ):
        transactions.finalize_unit_transaction(str(missing))


def test_finalize_unit_reports_unarmed_empty_backup_directory() -> None:
    with pytest.raises(
        transactions.JournalError,
        match=r"^ERROR: Managed systemd transaction was not armed at deployment commit\.$",
    ):
        transactions.finalize_unit_transaction("")


def test_artifact_rollback_validates_journal_before_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery_dir = tmp_path / "recovery"
    recovery_dir.mkdir()
    monkeypatch.delenv("SYSTEMD_USER_DIR", raising=False)
    monkeypatch.delenv("GATEWAY_SERVICE_NAME", raising=False)
    result = _run_transactions(["rollback-artifact-tx", "--", str(recovery_dir)])

    assert result.returncode == 1
    assert "transaction journal is missing" in result.stderr
    assert "SYSTEMD_USER_DIR" not in result.stderr


def _make_empty_transaction(tmp_path: Path, kind: str) -> Path:
    recovery_dir = tmp_path / f"{kind}-recovery"
    recovery_dir.mkdir()
    if kind == transactions.UNIT_KIND:
        transactions.begin_transaction(kind, str(recovery_dir), [])
    else:
        transactions.begin_artifact_transaction(str(recovery_dir))
    return recovery_dir


def test_double_finalize_is_refused(tmp_path: Path) -> None:
    recovery_dir = _make_empty_transaction(tmp_path, transactions.UNIT_KIND)
    transactions.finalize_unit_transaction(str(recovery_dir))

    with pytest.raises(transactions.JournalError, match=r"^ERROR: "):
        transactions.finalize_unit_transaction(str(recovery_dir))


def test_rollback_after_finalize_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery_dir = _make_empty_transaction(tmp_path, transactions.UNIT_KIND)
    transactions.finalize_unit_transaction(str(recovery_dir))
    monkeypatch.setattr(transactions, "_run_daemon_reload", lambda: True)

    with pytest.raises(transactions.JournalError, match=r"^ERROR: "):
        transactions.rollback_unit_transaction(str(recovery_dir))


def test_finalize_after_rollback_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery_dir = _make_empty_transaction(tmp_path, transactions.UNIT_KIND)
    monkeypatch.setattr(transactions, "_run_daemon_reload", lambda: True)
    transactions.rollback_unit_transaction(str(recovery_dir))

    with pytest.raises(transactions.JournalError, match=r"^ERROR: "):
        transactions.finalize_unit_transaction(str(recovery_dir))


def test_double_rollback_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recovery_dir = _make_empty_transaction(tmp_path, transactions.UNIT_KIND)
    monkeypatch.setattr(transactions, "_run_daemon_reload", lambda: True)
    transactions.rollback_unit_transaction(str(recovery_dir))

    with pytest.raises(transactions.JournalError, match=r"^ERROR: "):
        transactions.rollback_unit_transaction(str(recovery_dir))
