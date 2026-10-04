"""Deterministic implementation-submission build and mechanical preflight.

``build_submission`` derives the ``ImplementationRecord``, the ``RunPlan`` and the provenance
evidence index from a small declarative input plus the store, so nobody hand-computes digests
or argv.  ``run_preflight`` runs, read-only, every check ``implementation-submit`` performs (the
very same callables, see ``ResearchStore.submission_checks``) plus the host-side launch, budget
and review-bundle checks that otherwise fail only at the reviewer or at run time.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .codec import to_json
from .containment import RuntimePins, runtime_pins_from_record
from .contracts import (
    MAX_RUN_TIMEOUT_SECONDS,
    MAX_STAGE_RSS_MB,
    RUN_OVERHEAD_SECONDS,
    Attempt,
    AttemptState,
    ImplementationRecord,
    RunPlan,
    RunScenario,
    SubmissionInput,
    is_host_owned_target_flag,
)
from .hypothesis import HypothesisDocument
from .jobs import preflight as worktree_preflight
from .jobs import validate_launch
from .machine import IllegalTransition
from .machine import submit_implementation as machine_submit_implementation
from .provenance import validate_provenance_evidence
from .review_evidence import BundleCandidate, BundleReport, dry_build_review_bundle
from .store import ResearchStore, StoreConflict, now_utc

IMPLEMENTATION_FILE = "implementation-record.json"
RUN_PLAN_FILE = "run-plan.json"
PROVENANCE_FILE = "provenance-evidence.json"


class SubmissionError(RuntimeError):
    """The submission input cannot be turned into a valid submission."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _hypothesis_inputs(store: ResearchStore, attempt: Attempt) -> tuple[str, str, Path]:
    """Return the bound panel, receipt and the canonical attempt run directory."""
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    return hypothesis.panel_path, hypothesis.receipt_path, run_dir


def production_targets_argv(
    *,
    interpreter: str,
    program: tuple[str, ...],
    panel: str,
    receipt: str,
    scenario_id: str,
    extra_args: tuple[str, ...],
    run_dir: Path,
) -> tuple[str, ...]:
    """The one production targets argv shape accepted by the containment and launch validators.

    ``[interpreter, *program, --panel P, --receipt R, --scenario-id sNNN, *extra, --out
    <run_dir>/scenarios/sNNN/targets-stage/targets.json]`` where ``program`` is either
    ``(<absolute script>,)`` or ``("-m", <module>)``.
    """
    out = run_dir / "scenarios" / scenario_id / "targets-stage" / "targets.json"
    return (
        interpreter,
        *program,
        "--panel",
        panel,
        "--receipt",
        receipt,
        "--scenario-id",
        scenario_id,
        *extra_args,
        "--out",
        str(out),
    )


def _git_has_file(worktree: Path, commit: str, relative: str) -> bool:
    """Whether ``relative`` is a tracked regular blob (not a tree or submodule) at ``commit``."""
    result = subprocess.run(
        ["git", "-C", str(worktree), "cat-file", "-t", f"{commit}:{relative}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.decode().strip() == "blob"


@dataclass(frozen=True, slots=True)
class BuiltSubmission:
    """The three files ``submission-build`` wrote, with their digests."""

    implementation: Path
    run_plan: Path
    provenance: Path
    implementation_sha256: str
    evaluation_spec_set_sha256: str


def build_submission(
    store: ResearchStore, attempt_id: str, source: SubmissionInput, out_dir: Path
) -> BuiltSubmission:
    """Derive and write the submission files; never calls ``implementation-submit``."""
    attempt = store.get_attempt(attempt_id)
    if attempt.state != AttemptState.OPENED:
        # Exactly what ``machine.submit_implementation`` (hence implementation-submit) raises.
        raise IllegalTransition(attempt.state.value, AttemptState.IMPLEMENTED.value)
    worktree = Path(attempt.worktree_path)
    if not worktree.is_absolute():
        raise SubmissionError("attempt worktree path must be absolute")
    worktree_preflight(worktree, source.commit)
    if not out_dir.is_absolute():
        raise SubmissionError("--out-dir must be an absolute path")
    resolved_out = out_dir.resolve()
    if resolved_out == worktree.resolve() or worktree.resolve() in resolved_out.parents:
        raise SubmissionError("--out-dir must be outside the attempt worktree")
    if resolved_out.exists() and (not resolved_out.is_dir() or any(resolved_out.iterdir())):
        raise SubmissionError("--out-dir must be empty or absent")

    pins = runtime_pins_from_record(dict(store.config()))
    if Path(source.interpreter).resolve() != pins.shared_python.resolve():
        raise SubmissionError("interpreter does not match the configured shared interpreter pin")
    for relative, label in (
        (source.target_script, "target_script"),
        (source.test_evidence_path, "test_evidence_path"),
    ):
        if not _git_has_file(worktree, source.commit, relative):
            raise SubmissionError(f"{label} is not tracked at commit {source.commit}: {relative}")
    spec_set = store.evaluation_spec_set(attempt.hypothesis_id)
    entries = {entry.spec_id: entry for entry in spec_set.specs}
    panel, receipt, run_dir = _hypothesis_inputs(store, attempt)
    script = str(worktree / source.target_script)
    scenarios: list[RunScenario] = []
    for item in source.scenarios:
        entry = entries.get(item.spec_id)
        if entry is None:
            raise SubmissionError(f"scenario {item.scenario_id} names unknown spec {item.spec_id}")
        scenarios.append(
            RunScenario(
                item.scenario_id,
                production_targets_argv(
                    interpreter=str(pins.shared_python),
                    program=(script,),
                    panel=panel,
                    receipt=receipt,
                    scenario_id=item.scenario_id,
                    extra_args=item.extra_args,
                    run_dir=run_dir,
                ),
                item.spec_id,
                entry.sha256,
            )
        )
    primary = next(item for item in scenarios if item.scenario_id == source.primary_scenario_id)
    record = ImplementationRecord(
        attempt_id,
        source.commit,
        primary.targets_argv,
        str(worktree / source.test_evidence_path),
        source.reported_coder_model,
        source.coder_effort,
        source.coder_service_tier,
        now_utc(),
    )
    record_text = record.to_json()
    plan = RunPlan(
        "research-run-plan-v1",
        attempt_id,
        source.commit,
        _sha256(record_text),
        _sha256(spec_set.to_json()),
        source.primary_scenario_id,
        tuple(scenarios),
        source.analysis,
        source.scenario_timeout_seconds,
        source.analysis_timeout_seconds,
    )
    plan_text = plan.to_json()
    provenance_text = to_json(
        {
            "attempt_id": attempt_id,
            "commit": source.commit,
            "contract": "research-provenance-evidence-v1",
            "stages": [
                {"records_dir": str(worktree / stage.records_dir), "stage": stage.stage}
                for stage in source.provenance_stages
            ],
        }
    )

    # Validate everything (round trip and provenance) before writing anything to --out-dir.
    if ImplementationRecord.from_json(record_text).to_json() != record_text:
        raise SubmissionError("implementation record is not canonical after a round trip")
    if RunPlan.from_json(plan_text).to_json() != plan_text:
        raise SubmissionError("run plan is not canonical after a round trip")
    with tempfile.TemporaryDirectory(prefix="submission-build-") as scratch:
        index = Path(scratch) / PROVENANCE_FILE
        index.write_text(provenance_text, encoding="utf-8")
        validate_provenance_evidence(store, attempt_id, plan, index)
    created_dir = not resolved_out.exists()
    resolved_out.mkdir(parents=True, exist_ok=True)
    paths = (
        (resolved_out / IMPLEMENTATION_FILE, record_text),
        (resolved_out / RUN_PLAN_FILE, plan_text),
        (resolved_out / PROVENANCE_FILE, provenance_text),
    )
    try:
        for path, text in paths:
            path.write_text(text, encoding="utf-8")
    except BaseException:
        for path, _text in paths:
            path.unlink(missing_ok=True)
        if created_dir:
            with suppress(OSError):
                resolved_out.rmdir()
        raise
    return BuiltSubmission(
        paths[0][0], paths[1][0], paths[2][0], _sha256(record_text), plan.evaluation_spec_set_sha256
    )


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str

    @property
    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'} {self.name} {self.detail}".rstrip()


@dataclass(frozen=True, slots=True)
class PreflightReport:
    attempt_id: str
    checks: tuple[PreflightCheck, ...]
    bundle: BundleReport | None

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def lines(self) -> list[str]:
        return [check.line for check in self.checks]

    def to_json_value(self) -> dict[str, object]:
        bundle: dict[str, object] | None = None
        if self.bundle is not None:
            report = self.bundle
            bundle = {
                "commit": report.commit,
                "error": report.error,
                "total_bytes": report.total_bytes,
                "source_bytes": report.source_bytes,
                "diff_bytes": report.diff_bytes,
                "deployed_estimate_bytes": report.deployed_estimate_bytes,
                "max_bundle_bytes": report.max_bundle_bytes,
                "largest_files": [
                    {"path": name, "bytes": size} for name, size in report.largest_files
                ],
            }
        return {
            "attempt_id": self.attempt_id,
            "ok": self.ok,
            "checks": [
                {"name": c.name, "status": "PASS" if c.ok else "FAIL", "detail": c.detail}
                for c in self.checks
            ],
            "bundle": bundle,
        }


def _bundle_detail(report: BundleReport) -> str:
    largest = ", ".join(f"{name}={size}" for name, size in report.largest_files)
    return (
        f"total_bytes={report.total_bytes} of {report.max_bundle_bytes}; deployed_estimate_bytes="
        f"{report.deployed_estimate_bytes} (tracked source {report.source_bytes} + diff "
        f"{report.diff_bytes} + 2 MiB overhead); diff_bytes={report.diff_bytes}; "
        f"largest: {largest}"
    )


@contextmanager
def _no_optional_git_locks() -> Iterator[None]:
    """Keep ``git status`` from refreshing the worktree index while preflight runs."""
    previous = os.environ.get("GIT_OPTIONAL_LOCKS")
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GIT_OPTIONAL_LOCKS", None)
        else:
            os.environ["GIT_OPTIONAL_LOCKS"] = previous


def _argv_shape(
    argv: tuple[str, ...],
    *,
    pins: RuntimePins,
    panel: str,
    receipt: str,
    scenario_id: str,
    run_dir: Path,
) -> None:
    """Require the exact production targets argv shape for one scenario."""
    if not argv or Path(argv[0]).resolve() != pins.shared_python.resolve():
        raise StoreConflict("argv[0] is not the pinned shared interpreter")
    program = 3 if len(argv) > 1 and argv[1] == "-m" else 2
    expected_out = str(run_dir / "scenarios" / scenario_id / "targets-stage" / "targets.json")
    rest = argv[program:]
    if len(rest) < 8 or rest[:6] != (
        "--panel",
        panel,
        "--receipt",
        receipt,
        "--scenario-id",
        scenario_id,
    ):
        raise StoreConflict(
            f"{scenario_id} argv must continue with --panel <bound panel> --receipt <bound "
            f"receipt> --scenario-id {scenario_id} after the program"
        )
    if rest[-2:] != ("--out", expected_out):
        raise StoreConflict(f"{scenario_id} argv must end with --out {expected_out}")
    if any(is_host_owned_target_flag(token) for token in rest[6:-2]):
        raise StoreConflict(f"{scenario_id} argv repeats a host-owned flag")


def _launch_check(
    store: ResearchStore, attempt: Attempt, record: ImplementationRecord, plan: RunPlan
) -> str:
    """Run the pure ``jobs.validate_launch`` exactly as the host queue dispatch would."""
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    config = store.config()
    pins = runtime_pins_from_record(dict(config))
    run_dir = (
        store.root
        / "hypotheses"
        / hypothesis.hypothesis_id
        / "attempts"
        / attempt.attempt_id
        / "run"
    )
    artifact_paths = {
        "spec": store.root / "hypotheses" / hypothesis.hypothesis_id / "spec.json",
        "panel": Path(hypothesis.panel_path),
        "receipt": Path(hypothesis.receipt_path),
        "evaluation_spec": Path(hypothesis.evaluation_spec_path),
        "dividends": Path(hypothesis.dividends_path),
    }
    artifact_digests = {
        "spec": hypothesis.spec_sha256,
        "panel": hypothesis.panel_sha256,
        "receipt": hypothesis.receipt_sha256,
        "evaluation_spec": hypothesis.evaluation_spec_sha256,
        "dividends": hypothesis.dividends_sha256,
    }
    entries = {
        entry.spec_id: entry for entry in store.evaluation_spec_set(hypothesis.hypothesis_id).specs
    }
    spec_ids = {scenario.spec_id for scenario in plan.scenarios}
    document = HypothesisDocument.from_json(hypothesis.spec_json)
    timeout = min(plan.stage_budget_seconds + RUN_OVERHEAD_SECONDS, float(MAX_RUN_TIMEOUT_SECONDS))
    validate_launch(
        run_dir.parent,
        Path(attempt.worktree_path),
        record.targets_argv,
        timeout,
        min(document.compute.max_rss_mb, MAX_STAGE_RSS_MB),
        shared_python=pins.shared_python,
        expected_commit=record.commit,
        artifact_paths={key: value.resolve() for key, value in artifact_paths.items()},
        artifact_digests=artifact_digests,
        evaluator_source=Path(str(config["evaluator_source"])),
        evaluator_source_sha256=str(config["evaluator_source_sha256"]),
        configured_pins=pins,
        dividends_path=Path(hypothesis.dividends_path),
        run_plan=plan,
        evaluation_spec_paths={key: Path(entries[key].path) for key in spec_ids if key in entries},
        evaluation_spec_digests={key: entries[key].sha256 for key in spec_ids if key in entries},
    )
    return f"{len(plan.scenarios)} scenario argv(s) pass jobs.validate_launch"


def _stage_budget_check(store: ResearchStore, attempt: Attempt, plan: RunPlan) -> str:
    budget = plan.stage_budget_seconds
    derived = budget + RUN_OVERHEAD_SECONDS
    if derived > MAX_RUN_TIMEOUT_SECONDS:
        raise StoreConflict(
            f"derived run timeout {derived:g}s (stage budget {budget:g}s plus "
            f"{RUN_OVERHEAD_SECONDS:g}s overhead) exceeds the {MAX_RUN_TIMEOUT_SECONDS}s limit"
        )
    try:
        document = HypothesisDocument.from_json(
            store.get_hypothesis(attempt.hypothesis_id).spec_json
        )
    except ValueError as exc:
        raise StoreConflict(
            f"frozen hypothesis spec cannot supply compute.max_wall_seconds: {exc}"
        ) from exc
    if budget > document.compute.max_wall_seconds:
        raise StoreConflict(
            f"stage budget {budget:g}s exceeds compute.max_wall_seconds="
            f"{document.compute.max_wall_seconds:g}"
        )
    return (
        f"stage budget {budget:g}s, derived run timeout {derived:g}s <= "
        f"{MAX_RUN_TIMEOUT_SECONDS}s, stage budget <= compute.max_wall_seconds="
        f"{document.compute.max_wall_seconds:g}"
    )


def run_preflight(
    store: ResearchStore,
    attempt_id: str,
    *,
    record_path: Path,
    run_plan_path: Path,
    provenance_path: Path,
    nominal_bundle_dir: Path | None = None,
) -> PreflightReport:
    """Run every submission check without mutating the store or the worktree."""
    with _no_optional_git_locks():
        return _run_preflight(
            store,
            attempt_id,
            record_path=record_path,
            run_plan_path=run_plan_path,
            provenance_path=provenance_path,
            nominal_bundle_dir=nominal_bundle_dir,
        )


def _run_preflight(
    store: ResearchStore,
    attempt_id: str,
    *,
    record_path: Path,
    run_plan_path: Path,
    provenance_path: Path,
    nominal_bundle_dir: Path | None,
) -> PreflightReport:
    checks: list[PreflightCheck] = []

    def check(name: str, run: Callable[[], str]) -> str | None:
        try:
            detail = run()
        except Exception as exc:
            checks.append(PreflightCheck(name, False, str(exc) or type(exc).__name__))
            return None
        checks.append(PreflightCheck(name, True, detail))
        return detail

    attempt = store.get_attempt(attempt_id)
    parsed: dict[str, object] = {}

    def parse_record() -> str:
        parsed["record"] = ImplementationRecord.from_json(record_path.read_text(encoding="utf-8"))
        return str(record_path)

    def parse_plan() -> str:
        parsed["plan"] = RunPlan.from_json(run_plan_path.read_text(encoding="utf-8"))
        return str(run_plan_path)

    check("implementation-file", parse_record)
    check("run-plan-file", parse_plan)
    record = parsed.get("record")
    plan = parsed.get("plan")
    if not isinstance(record, ImplementationRecord) or not isinstance(plan, RunPlan):
        return PreflightReport(attempt_id, tuple(checks), None)

    provenance: dict[str, str] = {}

    def provenance_check() -> str:
        provenance["text"] = validate_provenance_evidence(store, attempt_id, plan, provenance_path)
        stages = json.loads(provenance["text"])["stages"]
        return f"{len(stages)} stage(s) verified against the pins and commit {plan.commit[:12]}"

    def state_check() -> str:
        if "text" in provenance:
            replay = store.implementation_replay_state(attempt_id, record, plan, provenance["text"])
            return "identical replay of stored evidence" if replay else "attempt is OPENED"
        machine_submit_implementation(attempt, record, now_utc())
        return "attempt is OPENED"

    check("provenance", provenance_check)
    check("attempt-state", state_check)
    for step in store.submission_checks(attempt, record, plan):
        check(step.name, step.run)
    check("worktree", lambda: _worktree(attempt, record))
    panel, receipt, run_dir = _hypothesis_inputs(store, attempt)

    def argv_shapes() -> str:
        pins = runtime_pins_from_record(dict(store.config()))
        for scenario in plan.scenarios:
            _argv_shape(
                scenario.targets_argv,
                pins=pins,
                panel=panel,
                receipt=receipt,
                scenario_id=scenario.scenario_id,
                run_dir=run_dir,
            )
        return f"{len(plan.scenarios)} scenario argv(s) have the production shape"

    check("argv-shape", argv_shapes)
    check("launch", lambda: _launch_check(store, attempt, record, plan))
    check("stage-budget", lambda: _stage_budget_check(store, attempt, plan))

    bundle: list[BundleReport] = []

    def bundle_check() -> str:
        if "text" not in provenance:
            raise StoreConflict("review bundle cannot be built without valid provenance evidence")
        report = dry_build_review_bundle(
            store,
            attempt_id,
            BundleCandidate(record.commit, record.to_json(), plan, provenance["text"]),
            nominal_bundle_dir=nominal_bundle_dir,
        )
        bundle.append(report)
        if report.error is not None:
            raise StoreConflict(f"{report.error}; {_bundle_detail(report)}")
        if not report.within_limits:
            raise StoreConflict(f"review bundle exceeds its size limit; {_bundle_detail(report)}")
        return _bundle_detail(report)

    check("bundle", bundle_check)
    return PreflightReport(attempt_id, tuple(checks), bundle[0] if bundle else None)


def _worktree(attempt: Attempt, record: ImplementationRecord) -> str:
    worktree_preflight(Path(attempt.worktree_path), record.commit)
    return f"clean, HEAD == {record.commit[:12]}"
