from __future__ import annotations

# The fixture embeds a compact evaluator script for disposable subprocess tests.
# ruff: noqa: E501
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from gateway.research.contracts import HypothesisSpec, ImplementationRecord, ReviewRecord
from gateway.research.store import ResearchStore


@pytest.fixture
def campaign(tmp_path: Path) -> tuple[ResearchStore, Path, HypothesisSpec]:
    root = tmp_path / "driver"
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Research Tests"], cwd=source, check=True)
    (source / "tracked.txt").write_text("tracked", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
    spec = tmp_path / "spec.json"
    panel = tmp_path / "panel.json"
    receipt = tmp_path / "receipt.json"
    eval_spec = tmp_path / "eval-spec.json"
    spec.write_text(json.dumps({"title": "fixture"}), encoding="utf-8")
    panel.write_text("panel", encoding="utf-8")
    receipt.write_text("receipt", encoding="utf-8")
    eval_spec.write_text("eval", encoding="utf-8")
    evaluator = tmp_path / "evaluator"
    evaluation_digest = hashlib.sha256(eval_spec.read_bytes()).hexdigest()
    evaluator.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, pathlib\n"
        "p=argparse.ArgumentParser(); p.add_argument('command', nargs='*'); p.add_argument('--out'); p.add_argument('--spec'); p.add_argument('--targets'); p.add_argument('--panel'); p.add_argument('--receipt')\n"
        "a=p.parse_args(); pathlib.Path(a.out).mkdir(parents=True, exist_ok=True); json.dump({'evaluator_version':'research-evaluator-v1','spec_sha256':'"
        + evaluation_digest
        + "','compliant':True,'zero_trade':False,'metrics_available':True,'acceptance_class':'accepted','earnings_provenance':'fixture'}, open(pathlib.Path(a.out)/'result.json','w'))\n",
        encoding="utf-8",
    )
    evaluator.chmod(0o555)
    for frozen_input in (panel, receipt, eval_spec):
        frozen_input.chmod(0o444)
    store = ResearchStore(root)
    store.configure(Path("/usr/bin/python3"), evaluator)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    hypothesis = store.create_hypothesis("fixture", spec, panel, receipt, eval_spec, commit)
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
) -> ReviewRecord:
    assert commit is not None
    return ReviewRecord(
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
