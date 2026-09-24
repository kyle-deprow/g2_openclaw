"""Typer commands for the bounded research driver."""

# Typer's public declaration syntax intentionally uses call expressions.
# ruff: noqa: B008

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import time
import zlib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import date
from pathlib import Path

import typer

from gateway.openclaw_client import OpenClawClient

from .admission import (
    AdmissionDecision,
    AdmissionReason,
    CampaignPolicy,
    EarningsCoverage,
    EarningsCoverageStatus,
    EvaluatorBounds,
    ExecutionCapability,
    ExposureLedger,
    ExposureRange,
    Instrument,
    InstrumentClass,
    ValidationReceipt,
    admit_hypothesis,
)
from .containment import runtime_pins_from_record
from .contracts import (
    Attempt,
    AttemptDecision,
    AttemptState,
    HypothesisDecision,
    ImplementationRecord,
    RunOutcome,
    RunPlan,
)
from .hypothesis import HypothesisDocument
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
from .provenance import validate_provenance_evidence
from .readiness import (
    budget_execution_ready,
    host_execution_ready,
    native_execution_ready,
    register_native_runtime_record,
)
from .review_evidence import (
    REVIEW_EFFORT,
    acknowledge_review,
    build_review_bundle,
    cancel_review,
    collect_review,
    reconcile_review,
    request_cancel,
    reserve_review,
)
from .store import OwnerLockHeld, ResearchStore, StoreConflict, now_utc
from .wake import (
    OpenClawWakeSender,
    OwnerPollUnavailable,
    compose_wake,
    deliver,
    poll_owner_turn,
)

app = typer.Typer(help="Durable, bounded Quantipy research driver.")
_SERVE_FATAL_EXIT = 78
_MAX_CONSECUTIVE_POLL_FAILURES = 30


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


@app.command("campaign-policy-set")
def campaign_policy_set(
    root: Path = typer.Option(..., "--root"),
    attempt_cap: int = typer.Option(..., "--attempt-cap"),
    wall_clock_cap_seconds: float | None = typer.Option(None, "--wall-clock-cap-seconds"),
    operator_reference: str = typer.Option(..., "--operator-reference"),
) -> None:
    try:
        policy = ResearchStore(_root(root)).set_campaign_policy(
            attempt_cap, wall_clock_cap_seconds, operator_reference
        )
        typer.echo(
            json.dumps(
                {
                    "attempt_cap": policy.attempt_cap,
                    "is_set": policy.is_set,
                    "wall_clock_cap_seconds": policy.wall_clock_cap_seconds,
                },
                sort_keys=True,
            )
        )
    except Exception as exc:
        _fail(exc)


@app.command("exposure-ledger-register")
def exposure_ledger_register(
    root: Path = typer.Option(..., "--root"),
    path: Path = typer.Option(..., "--path"),
    sha256: str = typer.Option(..., "--sha256"),
) -> None:
    try:
        registered_path, registered_sha = ResearchStore(_root(root)).register_exposure_ledger(
            path, sha256
        )
        typer.echo(json.dumps({"path": registered_path, "sha256": registered_sha}, sort_keys=True))
    except Exception as exc:
        _fail(exc)


@app.command("native-runtime-register")
def native_runtime_register(
    root: Path = typer.Option(..., "--root"),
    path: Path = typer.Option(..., "--path"),
    sha256: str = typer.Option(..., "--sha256"),
) -> None:
    """Register an official Codex rollout record for native readiness."""
    try:
        registered_path, registered_sha = register_native_runtime_record(root, path, sha256)
        typer.echo(json.dumps({"path": registered_path, "sha256": registered_sha}, sort_keys=True))
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
    evaluation_spec_set: list[Path] = typer.Option([], "--evaluation-spec-set"),
) -> None:
    try:
        if len(evaluation_spec_set) > 1:
            raise ValueError("--evaluation-spec-set accepts one manifest")
        spec = ResearchStore(_root(root)).create_hypothesis(
            title,
            spec_file,
            panel,
            receipt,
            eval_spec,
            base_commit,
            dividends=dividends,
            evaluation_spec_set=evaluation_spec_set[0] if evaluation_spec_set else None,
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
    store: ResearchStore | None = None
    try:
        store = ResearchStore(_root(root))
        try:
            decision = _admission_for_hypothesis(store, hypothesis_id)
        except _AdmissionInputError as exc:
            store.record_admission_failure(hypothesis_id, exc.reason, exc.detail)
            raise StoreConflict(f"admission refused: {exc.reason}: {exc.detail}") from exc
        if not decision.admitted:
            _record_admission_refusal(store, hypothesis_id, decision)
            reason = decision.reason.value if decision.reason is not None else "UNKNOWN"
            raise StoreConflict(f"admission refused: {reason}: {decision.detail or ''}".strip())
        typer.echo(store.open_attempt(hypothesis_id, worktree, admission=decision).attempt_id)
    except Exception as exc:
        _fail(exc)


@app.command("implementation-submit")
def implementation_submit(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    file: Path = typer.Option(..., "--file"),
    run_plan: Path = typer.Option(..., "--run-plan"),
    provenance_evidence: Path = typer.Option(..., "--provenance-evidence"),
) -> None:
    try:
        record = ImplementationRecord.from_json(file.read_text(encoding="utf-8"))
        plan = RunPlan.from_json(run_plan.read_text(encoding="utf-8"))
        store = ResearchStore(_root(root))
        provenance = validate_provenance_evidence(store, attempt_id, plan, provenance_evidence)
        typer.echo(
            store.submit_implementation(
                attempt_id,
                record,
                plan,
                containment_provenance=provenance,
            ).state.value
        )
    except Exception as exc:
        _fail(exc)


@app.command("review-bundle")
def review_bundle(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    bundle_dir: Path = typer.Option(..., "--bundle-dir"),
) -> None:
    try:
        typer.echo(build_review_bundle(ResearchStore(_root(root)), attempt_id, bundle_dir))
    except Exception as exc:
        _fail(exc)


@app.command("review-reserve")
def review_reserve(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    bundle_dir: Path = typer.Option(..., "--bundle-dir"),
    owner_key: str = typer.Option(..., "--owner-key"),
) -> None:
    try:
        reservation = reserve_review(ResearchStore(_root(root)), attempt_id, bundle_dir, owner_key)
        typer.echo(
            json.dumps(
                {
                    "runtime": "acp",
                    "agentId": "claude",
                    "mode": "run",
                    "thread": False,
                    "cwd": reservation.bundle_dir,
                    "model": "claude-opus-5",
                    "effort": REVIEW_EFFORT,
                    "label": reservation.label,
                },
                sort_keys=True,
            )
        )
    except Exception as exc:
        _fail(exc)


@app.command("review-ack")
def review_ack(
    attempt_id: str,
    child_session_key: str = typer.Option(..., "--child-session-key"),
    run_id: str = typer.Option(..., "--run-id"),
    mode: str = typer.Option(..., "--mode"),
    run_timeout_seconds: int | None = typer.Option(None, "--run-timeout-seconds"),
    root: Path = typer.Option(..., "--root"),
) -> None:
    try:
        ack = acknowledge_review(
            ResearchStore(_root(root)),
            attempt_id,
            child_session_key,
            run_id,
            mode,
            run_timeout_seconds,
        )
        typer.echo(ack.to_json())
    except Exception as exc:
        _fail(exc)


@app.command("review-reconcile")
def review_reconcile(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    core_database: Path = typer.Option(..., "--core-database"),
) -> None:
    try:
        ack = reconcile_review(ResearchStore(_root(root)), attempt_id, core_database)
        typer.echo(ack.to_json() if ack is not None else "pending")
    except Exception as exc:
        _fail(exc)


@app.command("review-collect")
def review_collect(
    attempt_id: str,
    root: Path = typer.Option(..., "--root"),
    core_database: Path = typer.Option(..., "--core-database"),
    acpx_sessions: Path = typer.Option(..., "--acpx-sessions"),
    claude_projects: Path = typer.Option(..., "--claude-projects"),
) -> None:
    try:
        typer.echo(
            collect_review(
                ResearchStore(_root(root)),
                attempt_id,
                core_database,
                acpx_sessions,
                claude_projects,
            ).state.value
        )
    except Exception as exc:
        _fail(exc)


@app.command("review-cancel")
def review_cancel(
    attempt_id: str,
    reason: str = typer.Option(..., "--reason"),
    root: Path = typer.Option(..., "--root"),
    core_database: Path = typer.Option(..., "--core-database"),
) -> None:
    async def request(task_id: str, cancel_reason: str) -> Mapping[str, object]:
        client = OpenClawClient(
            os.environ.get("OPENCLAW_HOST", "127.0.0.1"),
            int(os.environ.get("OPENCLAW_PORT", "18789")),
            os.environ.get("OPENCLAW_GATEWAY_TOKEN", ""),
        )
        return await request_cancel(client.request_once, task_id, cancel_reason)

    try:
        outcome = cancel_review(
            ResearchStore(_root(root)), attempt_id, reason, core_database, request
        )
        typer.echo(json.dumps({"status": outcome.status, "pending": outcome.pending}))
    except Exception as exc:
        _fail(exc)


class _AdmissionInputError(ValueError):
    """A stored admission input could not be verified at the wiring boundary."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _strict_object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _decode_panel_sessions(
    receipt_wire: dict[str, object], receipt: ValidationReceipt
) -> tuple[str, ...]:
    """Decode the trusted compact receipt artifact; never derive calendar dates."""
    raw_coverage = _strict_object(receipt_wire.get("coverage"), "receipt.coverage")
    expected_keys = {
        "compressed_size",
        "compression_ratio",
        "contract_version",
        "coverage_sha256",
        "encoding",
        "expanded_size",
        "payload",
    }
    if set(raw_coverage) != expected_keys:
        raise ValueError("receipt.coverage has unexpected keys")
    compressed_size = raw_coverage.get("compressed_size")
    expanded_size = raw_coverage.get("expanded_size")
    payload = raw_coverage.get("payload")
    if (
        type(compressed_size) is not int
        or type(expanded_size) is not int
        or compressed_size < 1
        or expanded_size < 1
        or not isinstance(payload, str)
        or raw_coverage.get("contract_version") != "price-coverage-compact-v1"
        or raw_coverage.get("encoding") != "canonical-json-zlib-base64-v1"
        or raw_coverage.get("coverage_sha256") != receipt.coverage_sha256
    ):
        raise ValueError("receipt coverage is not the pinned compact contract")
    try:
        compressed = base64.b64decode(payload.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("receipt coverage payload is not canonical base64") from exc
    if len(compressed) != compressed_size:
        raise ValueError("receipt coverage compressed size does not match payload")
    try:
        decompressor = zlib.decompressobj()
        expanded = decompressor.decompress(compressed, expanded_size + 1)
    except zlib.error as exc:
        raise ValueError("receipt coverage payload is not valid zlib") from exc
    if (
        len(expanded) != expanded_size
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ValueError("receipt coverage payload is incomplete or has trailing data")
    if hashlib.sha256(expanded).hexdigest() != receipt.coverage_sha256:
        raise ValueError("receipt coverage digest does not match expanded evidence")
    try:
        expanded_object = json.loads(expanded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("receipt coverage payload is not JSON") from exc
    canonical = json.dumps(expanded_object, sort_keys=True, separators=(",", ":")).encode()
    if expanded != canonical:
        raise ValueError("receipt coverage payload is not canonical JSON")
    coverage = _strict_object(expanded_object, "expanded receipt coverage")
    if coverage.get("contract_version") != "price-coverage-v1":
        raise ValueError("expanded receipt coverage contract is unsupported")
    tickers = coverage.get("tickers")
    if not isinstance(tickers, list) or not tickers:
        raise ValueError("expanded receipt coverage has no tickers")
    session_sets: list[tuple[str, ...]] = []
    for ticker_index, raw_ticker in enumerate(tickers):
        ticker = _strict_object(raw_ticker, f"expanded receipt tickers[{ticker_index}]")
        sessions = ticker.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            raise ValueError("expanded receipt ticker has no sessions")
        dates: list[str] = []
        for session_index, raw_session in enumerate(sessions):
            session = _strict_object(
                raw_session,
                f"expanded receipt tickers[{ticker_index}].sessions[{session_index}]",
            )
            date_text = session.get("session_date")
            if not isinstance(date_text, str):
                raise ValueError("expanded receipt session date is not text")
            try:
                parsed = date.fromisoformat(date_text)
            except ValueError as exc:
                raise ValueError("expanded receipt session date is not ISO") from exc
            if parsed.isoformat() != date_text:
                raise ValueError("expanded receipt session date is not canonical")
            if session.get("coverage_state") != "observed":
                raise ValueError("expanded receipt session is not observed")
            dates.append(date_text)
        if dates != sorted(set(dates)):
            raise ValueError("expanded receipt sessions are not unique and ordered")
        session_sets.append(tuple(dates))
    if any(item != session_sets[0] for item in session_sets[1:]):
        raise ValueError("expanded receipt tickers disagree on panel sessions")
    return session_sets[0]


def _parse_evaluator_bounds(
    path: Path, evaluation_spec_sha256: str, panel_sessions: tuple[str, ...]
) -> EvaluatorBounds:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        document = _strict_object(raw, "evaluator spec")
        allowed_keys = {
            "instruments",
            "start_session",
            "end_session",
            "costs",
            "holding",
            "execution",
            "max_gross_exposure",
            "long_only",
        }
        if not set(document).issubset(allowed_keys):
            raise ValueError("evaluator spec has unsupported non-cost bounds")
        instruments_raw = document.get("instruments")
        if not isinstance(instruments_raw, list) or not instruments_raw:
            raise ValueError("evaluator instruments missing")
        instruments: list[Instrument] = []
        for index, raw_instrument in enumerate(instruments_raw):
            item = _strict_object(raw_instrument, f"evaluator instruments[{index}]")
            instruments.append(
                Instrument(
                    str(item["ticker"]),
                    InstrumentClass(str(item["instrument_class"])),
                )
            )
        start = str(document["start_session"])
        end = str(document["end_session"])
        holding = _strict_object(document["holding"], "evaluator holding")
        max_holding = holding["max_sessions"]
        if type(max_holding) is not int:
            raise ValueError("evaluator holding.max_sessions must be an integer")
        execution = _strict_object(
            document.get(
                "execution",
                {
                    "decision_at": "regular_close",
                    "fill_at": "next_regular_open",
                },
            ),
            "evaluator execution",
        )
        if not set(holding).issubset({"max_sessions", "exit_at"}):
            raise ValueError("evaluator holding has unsupported bounds")
        if not set(execution).issubset({"decision_at", "fill_at"}):
            raise ValueError("evaluator execution has unsupported bounds")
        costs = document.get("costs")
        if costs is not None:
            costs_object = _strict_object(costs, "evaluator costs")
            if not set(costs_object).issubset(
                {"half_spread_bps", "slippage_bps", "commission_bps"}
            ):
                raise ValueError("evaluator costs has unsupported fields")
        max_gross_exposure = document.get("max_gross_exposure", 1.0)
        long_only = document.get("long_only", True)
        if (
            isinstance(max_gross_exposure, bool)
            or not isinstance(max_gross_exposure, (int, float))
            or not math.isfinite(float(max_gross_exposure))
            or not isinstance(long_only, bool)
        ):
            raise ValueError("evaluator execution bounds are malformed")
        return EvaluatorBounds(
            start,
            end,
            tuple(instruments),
            evaluation_spec_sha256,
            max_holding,
            EarningsCoverage(EarningsCoverageStatus.UNAVAILABLE),
            panel_sessions,
            str(holding.get("exit_at", "session_close")),
            str(execution.get("decision_at", "regular_close")),
            str(execution.get("fill_at", "next_regular_open")),
            float(max_gross_exposure),
            long_only,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise _AdmissionInputError(
            "EVALUATION_SPEC_DIGEST_MISMATCH", f"invalid evaluator bounds: {exc}"
        ) from exc


def _parse_exposure_ledger(store: ResearchStore) -> ExposureLedger | None:
    registration = store.exposure_ledger_registration()
    if registration is None:
        return None
    path_text, registered_sha = registration
    try:
        path = Path(path_text)
        raw_bytes = path.read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != registered_sha:
            return ExposureLedger((), False, None, "")
        raw = json.loads(raw_bytes.decode("utf-8"))
        data = _strict_object(raw, "exposure ledger")
        if set(data) != {"history_unknown", "known_exposed_ranges", "trial_count"}:
            raise ValueError("exposure ledger has unexpected keys")
        ranges = data["known_exposed_ranges"]
        if not isinstance(ranges, list):
            raise ValueError("exposure ledger ranges must be an array")
        typed_ranges_list: list[ExposureRange] = []
        for item in ranges:
            raw_range = _strict_object(item, "exposure range")
            if set(raw_range) != {"start", "end"}:
                raise ValueError("exposure range has unexpected keys")
            typed_ranges_list.append(ExposureRange(str(raw_range["start"]), str(raw_range["end"])))
        typed_ranges = tuple(typed_ranges_list)
        history_unknown = data["history_unknown"]
        trial_count = data["trial_count"]
        if not isinstance(history_unknown, bool) or (
            trial_count is not None and type(trial_count) is not int
        ):
            raise ValueError("exposure ledger scalar fields are malformed")
        unsigned = ExposureLedger(typed_ranges, history_unknown, trial_count, "")
        return replace(unsigned, ledger_sha256=unsigned.computed_sha256)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        # An invalid artifact must never inherit the digest of a real empty
        # ledger; the empty string is intentionally unmatchable.
        return ExposureLedger((), False, None, "")


def _same_evaluator_bounds(left: EvaluatorBounds, right: EvaluatorBounds) -> bool:
    """Compare every admission bound except the explicitly cost-bearing spec."""
    return (
        left.panel_start == right.panel_start
        and left.panel_end == right.panel_end
        and left.instruments == right.instruments
        and left.max_holding_sessions == right.max_holding_sessions
        and left.panel_sessions == right.panel_sessions
        and left.holding_exit_at == right.holding_exit_at
        and left.execution_decision_at == right.execution_decision_at
        and left.execution_fill_at == right.execution_fill_at
        and left.max_gross_exposure == right.max_gross_exposure
        and left.long_only == right.long_only
    )


def _admission_for_hypothesis(store: ResearchStore, hypothesis_id: str) -> AdmissionDecision:
    hypothesis_spec = store.get_hypothesis(hypothesis_id)
    try:
        hypothesis = HypothesisDocument.from_json(hypothesis_spec.spec_json)
    except ValueError as exc:
        raise _AdmissionInputError("HYPOTHESIS_SPEC_MISMATCH", str(exc)) from exc
    receipt_path = Path(hypothesis_spec.receipt_path)
    try:
        receipt_bytes = receipt_path.read_bytes()
    except OSError as exc:
        raise _AdmissionInputError("RECEIPT_DIGEST_MISMATCH", str(exc)) from exc
    receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
    if receipt_sha != hypothesis_spec.receipt_sha256:
        raise _AdmissionInputError("RECEIPT_DIGEST_MISMATCH", "stored receipt digest differs")
    try:
        receipt_wire = _strict_object(json.loads(receipt_bytes.decode("utf-8")), "receipt")
        receipt = ValidationReceipt.from_wire(
            receipt_wire,
            acceptance_class="wire",
            receipt_sha256=receipt_sha,
        )
        panel_sessions = _decode_panel_sessions(receipt_wire, receipt)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _AdmissionInputError("RECEIPT_REJECTED", str(exc)) from exc
    evaluation_path = Path(hypothesis_spec.evaluation_spec_path)
    try:
        evaluation_bytes = evaluation_path.read_bytes()
    except OSError as exc:
        raise _AdmissionInputError("EVALUATION_SPEC_DIGEST_MISMATCH", str(exc)) from exc
    if hashlib.sha256(evaluation_bytes).hexdigest() != hypothesis_spec.evaluation_spec_sha256:
        raise _AdmissionInputError(
            "EVALUATION_SPEC_DIGEST_MISMATCH", "stored evaluator spec digest differs"
        )
    bounds = _parse_evaluator_bounds(
        evaluation_path, hypothesis_spec.evaluation_spec_sha256, panel_sessions
    )
    try:
        spec_set = store.evaluation_spec_set(hypothesis_id)
    except (StoreConflict, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise _AdmissionInputError("EVALUATION_SPEC_SET_REJECTED", str(exc)) from exc
    spec_set_digest = hashlib.sha256(spec_set.to_json().encode("utf-8")).hexdigest()
    primary = next(entry for entry in spec_set.specs if entry.spec_id == spec_set.primary_spec_id)
    if (
        primary.path != hypothesis_spec.evaluation_spec_path
        or primary.sha256 != hypothesis_spec.evaluation_spec_sha256
    ):
        raise _AdmissionInputError(
            "EVALUATION_SPEC_DIGEST_MISMATCH", "primary spec set entry differs from hypothesis"
        )
    for entry in spec_set.specs:
        entry_path = Path(entry.path)
        if (
            entry_path.is_symlink()
            or not entry_path.is_file()
            or hashlib.sha256(entry_path.read_bytes()).hexdigest() != entry.sha256
        ):
            raise _AdmissionInputError(
                "EVALUATION_SPEC_DIGEST_MISMATCH",
                f"evaluation spec {entry.spec_id} bytes differ from its declared digest",
            )
        candidate = _parse_evaluator_bounds(entry_path, entry.sha256, panel_sessions)
        if not _same_evaluator_bounds(bounds, candidate):
            raise _AdmissionInputError(
                "EVALUATION_SPEC_DIGEST_MISMATCH",
                f"evaluation spec {entry.spec_id} changes a non-cost bound",
            )
    try:
        policy = store.campaign_policy()
    except StoreConflict as exc:
        policy = CampaignPolicy(False)
        store.record_admission_failure(hypothesis_id, "CAMPAIGN_POLICY_UNSET", str(exc))
    return admit_hypothesis(
        hypothesis,
        hypothesis_spec,
        receipt,
        _parse_exposure_ledger(store),
        bounds,
        policy,
        ExecutionCapability(frozenset({"panel"})),
        evaluation_spec_set_sha256=spec_set_digest,
    )


def _record_admission_refusal(
    store: ResearchStore,
    hypothesis_id: str,
    decision: AdmissionDecision,
    *,
    attempt_id: str | None = None,
) -> None:
    store.record_admission_refusal(hypothesis_id, decision, attempt_id=attempt_id)
    if decision.reason is AdmissionReason.CAMPAIGN_POLICY_UNSET:
        store.pause(decision.detail or "campaign remains paused until policy is set")


def _host_terminal_outcome(
    attempt_id: str, attempt: Attempt, terminal: dict[str, object]
) -> RunOutcome:
    raw_exit = terminal.get("evaluator_exit", terminal.get("targets_exit", -1))
    exit_code = int(raw_exit) if isinstance(raw_exit, (int, float, str)) else -1
    raw_status = terminal.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status else "unknown"
    return RunOutcome(
        attempt_id=attempt_id,
        job_id=str(terminal.get("job_id", attempt.run_job_id or "")),
        exit_code=exit_code,
        compliant=False,
        zero_trade=False,
        metrics_available=False,
        acceptance_class=status,
        earnings_provenance="none",
        result_path="",
        started_at=str(terminal.get("started_at", now_utc())),
        finished_at=str(terminal.get("finished_at", now_utc())),
        status=status,
    )


def _run_evidence_mismatch_outcome(
    attempt_id: str, attempt: Attempt, terminal: dict[str, object]
) -> RunOutcome:
    return RunOutcome(
        attempt_id=attempt_id,
        job_id=str(terminal.get("job_id", attempt.run_job_id or "")),
        exit_code=-1,
        compliant=False,
        zero_trade=False,
        metrics_available=False,
        acceptance_class="unknown",
        earnings_provenance="unknown",
        result_path="",
        started_at=str(terminal.get("started_at", now_utc())),
        finished_at=str(terminal.get("finished_at", now_utc())),
        status="run_evidence_mismatch",
    )


def _regular_file_sha256(path: Path) -> str | None:
    """Digest one canonical regular file without following a symlink."""
    try:
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _scenario_result_path(run_dir: Path, scenario_id: str) -> Path:
    return run_dir / "scenarios" / scenario_id / "evaluator-stage" / "out" / "result.json"


def _validate_scenario_result(
    run_dir: Path,
    scenario_id: str,
    scenario: dict[str, object],
    *,
    expected_spec_id: str | None = None,
    expected_spec_sha256: str | None = None,
    require_complete: bool,
) -> tuple[bool, Path | None]:
    """Validate a worker scenario binding and its canonical result file."""
    if require_complete and (
        scenario.get("spec_id") != expected_spec_id
        or scenario.get("spec_sha256") != expected_spec_sha256
    ):
        return False, None
    if not require_complete and "result_path" not in scenario and "result_sha256" not in scenario:
        return True, None
    raw_path = scenario.get("result_path")
    expected_digest = scenario.get("result_sha256")
    if not isinstance(raw_path, str) or not isinstance(expected_digest, str):
        return False, None
    canonical = _scenario_result_path(run_dir, scenario_id)
    if Path(raw_path) != canonical or _regular_file_sha256(canonical) != expected_digest:
        return False, None
    return True, canonical


def _validate_worker_success_evidence(
    run_dir: Path,
    plan: RunPlan,
    evidence: dict[str, object],
    scenarios: dict[str, object],
) -> tuple[bool, Path | None]:
    """Validate complete worker evidence against the immutable run plan."""
    scenario_ids = tuple(item.scenario_id for item in plan.scenarios)
    completed = evidence.get("completed_scenarios")
    if not isinstance(completed, list) or tuple(completed) != scenario_ids:
        return False, None
    if set(scenarios) != set(scenario_ids):
        return False, None
    primary_path: Path | None = None
    for item in plan.scenarios:
        scenario = scenarios.get(item.scenario_id)
        if not isinstance(scenario, dict) or scenario.get("status") != "succeeded":
            return False, None
        valid, result_path = _validate_scenario_result(
            run_dir,
            item.scenario_id,
            scenario,
            expected_spec_id=item.spec_id,
            expected_spec_sha256=item.evaluation_spec_sha256,
            require_complete=True,
        )
        if not valid or result_path is None:
            return False, None
        if item.scenario_id == plan.primary_scenario_id:
            primary_path = result_path

    validated_inputs = run_dir / "validated-inputs.json"
    if not isinstance(evidence.get("validated_inputs_sha256"), str) or _regular_file_sha256(
        validated_inputs
    ) != evidence.get("validated_inputs_sha256"):
        return False, None

    analysis = evidence.get("analysis")
    analysis_dir = run_dir / "analysis-stage"
    artifacts = tuple(plan.analysis.artifacts)
    if (
        not isinstance(analysis, dict)
        or set(analysis) != set(artifacts)
        or analysis_dir.is_symlink()
        or not analysis_dir.is_dir()
        or analysis_dir.resolve() != analysis_dir
    ):
        return False, None
    for relative in artifacts:
        digest = analysis.get(relative)
        path = analysis_dir / relative
        if not isinstance(digest, str) or _regular_file_sha256(path) != digest:
            return False, None

    checks = evidence.get("checks")
    if not isinstance(checks, list) or any(
        not isinstance(check, dict) or check.get("ok") is not True for check in checks
    ):
        return False, None
    return primary_path is not None, primary_path


def _terminal_outcome(
    store: ResearchStore,
    attempt_id: str,
    terminal: dict[str, object],
    *,
    origin: str | None = None,
) -> RunOutcome:
    attempt = store.get_attempt(attempt_id)
    raw_status = terminal.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status else "unknown"
    if (
        origin == "cancel.json"
        or "worker_pid" not in terminal
        or (origin is None and status in {"cancelled", "cancelling"})
    ):
        return _host_terminal_outcome(attempt_id, attempt, terminal)
    try:
        stored_plan = RunPlan.from_json(store.evidence(attempt_id, "run_plan"))
    except (StoreConflict, TypeError, ValueError, json.JSONDecodeError):
        return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
    expected_plan_digest = hashlib.sha256(stored_plan.to_json().encode()).hexdigest()
    expected_job_id = attempt.run_job_id
    attempt_dir = store.root / "hypotheses" / attempt.hypothesis_id / "attempts" / attempt_id
    run_dir = attempt_dir / "run"
    run_evidence_path = run_dir / "run-evidence.json"
    for key, expected in (
        ("attempt_id", attempt_id),
        ("job_id", expected_job_id),
        ("run_plan_sha256", expected_plan_digest),
        ("evaluation_spec_set_sha256", stored_plan.evaluation_spec_set_sha256),
    ):
        if key in terminal and terminal.get(key) != expected:
            return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
    result: dict[str, object] = {}
    terminal_evidence_digest = terminal.get("run_evidence_sha256")
    if (
        not isinstance(terminal_evidence_digest, str)
        or _regular_file_sha256(run_evidence_path) != terminal_evidence_digest
    ):
        return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
    try:
        loaded = json.loads(run_evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
    if not isinstance(loaded, dict):
        return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
    if (
        loaded.get("attempt_id") != attempt_id
        or loaded.get("job_id") != expected_job_id
        or loaded.get("run_plan_sha256") != expected_plan_digest
        or loaded.get("evaluation_spec_set_sha256") != stored_plan.evaluation_spec_set_sha256
        or loaded.get("status") != status
        or loaded.get("primary_scenario_id") != stored_plan.primary_scenario_id
    ):
        return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
    raw_scenarios = loaded.get("scenarios")
    if not isinstance(raw_scenarios, dict):
        return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
    primary_data = raw_scenarios.get(stored_plan.primary_scenario_id)
    primary_path: Path | None = None
    if isinstance(primary_data, dict):
        primary_plan = next(
            item
            for item in stored_plan.scenarios
            if item.scenario_id == stored_plan.primary_scenario_id
        )
        valid, primary_path = _validate_scenario_result(
            run_dir,
            stored_plan.primary_scenario_id,
            primary_data,
            expected_spec_id=primary_plan.spec_id if status == "succeeded" else None,
            expected_spec_sha256=(
                primary_plan.evaluation_spec_sha256 if status == "succeeded" else None
            ),
            require_complete=status == "succeeded",
        )
        if not valid:
            return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
    elif status == "succeeded":
        return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
    if status == "succeeded":
        valid, primary_path = _validate_worker_success_evidence(
            run_dir, stored_plan, loaded, raw_scenarios
        )
        if not valid or primary_path is None:
            return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
        try:
            decoded = json.loads(primary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
        if not isinstance(decoded, dict):
            return _run_evidence_mismatch_outcome(attempt_id, attempt, terminal)
        result = decoded
    raw_exit = terminal.get("evaluator_exit", terminal.get("targets_exit", -1))
    exit_code = int(raw_exit) if isinstance(raw_exit, (int, float, str)) else -1
    return RunOutcome(
        attempt_id=attempt_id,
        job_id=str(terminal.get("job_id", attempt.run_job_id or "")),
        exit_code=exit_code,
        compliant=bool(result.get("compliant", False)),
        zero_trade=bool(result.get("zero_trade", False)),
        metrics_available=bool(result.get("metrics_available", False)),
        acceptance_class=str(result.get("acceptance_class", "unknown")),
        earnings_provenance=str(result.get("earnings_provenance", "unknown")),
        result_path=str(primary_path) if status == "succeeded" and primary_path is not None else "",
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


def _completion_payload(job: JobRecord) -> tuple[dict[str, object], str] | None:
    for name in ("terminal.json", "cancel.json"):
        path = Path(job.run_dir) / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload, name
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


def _host_execution_ready(store: ResearchStore, attempt_id: str | None) -> str | None:
    return host_execution_ready(store, attempt_id)


def _native_execution_ready(store: ResearchStore, attempt_id: str | None) -> str | None:
    return native_execution_ready(store, attempt_id)


def _budget_execution_ready(store: ResearchStore, attempt_id: str | None) -> str | None:
    return budget_execution_ready(store, attempt_id)


def _release_admission_pending(store: ResearchStore, attempt_id: str, reason: str) -> None:
    attempt = store.get_attempt(attempt_id)
    if attempt.state != AttemptState.RUN_QUEUED or not attempt.run_job_id:
        return
    with suppress(StoreConflict):
        store.release_queued_run(attempt_id, attempt.run_job_id, reason)


def _admission_execution_ready(store: ResearchStore, attempt_id: str) -> str | None:
    """Recheck typed admission before readiness hooks and preserve owner control."""
    attempt = store.get_attempt(attempt_id)
    try:
        store.evidence(attempt_id, "admission_decision")
    except ValueError:
        reason = "ADMISSION_EVIDENCE_MISSING"
        store.record_admission_failure(
            attempt.hypothesis_id,
            reason,
            "admission decision evidence is required for a new launch",
            attempt_id=attempt_id,
        )
        _release_admission_pending(store, attempt_id, reason)
        return reason
    except StoreConflict as exc:
        reason = "ADMISSION_EVIDENCE_INVALID"
        store.record_admission_failure(
            attempt.hypothesis_id, reason, str(exc), attempt_id=attempt_id
        )
        _release_admission_pending(store, attempt_id, reason)
        return reason
    try:
        decision = _admission_for_hypothesis(store, attempt.hypothesis_id)
    except _AdmissionInputError as exc:
        store.record_admission_failure(
            attempt.hypothesis_id, exc.reason, exc.detail, attempt_id=attempt_id
        )
        _release_admission_pending(store, attempt_id, exc.reason)
        return exc.reason
    except (StoreConflict, OSError, TypeError, ValueError) as exc:
        reason = "ADMISSION_INPUT_INVALID"
        detail = f"admission inputs are invalid: {exc}"
        store.record_admission_failure(attempt.hypothesis_id, reason, detail, attempt_id=attempt_id)
        _release_admission_pending(store, attempt_id, reason)
        return reason
    if not decision.admitted:
        reason = decision.reason.value if decision.reason is not None else "ADMISSION_REFUSED"
        _record_admission_refusal(store, attempt.hypothesis_id, decision, attempt_id=attempt_id)
        _release_admission_pending(store, attempt_id, reason)
        return reason
    try:
        store.insert_admission_decision(attempt_id, decision)
    except StoreConflict as exc:
        reason = "ADMISSION_POLICY_RACE"
        store.record_admission_failure(
            attempt.hypothesis_id, reason, str(exc), attempt_id=attempt_id
        )
        _release_admission_pending(store, attempt_id, reason)
        return reason
    return None


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
        implementation = ImplementationRecord.from_json(
            store.evidence(attempt_id, "implementation")
        )
        try:
            plan = RunPlan.from_json(store.evidence(attempt_id, "run_plan"))
        except (StoreConflict, ValueError, TypeError, json.JSONDecodeError):
            return reject("run_plan_mismatch")
        try:
            host_review = json.loads(store.evidence(attempt_id, "review_host_evidence"))
        except (ValueError, json.JSONDecodeError):
            return reject("review_gate_unavailable")
        if (
            not isinstance(host_review, dict)
            or host_review.get("verdict") != "PASS"
            or host_review.get("bound_commit") != attempt.commit
            or host_review.get("bound_spec_sha256")
            != store.get_hypothesis(attempt.hypothesis_id).spec_sha256
            or host_review.get("bound_run_plan_sha256")
            != hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()
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
        if (
            plan.attempt_id != attempt_id
            or plan.commit != attempt.commit
            or plan.implementation_sha256 != attempt.implementation_sha256
        ):
            return reject("run_plan_mismatch")
        raw_plan = payload.get("run_plan")
        raw_digest = payload.get("run_plan_sha256")
        expected_plan_digest = hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()
        if (
            not isinstance(raw_plan, dict)
            or raw_digest != expected_plan_digest
            or json.dumps(raw_plan, sort_keys=True, separators=(",", ":")) != plan.to_json()
        ):
            return reject("run_plan_mismatch")
        try:
            spec_set = store.evaluation_spec_set(attempt.hypothesis_id)
        except (StoreConflict, ValueError, TypeError, json.JSONDecodeError):
            return reject("run_plan_mismatch")
        expected_specs = {entry.spec_id: (entry.path, entry.sha256) for entry in spec_set.specs}
        scenario_spec_ids = {scenario.spec_id for scenario in plan.scenarios}
        raw_paths = payload.get("evaluation_spec_paths")
        raw_digests = payload.get("evaluation_spec_digests")
        if not isinstance(raw_paths, dict) or not isinstance(raw_digests, dict):
            return reject("run_plan_mismatch")
        if set(raw_paths) != scenario_spec_ids or set(raw_digests) != scenario_spec_ids:
            return reject("run_plan_mismatch")
        if any(
            (str(raw_paths[key]), str(raw_digests[key])) != expected_specs.get(key)
            for key in scenario_spec_ids
        ):
            return reject("run_plan_mismatch")
        admission_reject = _admission_execution_ready(store, attempt_id)
        if admission_reject is not None:
            return admission_reject
        host_reason = _host_execution_ready(store, attempt_id)
        if host_reason is not None:
            return reject(f"host_execution_unavailable:{host_reason}")
        native_reason = _native_execution_ready(store, attempt_id)
        if native_reason is not None:
            return reject(f"native_execution_unavailable:{native_reason}")
        budget_reason = _budget_execution_ready(store, attempt_id)
        if budget_reason is not None:
            return reject(f"budget_unavailable:{budget_reason}")
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
                run_plan=plan,
                evaluation_spec_paths=(
                    {
                        key: Path(str(value))
                        for key, value in dict(payload["evaluation_spec_paths"]).items()
                    }
                    if isinstance(payload.get("evaluation_spec_paths"), dict)
                    else None
                ),
                evaluation_spec_digests=(
                    {
                        key: str(value)
                        for key, value in dict(payload["evaluation_spec_digests"]).items()
                    }
                    if isinstance(payload.get("evaluation_spec_digests"), dict)
                    else None
                ),
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
            completion = _completion_payload(job)
            if completion is None:
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
                terminal, origin = completion
                _finish_if_running(
                    store,
                    attempt.attempt_id,
                    _terminal_outcome(store, attempt.attempt_id, terminal, origin=origin),
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
            completion = _completion_payload(job)
            if completion is not None:
                terminal, origin = completion
                _finish_if_running(
                    store,
                    attempt.attempt_id,
                    _terminal_outcome(store, attempt.attempt_id, terminal, origin=origin),
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
            plan = RunPlan.from_json(store.evidence(attempt_id, "run_plan"))
            store.queue_run_request(
                attempt_id, job_id, run_dir, timeout_seconds, max_rss_mb, run_plan=plan
            )
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
        completion = _completion_payload(job)
        outcome = (
            _terminal_outcome(store, attempt_id, completion[0], origin=completion[1])
            if completion is not None
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


def _status_admission(store: ResearchStore, attempt_id: str) -> dict[str, object] | None:
    try:
        payload = json.loads(store.evidence(attempt_id, "admission_decision"))
    except (ValueError, json.JSONDecodeError, StoreConflict):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "admitted": payload.get("admitted"),
        "reason": payload.get("reason"),
        "detail": payload.get("detail"),
    }


def _status_refusal(store: ResearchStore, hypothesis_id: str) -> dict[str, object] | None:
    try:
        refusal = store.latest_admission_refusal(hypothesis_id)
    except StoreConflict as exc:
        return {"unavailable_reason": str(exc)}
    if refusal is None:
        return None
    reason, detail = refusal
    return {"reason": reason, "detail": detail}


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
        try:
            policy = store.campaign_policy()
        except StoreConflict:
            policy = CampaignPolicy(False)
        config = store.config()
        data = {
            "campaign": store.campaign(),
            "policy_set": policy.is_set,
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
                    "admission_refusal": _status_refusal(store, h.hypothesis_id),
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
                            "admission": _status_admission(store, a.attempt_id),
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
        consecutive_poll_failures = 0
        while True:
            # A concurrent cancel/reconcile owns the short lifecycle lock;
            # leave canonical state untouched and try on the next turn.
            with suppress(OwnerLockHeld):
                _reconcile_jobs(store)
            _dispatch_queued_job(store)
            plan = compose_wake(store)
            if plan is not None:
                deliver(store, sender, plan, session_key)
            try:
                poll_owner_turn(store, sender)
            except OwnerPollUnavailable as exc:
                consecutive_poll_failures += 1
                typer.echo(
                    "owner poll unavailable "
                    f"({consecutive_poll_failures}/{_MAX_CONSECUTIVE_POLL_FAILURES}): {exc}",
                    err=True,
                )
                if consecutive_poll_failures >= _MAX_CONSECUTIVE_POLL_FAILURES:
                    raise
            else:
                consecutive_poll_failures = 0
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
