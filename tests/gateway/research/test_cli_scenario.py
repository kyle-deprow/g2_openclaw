from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import Result
from gateway.cli import app
from gateway.research import cli as research_cli
from gateway.research.contracts import (
    Attempt,
    AttemptState,
    HypothesisSpec,
    ImplementationRecord,
    ReviewRecord,
)
from gateway.research.jobs import JobRecord, _starttime, cancel, cleanup_stage
from gateway.research.jobs import launch as jobs_launch
from gateway.research.store import ResearchStore
from gateway.research.wake import compose_wake
from typer.testing import CliRunner

runner = CliRunner()


def _call(root: Path, *args: str, expect: int = 0) -> Result:
    result = runner.invoke(app, ["research", *args, "--root", str(root)])
    assert result.exit_code == expect, f"{result.output}\n{result.exception!r}"
    return result


def _commit(source: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=source, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()


def _target(source: Path, run_dir: Path, *, sleep: float = 0.0) -> tuple[str, tuple[str, ...]]:
    source.chmod(0o755)
    path = source / f"target-{run_dir.parent.name}.py"
    path.write_text(
        "import pathlib, time\n"
        f"time.sleep({sleep})\n"
        f"pathlib.Path({str(run_dir / 'targets.json')!r}).write_text('{{}}')\n",
        encoding="utf-8",
    )
    commit = _commit(source, path.name)
    source.chmod(0o555)
    return commit, ("/usr/bin/python3", str(path))


def _submit_records(
    root: Path,
    attempt_id: str,
    commit: str,
    spec_sha256: str,
    targets: tuple[str, ...],
    verdict: str,
) -> None:
    implementation = ImplementationRecord(
        attempt_id,
        commit,
        targets,
        "/tmp/test-evidence.json",
        "reported-coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    impl_file = root / f"{attempt_id}-implementation.json"
    impl_file.write_text(implementation.to_json(), encoding="utf-8")
    _call(root, "implementation-submit", attempt_id, "--file", str(impl_file))
    review = ReviewRecord(
        attempt_id,
        commit,
        spec_sha256,
        verdict,
        ("needs retry",) if verdict == "FAIL" else (),
        "reported-reviewer",
        "reviewer-actual",
        "test-session",
        "2026-01-01T00:00:00Z",
    )
    review_file = root / f"{attempt_id}-review.json"
    review_file.write_text(review.to_json(), encoding="utf-8")
    _call(root, "review-submit", attempt_id, "--file", str(review_file))


def test_full_retry_finish_next_and_pause_resume_scenario(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, hypothesis = campaign
    root = store.root
    # Production init deliberately has no evaluator implementation pin.  The
    # scenario injects a test-pinned launcher so it can exercise lifecycle
    # transitions without asserting real Quantipy execution is available.
    real_launch = jobs_launch

    def injected_launch(
        attempt_dir: Path,
        worktree: Path,
        targets_argv: Sequence[str],
        evaluator_argv: Sequence[str],
        timeout_seconds: int | float,
        max_rss_mb: int,
        *,
        shared_python: Path | None = None,
        expected_commit: str | None = None,
        artifact_paths: dict[str, Path] | None = None,
        artifact_digests: dict[str, str] | None = None,
        evaluator_source: Path | None = None,
        evaluator_source_sha256: str | None = None,
        evaluator_implementation_pinned: bool = False,
        job_id: str | None = None,
    ) -> JobRecord:
        return real_launch(
            attempt_dir,
            worktree,
            targets_argv,
            evaluator_argv,
            timeout_seconds,
            max_rss_mb,
            shared_python=shared_python,
            expected_commit=expected_commit,
            artifact_paths=artifact_paths,
            artifact_digests=artifact_digests,
            evaluator_source=evaluator_source,
            evaluator_source_sha256=evaluator_source_sha256,
            evaluator_implementation_pinned=True,
            job_id=job_id,
        )

    monkeypatch.setattr(research_cli, "launch", injected_launch)
    early_spec = root / "early-spec.json"
    early_spec.write_text('{"early": true}', encoding="utf-8")
    _call(
        root,
        "hypothesis-create",
        "--title",
        "too early",
        "--spec-file",
        str(early_spec),
        "--panel",
        hypothesis.panel_path,
        "--receipt",
        hypothesis.receipt_path,
        "--eval-spec",
        hypothesis.evaluation_spec_path,
        "--base-commit",
        "a" * 40,
        expect=1,
    )
    _call(root, "hypothesis-freeze", hypothesis.hypothesis_id)
    (root / "hypotheses" / hypothesis.hypothesis_id / "spec.json").chmod(0o444)
    _call(root, "attempt-open", hypothesis.hypothesis_id, "--worktree", str(source))
    attempt = store.attempts_for(hypothesis.hypothesis_id)[0]
    commit, targets = _target(
        source, root / "hypotheses" / "H0001" / "attempts" / "H0001-A001" / "run"
    )
    _submit_records(root, attempt.attempt_id, commit, hypothesis.spec_sha256, targets, "FAIL")
    _call(root, "run", attempt.attempt_id, expect=1)
    _call(root, "attempt-close", attempt.attempt_id, "--decision", "RETRY", "--reason", "retry")

    _call(root, "attempt-open", hypothesis.hypothesis_id, "--worktree", str(source))
    attempt2 = store.attempts_for(hypothesis.hypothesis_id)[1]
    _call(root, "attempt-open", hypothesis.hypothesis_id, "--worktree", str(source), expect=1)
    commit2, targets2 = _target(
        source, root / "hypotheses" / "H0001" / "attempts" / "H0001-A002" / "run", sleep=0.2
    )
    _submit_records(root, attempt2.attempt_id, commit2, hypothesis.spec_sha256, targets2, "PASS")
    job_result = _call(root, "run", attempt2.attempt_id, "--no-wait")
    assert job_result.output.strip().startswith("accepted ")
    job_id = job_result.output.strip().splitlines()[-1].split()[-1]
    row = store.job_for(attempt2.attempt_id)
    assert row is not None and json.loads(row["payload_json"])["job_id"] == job_id
    payload = json.loads(row["payload_json"])
    os.kill(int(payload["worker_pid"]), 9)
    time.sleep(0.1)
    _call(root, "reconcile")
    assert store.get_attempt(attempt2.attempt_id).state.value == "RUN_FAILED"
    _call(root, "attempt-close", attempt2.attempt_id, "--decision", "RETRY", "--reason", "orphan")

    _call(root, "attempt-open", hypothesis.hypothesis_id, "--worktree", str(source))
    attempt3 = store.attempts_for(hypothesis.hypothesis_id)[2]
    commit3, targets3 = _target(
        source, root / "hypotheses" / "H0001" / "attempts" / "H0001-A003" / "run"
    )
    _submit_records(root, attempt3.attempt_id, commit3, hypothesis.spec_sha256, targets3, "PASS")
    _call(root, "run", attempt3.attempt_id, "--timeout-seconds", "20")
    assert store.get_attempt(attempt3.attempt_id).state.value == "RUN_SUCCEEDED"
    _call(root, "attempt-close", attempt3.attempt_id, "--decision", "FINISH", "--reason", "finish")
    _call(
        root,
        "hypothesis-decide",
        hypothesis.hypothesis_id,
        "--decision",
        "FINISHED",
        "--reason",
        "done",
    )

    spec_file = root / "h2-spec.json"
    spec_file.write_text('{"second": true}', encoding="utf-8")
    _call(
        root,
        "hypothesis-create",
        "--title",
        "second",
        "--spec-file",
        str(spec_file),
        "--panel",
        hypothesis.panel_path,
        "--receipt",
        hypothesis.receipt_path,
        "--eval-spec",
        hypothesis.evaluation_spec_path,
        "--base-commit",
        commit3,
    )
    _call(root, "hypothesis-freeze", "H0002")
    _call(root, "attempt-open", "H0002", "--worktree", str(source))
    attempt4 = store.attempts_for("H0002")[0]
    impl4 = ImplementationRecord(
        attempt4.attempt_id,
        commit3,
        (sys.executable, "-m", "module"),
        "/tmp/evidence",
        "coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    impl_file = root / "h2-impl.json"
    impl_file.write_text(impl4.to_json(), encoding="utf-8")
    _call(root, "implementation-submit", attempt4.attempt_id, "--file", str(impl_file))
    review4 = ReviewRecord(
        attempt4.attempt_id,
        commit3,
        store.get_hypothesis("H0002").spec_sha256,
        "FAIL",
        ("pause",),
        "reviewer",
        "actual",
        "sid",
        "2026-01-01T00:00:00Z",
    )
    review_file = root / "h2-review.json"
    review_file.write_text(review4.to_json(), encoding="utf-8")
    _call(root, "review-submit", attempt4.attempt_id, "--file", str(review_file))
    _call(
        root,
        "attempt-close",
        attempt4.attempt_id,
        "--decision",
        "PAUSE",
        "--reason",
        "operator pause",
    )
    assert compose_wake(store) is None
    _call(root, "resume", "--reason", "operator resume")
    assert compose_wake(store) is not None


def test_cli_run_refuses_head_change_after_review_pass(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    root = store.root
    _call(root, "hypothesis-freeze", hypothesis.hypothesis_id)
    (root / "hypotheses" / hypothesis.hypothesis_id / "spec.json").chmod(0o444)
    _call(root, "attempt-open", hypothesis.hypothesis_id, "--worktree", str(source))
    attempt = store.attempts_for(hypothesis.hypothesis_id)[0]
    commit, targets = _target(
        source, root / "hypotheses" / "H0001" / "attempts" / "H0001-A001" / "run"
    )
    _submit_records(root, attempt.attempt_id, commit, hypothesis.spec_sha256, targets, "PASS")
    source.chmod(0o755)
    (source / "tracked.txt").write_text("changed after review", encoding="utf-8")
    _commit(source, "unexpected post-review change")
    source.chmod(0o555)
    _call(root, "run", attempt.attempt_id, expect=1)
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.REVIEW_PASSED


def _ready_attempt(
    store: ResearchStore,
    source: Path,
    hypothesis: HypothesisSpec,
    *,
    sleep: float = 0.0,
) -> tuple[Attempt, str]:
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    commit, targets = _target(
        source,
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run",
        sleep=sleep,
    )
    _submit_records(store.root, attempt.attempt_id, commit, hypothesis.spec_sha256, targets, "PASS")
    return attempt, commit


def test_foreground_orphan_is_finalized_and_does_not_stall(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, hypothesis = campaign
    attempt, _commit_value = _ready_attempt(store, source, hypothesis)

    def fake_launch(attempt_dir: Path, *_args: object, **kwargs: object) -> JobRecord:
        job_id = kwargs.get("job_id")
        assert isinstance(job_id, str)
        return JobRecord(
            job_id,
            attempt.attempt_id,
            999999,
            1,
            str(attempt_dir / "run"),
        )

    monkeypatch.setattr(research_cli, "launch", fake_launch)
    _call(store.root, "run", attempt.attempt_id, expect=1)
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED
    assert "orphaned" in (store.get_attempt(attempt.attempt_id).run_outcome or "")


def test_orphan_recheck_rejects_identity_registered_before_lock(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, hypothesis = campaign
    attempt, _commit_value = _ready_attempt(store, source, hypothesis)
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    store.reserve_and_start_run(attempt.attempt_id, "job-race", run_dir)
    original_acquire = ResearchStore.acquire_run_lock
    switched = False

    def register_live_worker_before_lock(current_store: ResearchStore) -> None:
        nonlocal switched
        if current_store.root == store.root and not switched:
            switched = True
            starttime = _starttime(os.getpid())
            assert starttime is not None
            with current_store._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM jobs WHERE job_id=?", ("job-race",)
                ).fetchone()
                assert row is not None
                payload = json.loads(str(row[0]))
                payload["worker_pid"] = os.getpid()
                payload["worker_starttime"] = starttime
                text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                conn.execute(
                    "UPDATE jobs SET payload_json=?,payload_sha256=? WHERE job_id=?",
                    (text, hashlib.sha256(text.encode()).hexdigest(), "job-race"),
                )
        original_acquire(current_store)

    monkeypatch.setattr(ResearchStore, "acquire_run_lock", register_live_worker_before_lock)
    cleanup_called = False

    def must_not_cleanup(_job: JobRecord) -> bool:
        nonlocal cleanup_called
        cleanup_called = True
        return False

    monkeypatch.setattr(research_cli, "cleanup_stage", must_not_cleanup)
    _call(store.root, "reconcile")
    assert switched and not cleanup_called
    updated = store.get_attempt(attempt.attempt_id)
    assert updated.state == AttemptState.RUNNING
    assert updated.run_outcome is None


def test_orphan_reconcile_ignores_late_terminal_from_live_worker(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    attempt, _commit_value = _ready_attempt(store, source, hypothesis, sleep=10)
    with store._connect() as conn:
        conn.execute("UPDATE driver_config SET evaluator_implementation_pinned=1")
    config = store.config()
    implementation = ImplementationRecord.from_json(
        store.evidence(attempt.attempt_id, "implementation")
    )
    attempt_dir = (
        store.root / "hypotheses" / hypothesis.hypothesis_id / "attempts" / attempt.attempt_id
    )
    run_dir = attempt_dir / "run"
    (store.root / "hypotheses" / hypothesis.hypothesis_id / "spec.json").chmod(0o444)
    store.reserve_and_start_run(attempt.attempt_id, "job-live-orphan", run_dir)
    job = jobs_launch(
        attempt_dir,
        source,
        implementation.targets_argv,
        [
            str(config["evaluator"]),
            "research",
            "evaluate",
            "--panel",
            hypothesis.panel_path,
            "--receipt",
            hypothesis.receipt_path,
            "--spec",
            hypothesis.evaluation_spec_path,
            "--targets",
            str(run_dir / "targets.json"),
            "--out",
            str(run_dir / "out"),
        ],
        30,
        512,
        shared_python=Path(str(config["shared_python"])),
        expected_commit=attempt.commit,
        artifact_paths={
            "spec": store.root / "hypotheses" / hypothesis.hypothesis_id / "spec.json",
            "panel": Path(hypothesis.panel_path),
            "receipt": Path(hypothesis.receipt_path),
            "evaluation_spec": Path(hypothesis.evaluation_spec_path),
        },
        artifact_digests={
            "spec": hypothesis.spec_sha256,
            "panel": hypothesis.panel_sha256,
            "receipt": hypothesis.receipt_sha256,
            "evaluation_spec": hypothesis.evaluation_spec_sha256,
        },
        evaluator_source=Path(str(config["evaluator"])),
        evaluator_source_sha256=str(config["evaluator_source_sha256"]),
        evaluator_implementation_pinned=True,
        job_id="job-live-orphan",
    )
    store.update_job(json.loads(job.to_json()), attempt.attempt_id)
    try:
        active_stage = run_dir / "active-stage.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not active_stage.is_file():
            time.sleep(0.02)
        assert active_stage.is_file()

        # Make only the durable worker identity stale.  The real worker and
        # its target remain alive, so reconcile must clean the owned stage and
        # finalize the reservation before late worker evidence appears.
        with store._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()
            assert row is not None
            payload = json.loads(str(row[0]))
            payload["worker_starttime"] = int(payload["worker_starttime"]) + 1
            text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "UPDATE jobs SET payload_json=?,payload_sha256=? WHERE job_id=?",
                (text, hashlib.sha256(text.encode()).hexdigest(), job.job_id),
            )

        _call(store.root, "reconcile")
        failed = store.get_attempt(attempt.attempt_id)
        assert failed.state == AttemptState.RUN_FAILED
        first_outcome = failed.run_outcome
        assert first_outcome is not None and '"status":"orphaned"' in first_outcome
        terminal = Path(job.run_dir) / "terminal.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not terminal.is_file():
            time.sleep(0.02)
        assert terminal.is_file()

        _call(store.root, "reconcile")
        assert store.get_attempt(attempt.attempt_id).run_outcome == first_outcome
    finally:
        cleanup_stage(job)
        if _starttime(job.worker_pid) is not None:
            cancel(job)


def test_spawn_update_crash_is_reconciled_without_relaunch(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, hypothesis = campaign
    attempt, _commit_value = _ready_attempt(store, source, hypothesis)
    calls = 0

    def fake_launch(attempt_dir: Path, *_args: object, **kwargs: object) -> JobRecord:
        nonlocal calls
        calls += 1
        job_id = kwargs.get("job_id")
        assert isinstance(job_id, str)
        return JobRecord(
            job_id,
            attempt.attempt_id,
            999999,
            1,
            str(attempt_dir / "run"),
        )

    def crash_update(self: ResearchStore, job: object, attempt_id: str) -> None:
        raise RuntimeError("driver crashed after spawn")

    monkeypatch.setattr(research_cli, "launch", fake_launch)
    monkeypatch.setattr(ResearchStore, "update_job", crash_update)
    _call(store.root, "run", attempt.attempt_id, expect=1)
    assert calls == 1
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUNNING
    _call(store.root, "reconcile")
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED
    assert "orphaned" in (store.get_attempt(attempt.attempt_id).run_outcome or "")


def test_second_run_guard_refuses_without_spawning(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source, hypothesis = campaign
    attempt, _commit_value = _ready_attempt(store, source, hypothesis)
    store.reserve_and_start_run(attempt.attempt_id, "already-running", store.root / "run")
    spawned = False

    def must_not_launch(*args: object, **kwargs: object) -> object:
        nonlocal spawned
        spawned = True
        raise AssertionError("second run spawned a worker")

    monkeypatch.setattr(research_cli, "launch", must_not_launch)
    _call(store.root, "run", attempt.attempt_id, expect=1)
    assert not spawned
    assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUNNING


@pytest.mark.parametrize(
    "terminal_status", ["source_mutated", "cancelled", "containment_unavailable"]
)
def test_non_success_terminal_status_stays_failed_through_reconcile(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    terminal_status: str,
) -> None:
    store, source, hypothesis = campaign
    attempt, _commit_value = _ready_attempt(store, source, hypothesis)
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    store.reserve_and_start_run(attempt.attempt_id, "tampered-terminal", run_dir)
    (run_dir / "terminal.json").parent.mkdir(parents=True, exist_ok=True)
    (run_dir / "out").mkdir(parents=True, exist_ok=True)
    (run_dir / "out" / "result.json").write_text("{}", encoding="utf-8")
    (run_dir / "terminal.json").write_text(
        json.dumps(
            {
                "job_id": "tampered-terminal",
                "status": terminal_status,
                "evaluator_exit": 0,
                "targets_exit": 0,
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
            }
        ),
        encoding="utf-8",
    )
    _call(store.root, "reconcile")
    updated = store.get_attempt(attempt.attempt_id)
    assert updated.state == AttemptState.RUN_FAILED
    assert f'"status":"{terminal_status}"' in (updated.run_outcome or "")


def test_real_run_refuses_writable_worktree_without_bypass(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    attempt, _commit_value = _ready_attempt(store, source, hypothesis)
    source.chmod(0o755)
    _call(store.root, "run", attempt.attempt_id, expect=1)
    updated = store.get_attempt(attempt.attempt_id)
    assert updated.state == AttemptState.RUN_FAILED
    outcome = json.loads(updated.run_outcome or "{}")
    assert outcome["status"] == "containment_unavailable"
    assert (
        subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=source, text=True
        )
        == ""
    )


def test_owner_lock_does_not_block_run_or_cancel_lifecycle(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    store, source, hypothesis = campaign
    attempt, _commit_value = _ready_attempt(store, source, hypothesis, sleep=10)
    with store._connect() as conn:
        conn.execute("UPDATE driver_config SET evaluator_implementation_pinned=1")
    owner = ResearchStore(store.root)
    owner.acquire_owner_lock()
    command_env = {"PYTHONPATH": str(Path(__file__).resolve().parents[3])}
    run_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from gateway.cli import app; app()",
            "research",
            "run",
            attempt.attempt_id,
            "--root",
            str(store.root),
            "--timeout-seconds",
            "30",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=command_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        payload: dict[str, object] | None = None
        while time.monotonic() < deadline:
            row = store.job_for(attempt.attempt_id)
            if row is not None:
                value = json.loads(row["payload_json"])
                if int(value.get("worker_pid", 0)):
                    payload = value
                    break
            time.sleep(0.05)
        if payload is None:
            stdout, stderr = run_process.communicate(timeout=2)
            pytest.fail(
                f"run process did not register worker: rc={run_process.returncode}; "
                f"{stdout}; {stderr}"
            )
        cancel_result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from gateway.cli import app; app()",
                "research",
                "cancel",
                attempt.attempt_id,
                "--root",
                str(store.root),
            ],
            cwd=Path(__file__).resolve().parents[3],
            env=command_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert cancel_result.returncode == 0, cancel_result.stderr
        run_process.wait(timeout=20)
        assert store.get_attempt(attempt.attempt_id).state == AttemptState.RUN_FAILED
    finally:
        if run_process.poll() is None:
            run_process.kill()
            run_process.wait()
        owner.release_owner_lock()
