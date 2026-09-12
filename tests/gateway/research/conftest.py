from __future__ import annotations

# The fixture embeds a compact evaluator script for disposable subprocess tests.
# ruff: noqa: E501
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from gateway.research.contracts import (
    Attempt,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    HypothesisSpec,
    ImplementationRecord,
    ReviewEvidence,
)
from gateway.research.store import ResearchStore


@pytest.fixture
def campaign(
    tmp_path: Path, request: pytest.FixtureRequest
) -> tuple[ResearchStore, Path, HypothesisSpec]:
    root = tmp_path / "driver"
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Research Tests"], cwd=source, check=True)
    (source / "tracked.txt").write_text("tracked", encoding="utf-8")
    for relative, content in getattr(request, "param", ()):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
    spec = tmp_path / "spec.json"
    panel = tmp_path / "panel.json"
    receipt = tmp_path / "receipt.json"
    eval_spec = tmp_path / "eval-spec.json"
    dividends = tmp_path / "dividends.json"
    dividends.write_text('{"contract":"trusted-dividends-v2"}', encoding="utf-8")
    universe = tmp_path / "universe.json"
    universe.write_text('{"contract":"trusted-universe-v2"}', encoding="utf-8")
    spec.write_text(json.dumps({"title": "fixture"}), encoding="utf-8")
    panel.write_text("panel", encoding="utf-8")
    receipt.write_text("receipt", encoding="utf-8")
    eval_spec.write_text("eval", encoding="utf-8")
    evaluation_spec_set = tmp_path / "evaluation-spec-set.json"
    evaluation_spec_set.write_text(
        EvaluationSpecSet(
            "research-evaluation-spec-set-v1",
            "H0001",
            "c000",
            (
                EvaluationSpecEntry(
                    "c000", str(eval_spec), hashlib.sha256(eval_spec.read_bytes()).hexdigest()
                ),
            ),
            "2026-01-01T00:00:00Z",
        ).to_json(),
        encoding="utf-8",
    )
    snapshot = tmp_path / "snapshot"
    (snapshot / "src" / "quantipy").mkdir(parents=True)
    (snapshot / "src" / "quantipy" / "__init__.py").write_text("VERSION = 'v2'\n")
    (snapshot / "pyproject.toml").write_text("[project]\nname='quantipy'\n")
    (snapshot / "uv.lock").write_text("version = 1\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(str(Path(sys.executable).resolve()), venv / "bin" / "python")
    (venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).resolve().parent}\n", encoding="utf-8"
    )
    evaluator = venv / "bin" / "quantipy"
    evaluation_digest = hashlib.sha256(eval_spec.read_bytes()).hexdigest()
    evaluator.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, hashlib, json, pathlib, sys\n"
        "p=argparse.ArgumentParser(); p.add_argument('command', nargs='*'); p.add_argument('--out'); p.add_argument('--spec'); p.add_argument('--targets'); p.add_argument('--panel'); p.add_argument('--receipt'); p.add_argument('--dividends'); p.add_argument('--universe'); p.add_argument('--require-source-root')\n"
        "a=p.parse_args(); semantic=hashlib.sha256(b'fixture-semantic-spec').hexdigest()\n"
        "if 'validate-inputs' in a.command:\n"
        " print(json.dumps({'verdict':'PASS','reasons':[],'spec_sha256_semantic':semantic,'spec_sha256_raw':'"
        + evaluation_digest
        + "','panel_sha256':'"
        + hashlib.sha256(panel.read_bytes()).hexdigest()
        + "','receipt_sha256':'"
        + hashlib.sha256(receipt.read_bytes()).hexdigest()
        + "','universe_file_sha256':'"
        + hashlib.sha256(universe.read_bytes()).hexdigest()
        + "','dividends_sha256':'"
        + hashlib.sha256(dividends.read_bytes()).hexdigest()
        + "'}); sys.exit(0)\n"
        "pathlib.Path(a.out).mkdir(parents=True, exist_ok=True); json.dump({'evaluator_version':'research-evaluator-v2','spec_sha256':semantic,'dividends_sha256':'"
        + hashlib.sha256(dividends.read_bytes()).hexdigest()
        + "','compliant':True,'zero_trade':False,'metrics_available':True,'acceptance_class':'accepted','earnings_provenance':'fixture'}, open(pathlib.Path(a.out)/'result.json','w'))\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o555)
    for frozen_input in (panel, receipt, eval_spec, dividends):
        frozen_input.chmod(0o444)
    store = ResearchStore(root)
    store.configure(venv / "bin" / "python", evaluator, snapshot, universe)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    hypothesis = store.create_hypothesis(
        "fixture",
        spec,
        panel,
        receipt,
        eval_spec,
        commit,
        dividends=dividends,
        evaluation_spec_set=evaluation_spec_set,
    )
    source.chmod(0o555)
    return store, source, hypothesis


def implementation(attempt_id: str, commit: str | None) -> ImplementationRecord:
    assert commit is not None
    return ImplementationRecord(
        attempt_id,
        commit,
        (sys.executable, "-m", "fixture_target"),
        "/tmp/evidence.json",
        "reported-coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )


def review(
    attempt_id: str, commit: str | None, spec_sha256: str, verdict: str = "PASS"
) -> ReviewEvidence:
    assert commit is not None
    return ReviewEvidence(
        attempt_id,
        commit,
        spec_sha256,
        verdict,
        ("finding",) if verdict == "FAIL" else (),
        "reported-reviewer",
        "reviewer-actual",
        "session",
        "2026-01-01T00:00:00Z",
    )


def verified_review(store: ResearchStore, record: ReviewEvidence) -> Attempt:
    """Test fixture helper for the host-verified review path."""
    host_payload = {
        "task_id": "fixture-task",
        "task_status": "succeeded",
        "task_started_at": 1,
        "task_ended_at": 2,
        "acp_session_uuid": record.acp_session_id,
        "transcript_path": "",
        "transcript_sha256": "",
        "assistant_events": 1,
        "models_seen": ["claude-opus-5"],
        "efforts_seen": ["high"],
        "verdict_json": record.to_json(),
        "bound_commit": record.commit,
        "bound_spec_sha256": record.spec_sha256,
        "collected_at": record.submitted_at,
        "verdict": record.verdict,
    }
    try:
        run_plan_payload = store.evidence(record.attempt_id, "run_plan")
    except ValueError:
        run_plan_payload = None
    if run_plan_payload is not None:
        host_payload["bound_run_plan_sha256"] = hashlib.sha256(
            run_plan_payload.encode()
        ).hexdigest()
    payload = json.dumps(host_payload, sort_keys=True, separators=(",", ":"))
    try:
        existing = store.evidence(record.attempt_id, "review")
    except ValueError:
        existing = None
    if existing is not None and existing != record.to_json():
        from gateway.research.store import StoreConflict

        raise StoreConflict("review payload differs from stored payload")
    result = store.collect_review_evidence(record.attempt_id, record, payload)
    store._repair_evidence_projection(
        store.get_attempt(record.attempt_id), "review", record.to_json()
    )
    return result
