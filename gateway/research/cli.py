"""Typer commands for the bounded research driver."""

# Typer's public declaration syntax intentionally uses call expressions.
# ruff: noqa: B008

from __future__ import annotations

import json
import math
import os
import time
from contextlib import suppress
from pathlib import Path

import typer

from .containment import runtime_pins_from_record
from .contracts import (
    Attempt,
    AttemptDecision,
    AttemptState,
    HypothesisDecision,
    ImplementationRecord,
    ReviewRecord,
    RunOutcome,
)
from .jobs import (
    JobError,
    JobRecord,
    attach,
    cleanup_stage,
    launch,
    lifecycle_lock,
    new_job_id,
    preflight,
)
from .jobs import cancel as cancel_job
from .store import OwnerLockHeld, ResearchStore, StoreConflict, now_utc
from .wake import OpenClawWakeSender, compose_wake, deliver, poll_owner_turn

app = typer.Typer(help="Durable, bounded Quantipy research driver.")
_SERVE_FATAL_EXIT = 78


def _root(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("--root must be an absolute path")
    return value


def _fail(exc: Exception) -> None:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)


def _fatal_serve_failure(store: ResearchStore | None, exc: Exception) -> None:
    """Pause visibly before terminating so a service supervisor cannot retry."""
    detail = f"{type(exc).__name__}: {exc}"
    if store is None:
        typer.echo(f"fatal serve iteration: {detail}", err=True)
        raise typer.Exit(code=_SERVE_FATAL_EXIT)
    try:
        store.pause_for_failure(detail)
    except Exception as pause_exc:
        typer.echo(
            f"fatal serve iteration: {detail}; failed to persist pause: "
            f"{type(pause_exc).__name__}: {pause_exc}",
            err=True,
        )
        raise typer.Exit(code=_SERVE_FATAL_EXIT) from pause_exc
    typer.echo(f"fatal serve iteration; campaign paused: {detail}", err=True)
    raise typer.Exit(code=_SERVE_FATAL_EXIT)


@app.command("init")
def init(
    root: Path = typer.Option(..., "--root"),
    shared_python: Path = typer.Option(..., "--shared-python"),
    evaluator: Path = typer.Option(..., "--evaluator"),
    snapshot_dir: Path = typer.Option(..., "--snapshot-dir"),
    universe: Path = typer.Option(..., "--universe"),
) -> None:
    try:
        ResearchStore(_root(root)).configure(shared_python, evaluator, snapshot_dir, universe)
        typer.echo(f"initialized research store: {root}")
    except Exception as exc:
        _fail(exc)


@app.command("hypothesis-create")
def hypothesis_create(
    root: Path = typer.Option(..., "--root"),
    title: str = typer.Option(..., "--title"),
    spec_file: Path = typer.Option(..., "--spec-file"),
    panel: Path = typer.Option(..., "--panel"),
    receipt: Path = typer.Option(..., "--receipt"),
    eval_spec: Path = typer.Option(..., "--eval-spec"),
    dividends: Path = typer.Option(..., "--dividends"),
    base_commit: str = typer.Option(..., "--base-commit"),
) -> None:
    try:
        spec = ResearchStore(_root(root)).create_hypothesis(
            title, spec_file, panel, receipt, eval_spec, base_commit, dividends=dividends
        )
        typer.echo(spec.hypothesis_id)
    except Exception as exc:
        _fail(exc)


@app.command("hypothesis-freeze")
def hypothesis_freeze(hypothesis_id: str, root: Path = typer.Option(..., "--root")) -> None:
    try:
        typer.echo(ResearchStore(_root(root)).freeze(hypothesis_id).state.value)
    except Exception as exc:
        _fail(exc)


@app.command("attempt-open")
def attempt_open(
    hypothesis_id: str,
    root: Path = typer.Option(..., "--root"),
    worktree: Path = typer.Option(..., "--worktree"),
) -> None:
    try:
        typer.echo(ResearchStore(_root(root)).open_attempt(hypothesis_id, worktree).attempt_id)
    except Exception as exc:
        _fail(exc)


@app.command("implementation-submit")
def implementation_submit(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    file: Path = typer.Option(..., "--file"),
) -> None:
    try:
        record = ImplementationRecord.from_json(file.read_text(encoding="utf-8"))
        typer.echo(ResearchStore(_root(root)).submit_implementation(attempt_id, record).state.value)
    except Exception as exc:
        _fail(exc)


@app.command("review-submit")
def review_submit(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    file: Path = typer.Option(..., "--file"),
) -> None:
    try:
        record = ReviewRecord.from_json(file.read_text(encoding="utf-8"))
        typer.echo(ResearchStore(_root(root)).submit_review(attempt_id, record).state.value)
    except Exception as exc:
        _fail(exc)


def _terminal_outcome(
    store: ResearchStore, attempt_id: str, terminal: dict[str, object]
) -> RunOutcome:
    attempt = store.get_attempt(attempt_id)
    result_path = Path(
        str(
            store.root
            / "hypotheses"
            / attempt.hypothesis_id
            / "attempts"
            / attempt_id
            / "run"
            / "evaluator-stage"
            / "out"
            / "result.json"
        )
    )
    result: dict[str, object] = {}
    if result_path.is_file():
        try:
            loaded = json.loads(result_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                result = loaded
        except json.JSONDecodeError:
            pass
    raw_exit = terminal.get("evaluator_exit", terminal.get("targets_exit", -1))
    exit_code = int(raw_exit) if isinstance(raw_exit, (int, float, str)) else -1
    raw_status = terminal.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status else "unknown"
    return RunOutcome(
        attempt_id=attempt_id,
        job_id=str(terminal.get("job_id", attempt.run_job_id or "")),
        exit_code=exit_code,
        compliant=bool(result.get("compliant", False)),
        zero_trade=bool(result.get("zero_trade", False)),
        metrics_available=bool(result.get("metrics_available", False)),
        acceptance_class=str(result.get("acceptance_class", "unknown")),
        earnings_provenance=str(result.get("earnings_provenance", "unknown")),
        result_path=str(result_path) if status == "succeeded" and result_path.is_file() else "",
        started_at=str(terminal.get("started_at", now_utc())),
        finished_at=str(terminal.get("finished_at", now_utc())),
        status=status,
    )


def _job_from_row(row: object) -> JobRecord:
    payload = json.loads(str(row["payload_json"]))  # type: ignore[index]
    return JobRecord(
        str(payload["job_id"]),
        str(payload["attempt_id"]),
        int(payload.get("worker_pid", 0)),
        int(payload.get("worker_starttime", 0)),
        str(payload["run_dir"]),
        str(payload.get("state", str(row["state"]))),  # type: ignore[index]
    )


def _recover_reserved_job(job: JobRecord) -> JobRecord:
    """Recover child identity written by launch if the DB update was interrupted."""
    if job.worker_pid:
        return job
    path = Path(job.run_dir) / "job.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["worker_pid"])
        starttime = int(payload["worker_starttime"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return job
    return JobRecord(job.job_id, job.attempt_id, pid, starttime, job.run_dir, "LAUNCHED")


def _completion_payload(job: JobRecord) -> dict[str, object] | None:
    for name in ("terminal.json", "cancel.json"):
        path = Path(job.run_dir) / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _finish_if_running(store: ResearchStore, attempt_id: str, outcome: RunOutcome) -> Attempt:
    store.acquire_run_lock()
    try:
        current = store.get_attempt(attempt_id)
        if current.state != AttemptState.RUNNING:
            return current
        return store.finish_run(attempt_id, outcome)
    finally:
        store.release_run_lock()


def _mark_job_state(store: ResearchStore, attempt_id: str, state: str) -> None:
    row = store.job_for(attempt_id)
    if row is None or str(row["state"]) == state:
        return
    payload = json.loads(str(row["payload_json"]))
    payload["state"] = state
    store.update_job(payload, attempt_id, event="job_finished")


def _finish_orphan(store: ResearchStore, job: JobRecord, outcome: RunOutcome) -> Attempt:
    store.acquire_run_lock()
    try:
        current = store.get_attempt(outcome.attempt_id)
        if current.state != AttemptState.RUNNING:
            return current
        row = store.job_for(outcome.attempt_id)
        if row is None:
            return current
        canonical = _recover_reserved_job(_job_from_row(row))
        if (
            canonical.attempt_id != outcome.attempt_id
            or canonical.job_id != outcome.job_id
            or attach(canonical) != "ORPHANED"
        ):
            return current
        cleanup_stage(canonical)
        current = store.get_attempt(outcome.attempt_id)
        if current.state != AttemptState.RUNNING:
            return current
        return store.finish_run(outcome.attempt_id, outcome)
    finally:
        store.release_run_lock()


def _write_terminal(run_dir: Path, outcome: RunOutcome, *, detail: str | None = None) -> None:
    """Write trusted host-side failure evidence for a job that never ran."""
    with lifecycle_lock(run_dir):
        # Cancellation's marker is the same per-job authority used by the
        # detached worker.  A host-side orphan record must never race in after
        # a cancellation request has won that authority.
        cancel_path = run_dir / "cancel.json"
        if cancel_path.exists() or cancel_path.is_symlink():
            return
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(run_dir / "terminal.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            payload: dict[str, object] = {
                "job_id": outcome.job_id,
                "attempt_id": outcome.attempt_id,
                "targets_exit": outcome.exit_code,
                "evaluator_exit": outcome.exit_code,
                "checks": [{"name": outcome.status, "ok": False}],
                "started_at": outcome.started_at,
                "finished_at": outcome.finished_at,
                "status": outcome.status,
            }
            if detail is not None:
                payload["error"] = detail
            stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            stream.flush()
            os.fsync(stream.fileno())


def _failure(attempt_id: str, job_id: str, status: str, *, exit_code: int = -1) -> RunOutcome:
    started = now_utc()
    return RunOutcome(
        attempt_id,
        job_id,
        exit_code,
        False,
        False,
        False,
        status,
        "none",
        "",
        started,
        now_utc(),
        status,
    )


def _host_execution_ready(store: ResearchStore, attempt_id: str) -> bool:
    del store, attempt_id
    return False


def _native_execution_ready(store: ResearchStore, attempt_id: str) -> bool:
    del store, attempt_id
    return False


def _budget_execution_ready(store: ResearchStore, attempt_id: str) -> bool:
    """Integration hook for campaign-level budget accounting (P3b-1b)."""
    del store, attempt_id
    return False


def _dispatch_queued_job(store: ResearchStore) -> str | None:
    """Claim and launch one queue item under the already-held owner authority."""
    if not store.owner_lock_held():
        raise JobError("host queue dispatch requires owner lock")
    if store.campaign()[0] != "ACTIVE":
        return None
    if store.running_job_rows():
        return None
    rows = store.queued_jobs()
    if not rows:
        return None
    row = rows[0]
    payload = json.loads(str(row["payload_json"]))
    attempt_id = str(row["attempt_id"])
    job_id = str(row["job_id"])

    def reject(status: str) -> str | None:
        """Finalize only the still-canonical queue row under the short lock."""
        try:
            store.acquire_run_lock()
        except OwnerLockHeld:
            return None
        try:
            current = store.get_attempt(attempt_id)
            canonical_row = store.job_for(attempt_id)
            if canonical_row is None or str(canonical_row["job_id"]) != job_id:
                return None
            if (
                current.state != AttemptState.RUN_QUEUED
                or current.run_job_id != job_id
                or str(canonical_row["state"]) != "QUEUED"
            ):
                return None
            canonical_job = _job_from_row(canonical_row)
            if _completion_payload(canonical_job) is not None:
                return None
            outcome = _failure(attempt_id, job_id, status)
            _write_terminal(Path(canonical_job.run_dir), outcome)
            try:
                store.finalize_queued_run(attempt_id, outcome, "EXITED")
            except StoreConflict:
                # A cancellation/finalization that won the canonical race is
                # expected; its terminal artifact remains authoritative.
                return None
            return status
        finally:
            store.release_run_lock()

    try:
        attempt = store.get_attempt(attempt_id)
        if attempt.state != AttemptState.RUN_QUEUED or attempt.run_job_id != job_id:
            return reject("queue_identity_mismatch")
        if attempt.review_verdict != "PASS" or attempt.commit is None:
            return reject("review_gate_unavailable")
        review = ReviewRecord.from_json(store.evidence(attempt_id, "review"))
        implementation = ImplementationRecord.from_json(
            store.evidence(attempt_id, "implementation")
        )
        if (
            review.verdict != "PASS"
            or review.commit != attempt.commit
            or review.spec_sha256 != attempt.review_spec_sha256
            or implementation.commit != attempt.commit
        ):
            return reject("review_gate_unavailable")
        hypothesis = store.get_hypothesis(attempt.hypothesis_id)
        expected_artifacts = {
            "spec": (
                str(store.root / "hypotheses" / hypothesis.hypothesis_id / "spec.json"),
                hypothesis.spec_sha256,
            ),
            "panel": (hypothesis.panel_path, hypothesis.panel_sha256),
            "receipt": (hypothesis.receipt_path, hypothesis.receipt_sha256),
            "evaluation_spec": (
                hypothesis.evaluation_spec_path,
                hypothesis.evaluation_spec_sha256,
            ),
            "dividends": (hypothesis.dividends_path, hypothesis.dividends_sha256),
        }
        queued_paths = payload.get("artifact_paths")
        queued_digests = payload.get("artifact_digests")
        if not isinstance(queued_paths, dict) or not isinstance(queued_digests, dict):
            return reject("input_binding_mismatch")
        if any(
            str(queued_paths.get(key)) != path or str(queued_digests.get(key)) != digest
            for key, (path, digest) in expected_artifacts.items()
        ):
            return reject("input_binding_mismatch")
        if (
            tuple(str(item) for item in payload.get("targets_argv", ()))
            != implementation.targets_argv
        ):
            return reject("implementation_pin_mismatch")
        if not _host_execution_ready(store, attempt_id):
            return reject("host_execution_unavailable")
        if not _native_execution_ready(store, attempt_id):
            return reject("native_execution_unavailable")
        if not _budget_execution_ready(store, attempt_id):
            return reject("budget_unavailable")
        config = store.config()
        pins = runtime_pins_from_record(dict(config))
        expected = {
            "shared_python": str(pins.shared_python),
            "shared_python_sha256": pins.shared_python_sha256,
            "shared_python_resolved": str(pins.resolved_python),
            "snapshot_dir": str(pins.snapshot_dir),
            "snapshot_sha256": pins.snapshot_sha256,
            "pyvenv_cfg": str(pins.pyvenv_cfg),
            "pyvenv_sha256": pins.pyvenv_sha256,
            "distribution_dir": str(pins.distribution_dir),
            "evaluator_source": str(pins.evaluator),
            "evaluator_source_sha256": pins.evaluator_sha256,
            "universe": str(pins.universe),
            "universe_sha256": pins.universe_sha256,
        }
        if any(str(payload.get(key)) != value for key, value in expected.items()):
            return reject("runtime_pin_mismatch")
        if str(payload.get("expected_commit")) != attempt.commit:
            return reject("implementation_pin_mismatch")
        try:
            preflight(Path(str(payload["worktree"])), attempt.commit)
        except JobError:
            return reject("source_mutated")
        store.acquire_run_lock()
        claimed = False
        try:
            # Re-read and claim only after all checks; no lock spans worker polling.
            if store.campaign()[0] != "ACTIVE":
                return None
            locked_attempt = store.get_attempt(attempt_id)
            locked_row = store.job_for(attempt_id)
            if (
                locked_row is None
                or str(locked_row["job_id"]) != job_id
                or str(locked_row["state"]) != "QUEUED"
                or locked_attempt.state != AttemptState.RUN_QUEUED
                or locked_attempt.run_job_id != job_id
            ):
                return None
            store.claim_queued_job(attempt_id, job_id)
            claimed = True
            artifact_paths = {
                key: Path(value) for key, value in dict(payload["artifact_paths"]).items()
            }
            artifact_digests = {
                key: str(value) for key, value in dict(payload["artifact_digests"]).items()
            }
            job = launch(
                Path(str(payload["run_dir"])).parent,
                Path(str(payload["worktree"])),
                tuple(str(item) for item in payload["targets_argv"]),
                float(payload["timeout_seconds"]),
                int(payload["max_rss_mb"]),
                shared_python=Path(str(payload["shared_python"])),
                expected_commit=str(payload["expected_commit"]),
                artifact_paths=artifact_paths,
                artifact_digests=artifact_digests,
                evaluator_source=Path(str(payload["evaluator_source"])),
                evaluator_source_sha256=str(payload["evaluator_source_sha256"]),
                configured_pins=pins,
                dividends_path=Path(str(payload["dividends_path"])),
                job_id=job_id,
            )
            updated_payload = {**payload, **json.loads(job.to_json()), "state": "LAUNCHED"}
            store.update_job(updated_payload, attempt_id)
        except (StoreConflict, OwnerLockHeld):
            return None
        except Exception as exc:
            if not claimed:
                raise
            outcome = _failure(attempt_id, job_id, "launch_failed")
            _write_terminal(Path(str(payload["run_dir"])), outcome, detail=str(exc))
            store.finish_run(attempt_id, outcome)
            store.update_job({**payload, "state": "EXITED"}, attempt_id)
            return "launch_failed"
        finally:
            store.release_run_lock()
        return job_id
    except OwnerLockHeld:
        # Another lifecycle operation currently owns the short lock.
        return None


def _reconcile_jobs(store: ResearchStore) -> None:
    """Reconcile every active job, including reservations left by a host crash."""
    for attempt in [
        item
        for spec in store.hypotheses()
        for item in store.attempts_for(spec.hypothesis_id)
        if item.state == AttemptState.RUNNING
    ]:
        row = store.job_for(attempt.attempt_id)
        if row is None:
            continue
        payload = json.loads(str(row["payload_json"]))
        job = _recover_reserved_job(_job_from_row(row))
        if job.worker_pid == 0:
            terminal = _completion_payload(job)
            if terminal is None:
                outcome = _failure(attempt.attempt_id, job.job_id, "interrupted_before_launch")
                _write_terminal(Path(job.run_dir), outcome)
                store.acquire_run_lock()
                try:
                    current = store.get_attempt(attempt.attempt_id)
                    if current.state == AttemptState.RUNNING:
                        store.finish_run(attempt.attempt_id, outcome)
                        store.update_job({**payload, "state": "EXITED"}, attempt.attempt_id)
                finally:
                    store.release_run_lock()
            else:
                _finish_if_running(
                    store,
                    attempt.attempt_id,
                    _terminal_outcome(store, attempt.attempt_id, terminal),
                )
                _mark_job_state(store, attempt.attempt_id, "EXITED")
            continue
        if (
            payload.get("worker_pid") != job.worker_pid
            or payload.get("worker_starttime") != job.worker_starttime
        ):
            store.update_job({**payload, **json.loads(job.to_json())}, attempt.attempt_id)
        state = attach(job)
        if state == "ORPHANED":
            finished = _finish_orphan(
                store, job, _failure(attempt.attempt_id, job.job_id, "orphaned")
            )
            if finished.state != AttemptState.RUNNING:
                _mark_job_state(store, attempt.attempt_id, "ORPHANED")
        elif state in {"EXITED", "TIMED_OUT", "CANCELLED"}:
            terminal = _completion_payload(job)
            if terminal is not None:
                _finish_if_running(
                    store,
                    attempt.attempt_id,
                    _terminal_outcome(store, attempt.attempt_id, terminal),
                )
                _mark_job_state(store, attempt.attempt_id, state)


@app.command("run")
def run_command(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    timeout_seconds: float = typer.Option(7200, "--timeout-seconds"),
    max_rss_mb: int = typer.Option(8192, "--max-rss-mb"),
    no_wait: bool = typer.Option(False, "--no-wait"),
    wait_seconds: float = typer.Option(5.0, "--wait-seconds"),
) -> None:
    store: ResearchStore | None = None
    try:
        store = ResearchStore(_root(root))
        if not math.isfinite(wait_seconds) or wait_seconds < 0:
            raise JobError("wait-seconds must be finite and non-negative")
        store.acquire_run_lock()
        try:
            job_id = new_job_id()
            attempt = store.get_attempt(attempt_id)
            run_dir = (
                store.root / "hypotheses" / attempt.hypothesis_id / "attempts" / attempt_id / "run"
            )
            store.queue_run_request(attempt_id, job_id, run_dir, timeout_seconds, max_rss_mb)
        finally:
            store.release_run_lock()
        typer.echo(f"accepted {job_id} state=QUEUED")
        if no_wait:
            return
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            current = store.get_attempt(attempt_id)
            if current.state in {AttemptState.RUN_SUCCEEDED, AttemptState.RUN_FAILED}:
                typer.echo(current.state.value)
                if current.state != AttemptState.RUN_SUCCEEDED:
                    raise JobError("run finished unsuccessfully")
                return
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        current = store.get_attempt(attempt_id)
        state = "queued" if current.state == AttemptState.RUN_QUEUED else "running"
        typer.echo(f"{state} {job_id}")
    except Exception as exc:
        _fail(exc)
    finally:
        if store is not None:
            store.release_run_lock()


@app.command("reconcile")
def reconcile(root: Path = typer.Option(..., "--root")) -> None:
    store: ResearchStore | None = None
    try:
        store = ResearchStore(_root(root))
        store.repair_projections()
        _reconcile_jobs(store)
        typer.echo("reconciled")
    except Exception as exc:
        _fail(exc)
    finally:
        if store is not None:
            store.release_run_lock()


@app.command("cancel")
def cancel(attempt_id: str, root: Path = typer.Option(..., "--root")) -> None:
    store: ResearchStore | None = None
    try:
        store = ResearchStore(_root(root))
        # Re-read the job while holding the short lifecycle lock.  In
        # particular, a LAUNCH_RESERVED snapshot must not cause pid 0 to be
        # signalled after a concurrent host dispatch has registered its child.
        store.acquire_run_lock()
        try:
            attempt = store.get_attempt(attempt_id)
            row = store.job_for(attempt_id)
            if row is None:
                raise JobError("no job for attempt")
            job = _job_from_row(row)
            if str(row["state"]) == "QUEUED":
                outcome = _failure(attempt_id, job.job_id, "cancelled", exit_code=-15)
                _write_terminal(Path(job.run_dir), outcome)
                store.finalize_queued_run(attempt_id, outcome, "CANCELLED")
                typer.echo("CANCELLED")
                return
            if attempt.state != AttemptState.RUNNING:
                raise JobError(f"job is not cancellable from {attempt.state.value}")
            # A reservation with no worker identity is settled without
            # signalling pid 0.  This is also crash-safe because row is fresh.
            if job.worker_pid == 0:
                outcome = _failure(attempt_id, job.job_id, "cancelled", exit_code=-15)
                _write_terminal(Path(job.run_dir), outcome)
                store.finish_run(attempt_id, outcome)
                store.update_job(
                    {**json.loads(str(row["payload_json"])), "state": "CANCELLED"},
                    attempt_id,
                )
                typer.echo("CANCELLED")
                return
        finally:
            store.release_run_lock()
        cancellation = cancel_job(job)
        terminal = _completion_payload(job)
        outcome = (
            _terminal_outcome(store, attempt_id, terminal)
            if terminal is not None
            else _failure(attempt_id, job.job_id, "cancelled", exit_code=-15)
        )
        _finish_if_running(store, attempt_id, outcome)
        _mark_job_state(store, attempt_id, attach(job))
        if cancellation == "ALREADY_FINISHED":
            typer.echo(f"ALREADY_FINISHED status={outcome.status}")
        else:
            typer.echo("CANCELLED")
    except Exception as exc:
        _fail(exc)
    finally:
        if store is not None:
            store.release_run_lock()


@app.command("attempt-close")
def attempt_close(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    decision: AttemptDecision = typer.Option(..., "--decision"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    try:
        typer.echo(
            ResearchStore(_root(root)).close_attempt(attempt_id, decision, reason).state.value
        )
    except Exception as exc:
        _fail(exc)


@app.command("hypothesis-decide")
def hypothesis_decide(
    hypothesis_id: str,
    root: Path = typer.Option(..., "--root"),
    decision: HypothesisDecision = typer.Option(..., "--decision"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    try:
        typer.echo(
            ResearchStore(_root(root))
            .decide_hypothesis(hypothesis_id, decision, reason)
            .state.value
        )
    except Exception as exc:
        _fail(exc)


@app.command("pause")
def pause(
    root: Path = typer.Option(..., "--root"), reason: str = typer.Option(..., "--reason")
) -> None:
    try:
        ResearchStore(_root(root)).pause(reason)
        typer.echo("PAUSED")
    except Exception as exc:
        _fail(exc)


@app.command("resume")
def resume(
    root: Path = typer.Option(..., "--root"), reason: str = typer.Option(..., "--reason")
) -> None:
    try:
        typer.echo(ResearchStore(_root(root)).resume(reason))
    except Exception as exc:
        _fail(exc)


@app.command("status")
def status(
    root: Path = typer.Option(..., "--root"), as_json: bool = typer.Option(False, "--json")
) -> None:
    try:
        store = ResearchStore(_root(root))
        specs = store.hypotheses()
        events = store.events()
        owner_failure = next(
            (event.to_json() for event in reversed(events) if event.kind == "owner_turn_failed"),
            None,
        )
        config = store.config()
        data = {
            "campaign": store.campaign(),
            "containment": "configured" if bool(config["containment_ready"]) else "unavailable",
            # A console-script digest and source snapshot prove the selected
            # bytes, but do not attest the evaluator's external implementation
            # review.  Keep that readiness distinction visible until P3b-1b.
            "evaluator_source_snapshot": (
                "configured" if bool(config["containment_ready"]) else "unavailable"
            ),
            "evaluator_implementation_pin": "unavailable (implementation attestation pending)",
            "hypotheses": [
                {
                    "hypothesis_id": h.hypothesis_id,
                    "state": h.state.value,
                    "attempts": [
                        {
                            "attempt_id": a.attempt_id,
                            "state": a.state.value,
                            "job": a.run_job_id,
                            "reported_coder_model": a.reported_coder_model,
                            "reported_reviewer_model": a.reported_reviewer_model,
                            "reported_reviewer_actual_model": a.reported_reviewer_actual_model,
                            "run_status": (
                                json.loads(a.run_outcome).get("status") if a.run_outcome else None
                            ),
                            "model_identity": "reported, unverified",
                        }
                        for a in store.attempts_for(h.hypothesis_id)
                    ],
                }
                for h in specs
            ],
            "last_event": events[-1].to_json() if events else None,
            "owner_turn_failed": owner_failure,
        }
        typer.echo(
            json.dumps(data, sort_keys=True)
            if as_json
            else json.dumps(data, indent=2, sort_keys=True)
        )
    except Exception as exc:
        _fail(exc)


@app.command("serve")
def serve(
    session_key: str = typer.Option(..., "--session-key"),
    root: Path = typer.Option(..., "--root"),
    poll_seconds: int = typer.Option(60, "--poll-seconds"),
    once: bool = typer.Option(False, "--once"),
) -> None:
    if not session_key:
        _fail(ValueError("--session-key is required"))
    store: ResearchStore | None = None
    try:
        store = ResearchStore(_root(root))
        sender = OpenClawWakeSender(
            os.environ.get("OPENCLAW_HOST", "127.0.0.1"),
            int(os.environ.get("OPENCLAW_PORT", "18789")),
            os.environ.get("OPENCLAW_GATEWAY_TOKEN", ""),
        )
        store.acquire_owner_lock()
        while True:
            # A concurrent cancel/reconcile owns the short lifecycle lock;
            # leave canonical state untouched and try on the next turn.
            with suppress(OwnerLockHeld):
                _reconcile_jobs(store)
            _dispatch_queued_job(store)
            plan = compose_wake(store)
            if plan is not None:
                deliver(store, sender, plan, session_key)
            poll_owner_turn(store, sender)
            if once:
                return
            time.sleep(max(1, poll_seconds))
    except OwnerLockHeld as exc:
        _fail(exc)
    except Exception as exc:
        _fatal_serve_failure(store, exc)
    finally:
        if store is not None:
            store.release_owner_lock()
