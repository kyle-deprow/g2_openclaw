"""Measured compute probe for a DRAFT hypothesis.

The probe runs ``validate-inputs`` once per evaluation spec and one empty-target
``evaluate`` under the worker's own containment (same ``stage_plan``, mounts, pins,
``MAX_STAGE_RSS_MB`` ceiling and stage helper), serially, and records the measured wall
seconds and peak memory of every stage as immutable hypothesis evidence.  Bounds are
then taken from the measurement instead of being guessed, and the store refuses to
freeze or submit bounds the measurement shows are infeasible.
"""

from __future__ import annotations

import json
import signal
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .containment import (
    ContainmentError,
    StagePeak,
    StagePlan,
    runtime_pins_from_record,
    stage_plan,
    verify_configured_runtime_pins,
)
from .contracts import (
    MAX_RUN_TIMEOUT_SECONDS,
    MAX_STAGE_RSS_MB,
    ComputeProbe,
    HypothesisState,
    ProbeStage,
    compute_requirements,
)
from .store import sha256_file
from .worker import _owned_directory, _worker_term, evaluate_command, validate_inputs_command
from .worker import _stage as run_contained_stage

if TYPE_CHECKING:
    from .store import ResearchStore

__all__ = ["ComputeProbeError", "compute_requirements", "run_compute_probe"]

EMPTY_TARGETS = '{"targets":[]}'


class ComputeProbeError(RuntimeError):
    """The probe could not complete; nothing was recorded and the DRAFT is unchanged."""


def _regular(path: Path, digest: str, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ComputeProbeError(f"{label} is not a regular file: {path}")
    if sha256_file(path) != digest:
        raise ComputeProbeError(f"{label} changed since the hypothesis was created: {path}")
    return path


def _abandon_hint(hypothesis_id: str) -> str:
    return (
        f"if this repeats the hypothesis is infeasible within driver bounds: abandon the DRAFT "
        f"with `hypothesis-decide {hypothesis_id} --decision ABANDONED`"
    )


def _run_stage(
    plan: StagePlan, probe_dir: Path, stage: str, spec_id: str, deadline: float, hint: str
) -> ProbeStage:
    peak = StagePeak()
    tick = time.monotonic()
    err = probe_dir / "logs" / f"{stage}.err"
    deadline_message = (
        f"{stage} hit the {MAX_RUN_TIMEOUT_SECONDS}s overall probe deadline; inspect {err}; {hint}"
    )
    try:
        exit_code, timed_out = run_contained_stage(
            plan, probe_dir, probe_dir / "logs" / f"{stage}.out", deadline, peak
        )
    except ContainmentError as exc:
        # Stopping a stage at the deadline can race its scope start-up; keep the
        # timeout as the reported cause instead of the secondary stop failure.
        if time.monotonic() >= deadline:
            raise ComputeProbeError(f"{deadline_message}; {exc}") from exc
        raise
    wall = time.monotonic() - tick
    if timed_out:
        raise ComputeProbeError(deadline_message)
    if exit_code != 0:
        reasons = _validator_reasons(_read_json(probe_dir / "logs" / f"{stage}.out"))
        raise ComputeProbeError(f"{stage} exited {exit_code}{reasons}; inspect {err}; {hint}")
    if peak.peak_bytes <= 0:
        raise ComputeProbeError(f"{stage} peak memory could not be measured")
    return ProbeStage(stage, spec_id, exit_code, max(wall, 1e-6), peak.peak_mb)


MAX_REPORTED_REASONS = 5
MAX_REASON_CHARS = 300


def _validator_reasons(validation: object) -> str:
    """The validator's own refusal reasons (bounded), so the owner sees why it refused."""
    reasons = validation.get("reasons") if isinstance(validation, dict) else None
    if not isinstance(reasons, list):
        return ""
    texts = [str(item)[:MAX_REASON_CHARS] for item in reasons if isinstance(item, str)]
    if not texts:
        return ""
    more = len(texts) - MAX_REPORTED_REASONS
    suffix = f" (+{more} more)" if more > 0 else ""
    return f" (reasons: {'; '.join(texts[:MAX_REPORTED_REASONS])}{suffix})"


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _check_validation(stdout: Path, stage: str, expected: dict[str, str]) -> None:
    try:
        validation = json.loads(stdout.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComputeProbeError(f"{stage} output is not JSON") from exc
    if not isinstance(validation, dict) or validation.get("verdict") != "PASS":
        raise ComputeProbeError(
            f"{stage} did not return verdict PASS{_validator_reasons(validation)}; inspect {stdout}"
        )
    if any(validation.get(key) != value for key, value in expected.items()):
        raise ComputeProbeError(f"{stage} digests do not bind the hypothesis inputs")


def run_compute_probe(store: ResearchStore, hypothesis_id: str) -> ComputeProbe:
    """Measure, record and return the compute probe of a DRAFT hypothesis."""
    store.acquire_run_lock()  # non-blocking: refuses while any run/dispatch holds it
    try:
        return _probe_locked(store, hypothesis_id)
    finally:
        store.release_run_lock()


def _probe_locked(store: ResearchStore, hypothesis_id: str) -> ComputeProbe:
    hypothesis = store.get_hypothesis(hypothesis_id)
    if hypothesis.state != HypothesisState.DRAFT:
        raise ComputeProbeError(
            f"{hypothesis_id} is {hypothesis.state.value}; a compute probe needs a DRAFT"
        )
    if store.compute_probe(hypothesis_id) is not None:
        raise ComputeProbeError(f"compute probe already recorded for {hypothesis_id}")
    pins_digests, input_digests = store.probe_bindings(hypothesis_id)
    spec_set = store.evaluation_spec_set(hypothesis_id)
    try:
        pins = runtime_pins_from_record(dict(store.config()))
        verify_configured_runtime_pins(pins)
    except ContainmentError as exc:
        raise ComputeProbeError(str(exc)) from exc

    panel = _regular(Path(hypothesis.panel_path), hypothesis.panel_sha256, "panel")
    receipt = _regular(Path(hypothesis.receipt_path), hypothesis.receipt_sha256, "receipt")
    dividends = _regular(Path(hypothesis.dividends_path), hypothesis.dividends_sha256, "dividends")
    specs = [
        (entry.spec_id, _regular(Path(entry.path), entry.sha256, f"spec {entry.spec_id}"), entry)
        for entry in spec_set.specs
    ]

    probe_id = f"P{uuid.uuid4().hex[:12]}"
    hypothesis_dir = store.root / "hypotheses" / hypothesis_id
    probe_root = hypothesis_dir / "compute-probe"
    probe_dir = probe_root / probe_id
    try:
        # Same ownership rule as the worker's run directories: never follow a symlink.
        _owned_directory(hypothesis_dir, "hypothesis directory")
        _owned_directory(probe_root, "compute probe directory")
        probe_dir.mkdir(exist_ok=False)
        _owned_directory(probe_dir / "logs", "compute probe logs")
    except (ContainmentError, OSError) as exc:
        raise ComputeProbeError(f"compute probe scratch is not usable: {exc}") from exc
    hint = _abandon_hint(hypothesis_id)
    deadline = time.monotonic() + MAX_RUN_TIMEOUT_SECONDS
    job_id = f"probe-{hypothesis_id.lower()}-{probe_id[1:9]}"

    stages: list[ProbeStage] = []
    previous_handler = None
    try:
        previous_handler = signal.signal(signal.SIGTERM, _worker_term)
    except ValueError:  # not the main thread: nothing to install
        previous_handler = None
    try:
        for spec_id, spec_path, entry in specs:
            stage = f"validate-{spec_id}"
            plan = stage_plan(
                pins,
                job_id,
                stage,
                MAX_STAGE_RSS_MB,
                validate_inputs_command(pins),
                panel=panel,
                receipt=receipt,
                spec=spec_path,
                dividends=dividends,
            )
            stages.append(_run_stage(plan, probe_dir, stage, spec_id, deadline, hint))
            _check_validation(
                probe_dir / "logs" / f"{stage}.out",
                stage,
                {
                    "spec_sha256_raw": entry.sha256,
                    "panel_sha256": hypothesis.panel_sha256,
                    "receipt_sha256": hypothesis.receipt_sha256,
                    "universe_file_sha256": pins.universe_sha256,
                    "dividends_sha256": hypothesis.dividends_sha256,
                },
            )

        first_id, first_path, _ = specs[0]
        empty_targets = probe_dir / "empty-targets.json"
        empty_targets.write_text(EMPTY_TARGETS, encoding="utf-8")
        empty_targets.chmod(0o444)
        evaluator_dir = probe_dir / "evaluator-stage"
        provenance_dir = evaluator_dir / "provenance"
        provenance_dir.mkdir(parents=True)
        plan = stage_plan(
            pins,
            job_id,
            "evaluate-s000",
            MAX_STAGE_RSS_MB,
            evaluate_command(pins),
            panel=panel,
            receipt=receipt,
            spec=first_path,
            dividends=dividends,
            evaluator_stage=evaluator_dir,
            targets_file=empty_targets,
            provenance_dir=provenance_dir,
        )
        stages.append(_run_stage(plan, probe_dir, "evaluate-s000", first_id, deadline, hint))
        try:
            verify_configured_runtime_pins(pins)
        except ContainmentError as exc:
            raise ComputeProbeError(str(exc)) from exc
    except ContainmentError as exc:
        raise ComputeProbeError(str(exc)) from exc
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)

    probe = ComputeProbe(
        "research-compute-probe-v1",
        probe_id,
        hypothesis_id,
        pins_digests,
        input_digests,
        tuple(stages),
        datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    store.record_compute_probe(probe)
    return probe
