"""Typer commands for the bounded research driver."""

# Typer's public declaration syntax intentionally uses call expressions.
# ruff: noqa: B008

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import typer

from .contracts import (
    Attempt,
    AttemptDecision,
    AttemptState,
    HypothesisDecision,
    ImplementationRecord,
    ReviewRecord,
    RunOutcome,
)
from .jobs import JobError, JobRecord, attach, cleanup_stage, launch, new_job_id, preflight
from .jobs import cancel as cancel_job
from .store import ResearchStore, now_utc
from .wake import OpenClawWakeSender, compose_wake, deliver, poll_owner_turn

app = typer.Typer(help="Durable, bounded Quantipy research driver.")


def _root(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("--root must be an absolute path")
    return value


def _fail(exc: Exception) -> None:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)


@app.command("init")
def init(
    root: Path = typer.Option(..., "--root"),
    shared_python: Path = typer.Option(..., "--shared-python"),
    evaluator: Path = typer.Option(..., "--evaluator"),
) -> None:
    try:
        ResearchStore(_root(root)).configure(shared_python, evaluator)
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
    base_commit: str = typer.Option(..., "--base-commit"),
) -> None:
    try:
        spec = ResearchStore(_root(root)).create_hypothesis(
            title, spec_file, panel, receipt, eval_spec, base_commit
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
        int(payload["worker_pid"]),
        int(payload["worker_starttime"]),
        str(payload["run_dir"]),
        str(payload.get("state", "LAUNCHED")),
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


@app.command("run")
def run_command(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    timeout_seconds: int = typer.Option(7200, "--timeout-seconds"),
    max_rss_mb: int = typer.Option(8192, "--max-rss-mb"),
    no_wait: bool = typer.Option(False, "--no-wait"),
) -> None:
    store: ResearchStore | None = None
    try:
        store = ResearchStore(_root(root))
        attempt = store.get_attempt(attempt_id)
        implementation = ImplementationRecord.from_json(
            store.evidence(attempt_id, "implementation")
        )
        config = store.config()
        evaluator = Path(str(config["evaluator"]))
        run_dir = store.root / "hypotheses" / attempt.hypothesis_id / "attempts" / attempt_id
        hypothesis = store.get_hypothesis(attempt.hypothesis_id)
        evaluator_argv = [
            str(evaluator),
            "research",
            "evaluate",
            "--panel",
            hypothesis.panel_path,
            "--receipt",
            hypothesis.receipt_path,
            "--spec",
            hypothesis.evaluation_spec_path,
            "--targets",
            str(run_dir / "run" / "targets.json"),
            "--out",
            str(run_dir / "run" / "out"),
        ]
        # Cheap checks happen before the irreversible queue transition.
        preflight(Path(attempt.worktree_path), attempt.commit)
        store.acquire_run_lock()
        try:
            job_id = new_job_id()
            store.reserve_and_start_run(attempt_id, job_id, run_dir / "run")
            job = launch(
                run_dir,
                Path(attempt.worktree_path),
                implementation.targets_argv,
                evaluator_argv,
                timeout_seconds,
                max_rss_mb,
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
                evaluator_source=evaluator,
                evaluator_source_sha256=str(config["evaluator_source_sha256"]),
                evaluator_implementation_pinned=bool(config["evaluator_implementation_pinned"]),
                job_id=job_id,
            )
            store.update_job(json.loads(job.to_json()), attempt_id)
        finally:
            store.release_run_lock()
        typer.echo(f"accepted {job.job_id}")
        if no_wait:
            return
        while True:
            state = attach(job)
            if state == "ATTACHED":
                time.sleep(0.1)
                continue
            if state == "ORPHANED":
                outcome = RunOutcome(
                    attempt_id,
                    job.job_id,
                    -1,
                    False,
                    False,
                    False,
                    "orphaned",
                    "none",
                    "",
                    now_utc(),
                    now_utc(),
                    "orphaned",
                )
                _finish_orphan(store, job, outcome)
                raise JobError("orphaned")
            terminal = _completion_payload(job)
            if terminal is None:
                raise JobError("terminal evidence missing")
            outcome = _terminal_outcome(store, attempt_id, terminal)
            finished = _finish_if_running(store, attempt_id, outcome)
            typer.echo(finished.state.value)
            if finished.state != AttemptState.RUN_SUCCEEDED:
                raise JobError(f"run finished with status {outcome.status}")
            return
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
        for attempt in [
            item
            for spec in store.hypotheses()
            for item in store.attempts_for(spec.hypothesis_id)
            if item.state == AttemptState.RUNNING
        ]:
            row = store.job_for(attempt.attempt_id)
            if row is None:
                continue
            job = _recover_reserved_job(_job_from_row(row))
            state = attach(job)
            if state == "ORPHANED":
                _finish_orphan(
                    store,
                    job,
                    RunOutcome(
                        attempt.attempt_id,
                        job.job_id,
                        -1,
                        False,
                        False,
                        False,
                        "orphaned",
                        "none",
                        "",
                        now_utc(),
                        now_utc(),
                        "orphaned",
                    ),
                )
            elif state in {"EXITED", "TIMED_OUT", "CANCELLED"}:
                terminal = _completion_payload(job)
                if terminal is not None:
                    _finish_if_running(
                        store,
                        attempt.attempt_id,
                        _terminal_outcome(store, attempt.attempt_id, terminal),
                    )
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
        store.get_attempt(attempt_id)
        row = store.job_for(attempt_id)
        if row is None:
            raise JobError("no job for attempt")
        job = _job_from_row(row)
        cancel_job(job)
        terminal = _completion_payload(job)
        outcome = (
            _terminal_outcome(store, attempt_id, terminal)
            if terminal is not None
            else RunOutcome(
                attempt_id,
                job.job_id,
                -15,
                False,
                False,
                False,
                "cancelled",
                "none",
                "",
                now_utc(),
                now_utc(),
                "cancelled",
            )
        )
        _finish_if_running(store, attempt_id, outcome)
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
        data = {
            "campaign": store.campaign(),
            "evaluator_implementation_pin": "unavailable (P3b gate)",
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
            plan = compose_wake(store)
            if plan is not None:
                deliver(store, sender, plan, session_key)
            poll_owner_turn(store, sender)
            if once:
                return
            time.sleep(max(1, poll_seconds))
    except Exception as exc:
        _fail(exc)
    finally:
        if store is not None:
            store.release_owner_lock()
