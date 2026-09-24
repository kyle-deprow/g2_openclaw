from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import gateway.research.review_evidence as review_evidence
import pytest
from gateway.cli import app
from gateway.openclaw_client import OpenClawTransportError
from gateway.research.contracts import (
    Attempt,
    AttemptState,
    HypothesisDecision,
    HypothesisSpec,
    ImplementationRecord,
    ReviewRecord,
    RunPlan,
)
from gateway.research.review_evidence import (
    BundleError,
    ReviewPending,
    ReviewReservation,
    ReviewUnresolved,
    acknowledge_review,
    cancel_review,
    collect_review,
    reconcile_review,
    reserve_review,
)
from gateway.research.store import ResearchStore, StoreConflict
from typer.testing import CliRunner

from tests.gateway.research.conftest import provenance_evidence, run_plan

runner = CliRunner()


def _queue(store: ResearchStore, attempt_id: str, job_id: str) -> Attempt:
    attempt = store.get_attempt(attempt_id)
    run_dir = store.root / "hypotheses" / attempt.hypothesis_id / "attempts" / attempt_id / "run"
    plan = RunPlan.from_json(store.evidence(attempt_id, "run_plan"))
    return store.queue_run_request(attempt_id, job_id, run_dir, 30, 256, run_plan=plan)


def _setup(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> tuple[ResearchStore, Path, HypothesisSpec, str, Path]:
    store, source, hypothesis = campaign
    evidence = tmp_path / "tests.json"
    evidence.write_text('{"pytest":"pass"}\n', encoding="utf-8")
    store.freeze(hypothesis.hypothesis_id)
    attempt = store.open_attempt(hypothesis.hypothesis_id, source)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    record = ImplementationRecord(
        attempt.attempt_id,
        commit,
        ("python", "-m", "target"),
        str(evidence),
        "reported-coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    store.submit_implementation(
        attempt.attempt_id,
        record,
        run_plan=run_plan(store, attempt, record),
        containment_provenance=provenance_evidence(attempt.attempt_id, record.commit),
    )
    bundle = tmp_path / "review-bundle"
    return store, source, hypothesis, attempt.attempt_id, bundle


def _reserve_legacy_bundle(
    store: ResearchStore, attempt_id: str, bundle: Path
) -> ReviewReservation:
    review_evidence.build_review_bundle(store, attempt_id, bundle)
    bundle.chmod(0o755)
    (bundle / "containment-provenance.json").unlink()
    review_evidence._readonly_tree(bundle)
    attempt = store.get_attempt(attempt_id)
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    assert attempt.commit is not None
    reservation = ReviewReservation(
        attempt_id=attempt_id,
        commit=attempt.commit,
        hypothesis_spec_sha256=hypothesis.spec_sha256,
        bundle_dir=str(bundle.resolve()),
        bundle_sha256=review_evidence._bundle_digest(bundle),
        reserved_at="2026-01-01T00:00:00Z",
        owner_session_key="owner",
        label=f"{attempt_id}-legacy",
        reservation_nonce="legacy",
    )
    store.insert_review_evidence(
        attempt_id, "review_reservation", reservation.to_json(), "review_reserved"
    )
    return reservation


def _commit_source(source: Path, message: str) -> None:
    source.chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=source, check=True)
    source.chmod(0o555)


def _host_fixture(
    store: ResearchStore,
    attempt_id: str,
    bundle: Path,
    tmp_path: Path,
    *,
    status: str = "succeeded",
    verdict: str = "PASS",
    findings: tuple[str, ...] = (),
) -> tuple[Path, Path, Path]:
    reservation_payload = json.loads(store.evidence(attempt_id, "review_reservation"))
    label = str(reservation_payload["label"])
    reserved_at = datetime.fromisoformat(
        str(reservation_payload["reserved_at"]).replace("Z", "+00:00")
    )
    reserved_ms = int(reserved_at.timestamp() * 1000)
    core = tmp_path / "openclaw.sqlite"
    child_key = "agent:claude:acp:child"
    run_id = "run-1"
    session_uuid = "84785a20-b278-4d4a-9c62-fa35e5bb9928"
    with sqlite3.connect(core) as conn:
        conn.executescript(
            """
            CREATE TABLE task_runs(
              task_id TEXT, runtime TEXT, task_kind TEXT, source_id TEXT,
              requester_session_key TEXT, owner_key TEXT, scope_kind TEXT,
              child_session_key TEXT, agent_id TEXT, requester_agent_id TEXT,
              run_id TEXT, label TEXT, status TEXT, created_at INTEGER,
              started_at INTEGER, ended_at INTEGER
            );
            CREATE TABLE subagent_runs (
              run_id TEXT NOT NULL PRIMARY KEY,
              child_session_key TEXT NOT NULL,
              controller_session_key TEXT,
              requester_session_key TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              payload_json TEXT NOT NULL DEFAULT '{}'
            ) STRICT;
            """
        )
        conn.execute(
            "INSERT INTO task_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "task-1",
                "acp",
                "review",
                None,
                "owner",
                "owner",
                "session",
                child_key,
                "claude",
                None,
                run_id,
                label,
                status,
                reserved_ms + 1000,
                reserved_ms + 2000,
                reserved_ms + 3000,
            ),
        )
        conn.commit()
    sessions = tmp_path / "acpx-sessions"
    sessions.mkdir()
    record_uuid = "10551e01-0503-456f-9e61-6993c912d478"
    record = {
        "schema": "acpx.session.v1",
        "acpx_record_id": f"{child_key}:oneshot:{record_uuid}",
        "acp_session_id": session_uuid,
        "cwd": str(bundle),
        "name": child_key,
        "created_at": (reserved_at + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "last_used_at": (reserved_at + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        "last_request_id": run_id,
        "closed": True,
        "closed_at": (reserved_at + timedelta(seconds=4)).isoformat().replace("+00:00", "Z"),
        "title": f"review {attempt_id}",
        "messages": [{"User": {"content": []}}],
        "acpx": {
            "current_model_id": "opus[1m]",
            "available_models": [
                "default",
                "opus[1m]",
                "claude-fable-5-1[1m]",
                "sonnet",
                "haiku",
            ],
            "model_control": "config_option",
            "config_options": [
                {"id": "mode", "currentValue": "default"},
                {"id": "model", "currentValue": "opus[1m]"},
                {"id": "effort", "name": "Effort", "currentValue": "high"},
                {"id": "fast", "currentValue": "off"},
            ],
            "session_options": {"model": "claude-opus-5"},
        },
    }
    record_path = sessions / (quote(f"{child_key}:oneshot:{record_uuid}", safe="") + ".json")
    record_path.write_text(json.dumps(record), encoding="utf-8")
    encoded = "".join(char if char.isalnum() and char.isascii() else "-" for char in str(bundle))
    project = tmp_path / "projects" / encoded
    project.mkdir(parents=True)
    event_time = (reserved_at + timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    verdict_object = {
        "verdict": verdict,
        "attempt_id": attempt_id,
        "commit": str(reservation_payload["commit"]),
        "spec_sha256": str(reservation_payload["hypothesis_spec_sha256"]),
        "findings": list(findings),
    }
    transcript = {
        "type": "assistant",
        "sessionId": session_uuid,
        "cwd": str(bundle),
        "effort": "high",
        "timestamp": event_time,
        "message": {
            "model": "claude-opus-5",
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(verdict_object)}],
        },
    }
    (project / f"{session_uuid}.jsonl").write_text(json.dumps(transcript) + "\n", encoding="utf-8")
    return core, sessions, tmp_path / "projects"


def _transcript_path(projects: Path) -> Path:
    paths = list(projects.rglob("*.jsonl"))
    assert len(paths) == 1
    return paths[0]


def _insert_subagent(core: Path, label: str, reserved_ms: int) -> None:
    payload = {
        "runId": "run-1",
        "taskRunId": "run-1",
        "childSessionKey": "agent:claude:acp:child",
        "controllerSessionKey": "owner",
        "requesterSessionKey": "owner",
        "requesterAgentId": "research-orchestrator",
        "spawnMode": "run",
        "label": label,
        "createdAt": reserved_ms + 1000,
        "execution": {
            "status": "terminal",
            "startedAt": reserved_ms + 2000,
            "endedAt": reserved_ms + 3000,
            "outcome": {"status": "ok"},
        },
    }
    with sqlite3.connect(core) as conn:
        conn.execute(
            """
            INSERT INTO subagent_runs (
              run_id, child_session_key, controller_session_key,
              requester_session_key, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "run-1",
                "agent:claude:acp:child",
                "owner",
                "owner",
                reserved_ms + 1000,
                json.dumps(payload),
            ),
        )


def _prepare_review(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    *,
    status: str = "succeeded",
    verdict: str = "PASS",
    findings: tuple[str, ...] = (),
) -> tuple[ResearchStore, str, Path, Path, Path, Path]:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(
        store,
        attempt_id,
        bundle,
        tmp_path,
        status=status,
        verdict=verdict,
        findings=findings,
    )
    return store, attempt_id, bundle, core, sessions, projects


def test_bundle_aggregate_budget_is_128_mib() -> None:
    assert review_evidence.MAX_BUNDLE_BYTES == 128 * 1024 * 1024


def test_bundle_per_file_and_test_evidence_limits_remain_8_mib() -> None:
    assert review_evidence.MAX_BUNDLE_FILE_BYTES == 8 * 1024 * 1024
    assert review_evidence.MAX_TEST_EVIDENCE_BYTES == 8 * 1024 * 1024


def test_bundle_digest_allows_root_generated_patch_above_file_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_FILE_BYTES", 4)
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_BYTES", 8)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "diff.patch").write_bytes(b"patch!!")
    (bundle / "instructions.md").write_bytes(b"i")

    digest = review_evidence._bundle_digest(bundle)

    assert len(digest) == 64


def test_bundle_digest_rejects_root_generated_patch_over_aggregate_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_FILE_BYTES", 4)
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_BYTES", 8)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "diff.patch").write_bytes(b"patch!!!!")

    with pytest.raises(BundleError):
        review_evidence._bundle_digest(bundle)


def test_bundle_digest_rejects_aggregate_overflow_even_when_each_file_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_FILE_BYTES", 4)
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_BYTES", 8)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "diff.patch").write_bytes(b"patch!")
    (bundle / "instructions.md").write_bytes(b"iii")

    with pytest.raises(BundleError, match="review bundle exceeds its size limit"):
        review_evidence._bundle_digest(bundle)


@pytest.mark.parametrize("relative", ["source/diff.patch", "nested/diff.patch"])
def test_bundle_digest_keeps_non_root_diff_namesakes_on_file_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_FILE_BYTES", 4)
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_BYTES", 8)
    bundle = tmp_path / "bundle"
    (bundle / Path(relative).parent).mkdir(parents=True)
    (bundle / "diff.patch").write_bytes(b"p")
    (bundle / relative).write_bytes(b"large")

    with pytest.raises(BundleError):
        review_evidence._bundle_digest(bundle)


def test_real_bundle_build_and_revalidation_accept_generated_patch_above_file_cap(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, source, _hypothesis = campaign
    source.chmod(0o755)
    for index in range(20):
        (source / f"generated-{index:02d}.txt").write_text("x" * 96 + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "generated patch"], cwd=source, check=True)
    source.chmod(0o555)

    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_FILE_BYTES", 2048)
    monkeypatch.setattr(review_evidence, "MAX_BUNDLE_BYTES", 64 * 1024)
    store, source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    attempt = store.get_attempt(attempt_id)
    expected_diff = subprocess.check_output(
        ["git", "diff", "--binary", _hypothesis.base_commit, str(attempt.commit)], cwd=source
    )
    assert len(expected_diff) > review_evidence.MAX_BUNDLE_FILE_BYTES

    reservation = reserve_review(store, attempt_id, bundle, "owner")
    replay = reserve_review(store, attempt_id, bundle, "owner")

    assert (bundle / "diff.patch").read_bytes() == expected_diff
    assert replay.bundle_sha256 == reservation.bundle_sha256


def test_default_bundle_instructions_include_parseable_strict_verdict_example(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)

    reservation = reserve_review(store, attempt_id, bundle, "owner")
    instructions = (bundle / "instructions.md").read_text()
    marker = "Valid JSON example:\n"
    before, separator, example = instructions.partition(marker)

    assert separator == marker
    assert "findings must be an array of nonempty strings" in before.lower()
    assert "containment-provenance.json" in before
    assert (
        "containment-provenance.json is the host-verified in-sandbox import provenance "
        "for the tested commit."
    ) in before
    payload = json.loads(example)
    assert set(payload) == {"verdict", "attempt_id", "commit", "spec_sha256", "findings"}
    assert payload["verdict"] in {"PASS", "FAIL"}
    assert payload["attempt_id"] == attempt_id
    assert payload["commit"] == reservation.commit
    assert payload["spec_sha256"] == hypothesis.spec_sha256
    assert payload["findings"] == [
        "severity=high; location=source/example.py:1; explanation=Example finding."
    ]

    event = {
        "message": {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": example}],
        }
    }
    verdict, findings, _ = review_evidence._final_verdict(event, reservation)

    assert verdict == "FAIL"
    assert findings == tuple(payload["findings"])


def test_new_bundle_without_provenance_is_refused(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    monkeypatch.setattr(review_evidence, "_stored_containment_provenance", lambda *_: None)

    with pytest.raises(BundleError, match="review bundle requires containment provenance evidence"):
        review_evidence.build_review_bundle(store, attempt_id, bundle)


def test_reservation_builds_actual_read_only_bundle_and_ack_replays(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = __import__(
        "gateway.research.review_evidence", fromlist=["reserve_review"]
    ).reserve_review(store, attempt_id, bundle, "owner")
    assert reservation.label.startswith(attempt_id + "-")
    assert (bundle / "source" / "tracked.txt").read_text() == "tracked"
    attempt = store.get_attempt(attempt_id)
    assert attempt.commit is not None
    assert json.loads((bundle / "containment-provenance.json").read_text()) == json.loads(
        provenance_evidence(attempt_id, attempt.commit)
    )
    instructions = (bundle / "instructions.md").read_text()
    assert f"attempt_id={attempt_id}" in instructions
    assert f"spec_sha256={hypothesis.spec_sha256}" in instructions
    assert not (bundle / "source" / "tracked.txt").stat().st_mode & 0o222
    ack = acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    assert (
        acknowledge_review(store, attempt_id, ack.child_session_key, ack.run_id, ack.mode, None)
        == ack
    )
    result = runner.invoke(
        app,
        [
            "research",
            "review-reserve",
            attempt_id,
            "--root",
            str(store.root),
            "--bundle-dir",
            str(bundle),
            "--owner-key",
            "owner",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["effort"] == "high"


@pytest.mark.parametrize(
    "campaign",
    [((".env.example", "EXAMPLE=1\n"), ("data/README.md", "fixture data docs\n"))],
    indirect=True,
)
def test_reservation_excludes_unchanged_blocked_baseline_files(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    source = campaign[1]
    source.chmod(0o755)
    (source / "tracked.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "implementation"], cwd=source, check=True)
    source.chmod(0o555)
    store, source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)

    reservation = reserve_review(store, attempt_id, bundle, "owner")

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    expected = sorted(
        subprocess.check_output(
            ["git", "rev-parse", f"{commit}:{path}"], cwd=source, text=True
        ).strip()
        + "\t"
        + path
        for path in (".env.example", "data/README.md")
    )
    assert (bundle / "source" / "EXCLUDED").read_text() == "\n".join(expected) + "\n"
    assert not (bundle / "source" / ".env.example").exists()
    assert not (bundle / "source" / "data").exists()
    assert ".env.example" not in (bundle / "diff.patch").read_text()
    assert "data/README.md" not in (bundle / "diff.patch").read_text()
    assert (
        reserve_review(store, attempt_id, bundle, "owner").bundle_sha256
        == reservation.bundle_sha256
    )
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(store, attempt_id, bundle, tmp_path)
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_PASSED
    )


def test_reservation_ignores_untracked_blocked_file(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    source = campaign[1]
    source.chmod(0o755)
    (source / ".env").write_text("LIVE=not-committed\n", encoding="utf-8")
    source.chmod(0o555)

    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)

    reserve_review(store, attempt_id, bundle, "owner")

    assert not (bundle / "source" / ".env").exists()
    assert not (bundle / "source" / "EXCLUDED").exists()


@pytest.mark.parametrize(
    "campaign",
    [((".env.example", "EXAMPLE=1\n"), ("data/README.md", "fixture data docs\n"))],
    indirect=True,
)
def test_changed_blocked_baseline_file_fails_closed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    source = campaign[1]
    source.chmod(0o755)
    (source / ".env.example").write_text("LIVE_SECRET=changed\n", encoding="utf-8")
    _commit_source(source, "change blocked baseline")

    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)

    with pytest.raises(BundleError, match="changed data or credential path"):
        reserve_review(store, attempt_id, bundle, "owner")
    assert not bundle.exists()
    with pytest.raises(ValueError):
        store.evidence(attempt_id, "review_reservation")


def test_added_blocked_files_fail_closed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    source = campaign[1]
    source.chmod(0o755)
    (source / ".env").write_text("LIVE_SECRET=added\n", encoding="utf-8")
    (source / "data").mkdir()
    (source / "data" / "dump.db").write_bytes(b"not a database")
    (source / "secrets").mkdir()
    (source / "secrets" / "token.pem").write_text("private", encoding="utf-8")
    _commit_source(source, "add blocked files")

    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)

    with pytest.raises(BundleError, match="changed data or credential path"):
        reserve_review(store, attempt_id, bundle, "owner")
    assert not bundle.exists()


def test_moved_implementation_into_blocked_directory_fails_closed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    source = campaign[1]
    source.chmod(0o755)
    (source / "data").mkdir()
    subprocess.run(["git", "mv", "tracked.txt", "data/implementation.py"], cwd=source, check=True)
    _commit_source(source, "move implementation into data")

    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)

    with pytest.raises(BundleError, match="changed data or credential path"):
        reserve_review(store, attempt_id, bundle, "owner")
    assert not bundle.exists()


@pytest.mark.parametrize(
    "campaign",
    [((".env.example", "EXAMPLE=1\n"),)],
    indirect=True,
)
def test_executable_blocked_baseline_file_fails_closed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    source = campaign[1]
    source.chmod(0o755)
    (source / ".env.example").chmod(0o755)
    _commit_source(source, "make blocked baseline executable")

    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)

    with pytest.raises(BundleError, match="changed data or credential path"):
        reserve_review(store, attempt_id, bundle, "owner")
    assert not bundle.exists()


def test_added_symlink_fails_closed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    source = campaign[1]
    source.chmod(0o755)
    (source / "link").symlink_to("tracked.txt")
    _commit_source(source, "add symlink")

    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)

    with pytest.raises(BundleError, match="symlink or unsupported entry"):
        reserve_review(store, attempt_id, bundle, "owner")
    assert not bundle.exists()


@pytest.mark.parametrize(
    "campaign",
    [((".env.example", "EXAMPLE=1\n"), ("data/README.md", "fixture data docs\n"))],
    indirect=True,
)
def test_excluded_metadata_tampering_is_rejected(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    bundle.chmod(0o755)
    excluded = bundle / "source" / "EXCLUDED"
    excluded.chmod(0o644)
    excluded.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(BundleError):
        reserve_review(store, attempt_id, bundle, "owner")


@pytest.mark.parametrize(
    "campaign",
    [((".env.example", "EXAMPLE=1\n"), ("data/README.md", "fixture data docs\n"))],
    indirect=True,
)
def test_planted_blocked_bundle_file_is_rejected(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    bundle.chmod(0o755)
    source_dir = bundle / "source"
    source_dir.chmod(0o755)
    planted = source_dir / ".env.example"
    planted.write_text("PLANTED=1\n", encoding="utf-8")
    planted.chmod(0o444)
    source_dir.chmod(0o555)
    bundle.chmod(0o555)

    with pytest.raises(BundleError, match="data or credential path"):
        reserve_review(store, attempt_id, bundle, "owner")


def test_evaluation_spec_copy_tampering_is_rejected(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    spec_copy = bundle / "evaluation-specs" / "c000.json"
    bundle.chmod(0o755)
    spec_copy.chmod(0o644)
    spec_copy.write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(BundleError):
        reserve_review(store, attempt_id, bundle, "owner")


@pytest.mark.parametrize("filename", ["run-plan.json", "evaluation-spec-set.json"])
def test_run_plan_or_spec_set_bundle_tampering_is_rejected(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    filename: str,
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    bundle.chmod(0o755)
    target = bundle / filename
    target.chmod(0o644)
    target.write_text("{}\n", encoding="utf-8")
    target.chmod(0o444)
    bundle.chmod(0o555)

    with pytest.raises(BundleError):
        reserve_review(store, attempt_id, bundle, "owner")


def test_reservation_accepts_nested_tracked_source_directories(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    _store, source, _hypothesis = campaign
    source.chmod(0o755)
    nested = source / "fixture" / "nested" / "artifact.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "fixture"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "nested fixture"], cwd=source, check=True)
    source.chmod(0o555)

    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = reserve_review(store, attempt_id, bundle, "owner")

    assert (bundle / "source" / "fixture" / "nested" / "artifact.py").read_text() == "VALUE = 1\n"
    assert (
        reserve_review(store, attempt_id, bundle, "owner").bundle_sha256
        == reservation.bundle_sha256
    )


def test_reservation_replay_and_collect_use_frozen_test_evidence_copy(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = reserve_review(store, attempt_id, bundle, "owner")
    implementation = json.loads(store.evidence(attempt_id, "implementation"))
    Path(str(implementation["test_evidence_path"])).unlink()

    replay = reserve_review(store, attempt_id, bundle, "owner")
    assert replay.bundle_sha256 == reservation.bundle_sha256
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(store, attempt_id, bundle, tmp_path)

    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_PASSED
    )


def test_collect_requires_terminal_host_evidence_and_is_idempotent(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(store, attempt_id, bundle, tmp_path)
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_PASSED
    )
    assert json.loads(store.evidence(attempt_id, "review_host_evidence"))["task_source"] == (
        "task_runs"
    )
    attempt_dir = store.root / "hypotheses" / "H0001" / "attempts" / attempt_id
    (attempt_dir / "review.json").unlink()
    (attempt_dir / "review_host_evidence.json").unlink()
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_PASSED
    )
    assert (attempt_dir / "review.json").is_file()
    assert (attempt_dir / "review_host_evidence.json").is_file()
    assert len([row for row in store.events() if row.kind == "review_collected"]) == 1
    assert _queue(store, attempt_id, "job-evidence").state == AttemptState.RUN_QUEUED


def test_collect_recovers_from_pruned_task_run_using_subagent_run(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    attempt = store.get_attempt(attempt_id)
    reservation = json.loads(store.evidence(attempt_id, "review_reservation"))
    reserved_ms = int(
        datetime.fromisoformat(str(reservation["reserved_at"]).replace("Z", "+00:00")).timestamp()
        * 1000
    )
    with sqlite3.connect(core) as conn:
        conn.execute("DELETE FROM task_runs")
    _insert_subagent(core, str(reservation["label"]), reserved_ms)
    worktree = Path(attempt.worktree_path)
    for path in worktree.rglob("*"):
        path.chmod(path.stat().st_mode | 0o700)
    worktree.chmod(worktree.stat().st_mode | 0o700)
    shutil.rmtree(attempt.worktree_path)

    result = collect_review(store, attempt_id, core, sessions, projects)

    assert result.state == AttemptState.REVIEW_PASSED
    review = json.loads(store.evidence(attempt_id, "review"))
    host = json.loads(store.evidence(attempt_id, "review_host_evidence"))
    assert review["verdict"] == "PASS"
    assert review["findings"] == []
    assert host["task_source"] == "subagent_runs"
    assert "reason" not in host


def test_collect_records_transcript_bound_bundle_mutation_failure(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    bundle.chmod(0o755)
    instructions = bundle / "instructions.md"
    instructions.chmod(0o644)
    instructions.write_text("changed", encoding="utf-8")
    instructions.chmod(0o444)
    bundle.chmod(0o555)

    result = collect_review(store, attempt_id, core, sessions, projects)

    assert result.state == AttemptState.REVIEW_FAILED
    review = json.loads(store.evidence(attempt_id, "review"))
    host = json.loads(store.evidence(attempt_id, "review_host_evidence"))
    assert review["verdict"] == "FAIL"
    assert review["findings"] == ["bundle_mutated"]
    assert host["reason"] == "bundle_mutated"
    assert host["transcript_sha256"]


def test_collect_supersedes_failed_verification_once(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)

    def fail_reserved_bundle(_root: Path, _expected_digest: str) -> str:
        raise BundleError("x")

    monkeypatch.setattr(review_evidence, "_verify_reserved_bundle", fail_reserved_bundle)
    first = collect_review(store, attempt_id, core, sessions, projects)
    assert first.state == AttemptState.REVIEW_FAILED
    assert json.loads(store.evidence(attempt_id, "review"))["findings"] == ["bundle_invalid: x"]

    monkeypatch.undo()
    second = collect_review(store, attempt_id, core, sessions, projects)

    assert second.state == AttemptState.REVIEW_PASSED
    assert json.loads(store.evidence(attempt_id, "review"))["verdict"] == "PASS"
    assert json.loads(store.evidence(attempt_id, "review_host_evidence"))["verdict"] == "PASS"
    attempt_dir = store.root / "hypotheses" / "H0001" / "attempts" / attempt_id
    assert (attempt_dir / "review_superseded.json").is_file()
    assert (attempt_dir / "review_host_evidence_superseded.json").is_file()
    with store._connect() as conn:
        kinds = [
            str(row[0])
            for row in conn.execute(
                "SELECT kind FROM attempt_evidence WHERE attempt_id=? ORDER BY kind",
                (attempt_id,),
            ).fetchall()
        ]
    assert "review_superseded" in kinds
    assert "review_host_evidence_superseded" in kinds
    assert len([event for event in store.events() if event.kind == "review_recollected"]) == 1

    events_before = store.events()
    replay = collect_review(store, attempt_id, core, sessions, projects)
    assert replay == second
    assert store.events() == events_before


def test_collect_supersedes_failed_verification_with_transcript_fail(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = (
        "severity=high; location=source/a.py:1; explanation=first finding",
        "severity=medium; location=source/b.py:2; explanation=second finding",
        "severity=low; location=source/c.py:3; explanation=third finding",
        "severity=high; location=source/d.py:4; explanation=fourth finding",
    )
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(
        campaign, tmp_path, verdict="FAIL", findings=findings
    )

    def fail_reserved_bundle(_root: Path, _expected_digest: str) -> str:
        raise BundleError("x")

    monkeypatch.setattr(review_evidence, "_verify_reserved_bundle", fail_reserved_bundle)
    assert collect_review(store, attempt_id, core, sessions, projects).state == (
        AttemptState.REVIEW_FAILED
    )
    monkeypatch.undo()

    result = collect_review(store, attempt_id, core, sessions, projects)

    assert result.state == AttemptState.REVIEW_FAILED
    review = json.loads(store.evidence(attempt_id, "review"))
    assert review["verdict"] == "FAIL"
    assert review["findings"] == list(findings)
    assert len([event for event in store.events() if event.kind == "review_recollected"]) == 1


def test_historical_reserved_bundle_without_provenance_supersedes_without_worktree(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = (
        "severity=high; location=source/a.py:1; explanation=first finding",
        "severity=medium; location=source/b.py:2; explanation=second finding",
        "severity=low; location=source/c.py:3; explanation=third finding",
        "severity=high; location=source/d.py:4; explanation=fourth finding",
    )
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = _reserve_legacy_bundle(store, attempt_id, bundle)
    assert not (bundle / "containment-provenance.json").exists()
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(
        store, attempt_id, bundle, tmp_path, verdict="FAIL", findings=findings
    )
    attempt = store.get_attempt(attempt_id)
    worktree = Path(attempt.worktree_path)
    for path in worktree.rglob("*"):
        path.chmod(path.stat().st_mode | 0o700)
    worktree.chmod(worktree.stat().st_mode | 0o700)
    shutil.rmtree(worktree)

    def fail_reserved_bundle(_root: Path, expected_digest: str) -> str:
        assert expected_digest == reservation.bundle_sha256
        raise BundleError("historical host failure")

    monkeypatch.setattr(review_evidence, "_verify_reserved_bundle", fail_reserved_bundle)
    first = collect_review(store, attempt_id, core, sessions, projects)
    assert first.state == AttemptState.REVIEW_FAILED
    assert json.loads(store.evidence(attempt_id, "review"))["findings"] == [
        "bundle_invalid: historical host failure"
    ]

    monkeypatch.undo()
    second = collect_review(store, attempt_id, core, sessions, projects)
    assert second.state == AttemptState.REVIEW_FAILED
    review = json.loads(store.evidence(attempt_id, "review"))
    assert review["verdict"] == "FAIL"
    assert review["findings"] == list(findings)
    attempt_dir = store.root / "hypotheses" / "H0001" / "attempts" / attempt_id
    assert json.loads((attempt_dir / "review_superseded.json").read_text())["findings"] == [
        "bundle_invalid: historical host failure"
    ]
    assert (attempt_dir / "review_host_evidence_superseded.json").is_file()
    with store._connect() as conn:
        counts = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT kind, COUNT(*) FROM attempt_evidence WHERE attempt_id=? GROUP BY kind",
                (attempt_id,),
            ).fetchall()
        }
    assert counts["review_superseded"] == 1
    assert counts["review_host_evidence_superseded"] == 1
    assert len([event for event in store.events() if event.kind == "review_recollected"]) == 1

    events_before = store.events()
    assert collect_review(store, attempt_id, core, sessions, projects) == second
    assert store.events() == events_before


def test_transcript_verified_review_is_never_superseded(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    first = collect_review(store, attempt_id, core, sessions, projects)
    events_before = store.events()

    def fail_reserved_bundle(_root: Path, _expected_digest: str) -> str:
        raise AssertionError("transcript-verified evidence must replay without re-verification")

    monkeypatch.setattr(review_evidence, "_verify_reserved_bundle", fail_reserved_bundle)
    replay = collect_review(store, attempt_id, core, sessions, projects)

    assert replay == first
    assert store.events() == events_before


def test_reconcile_review_recovers_ack_from_subagent_run(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reservation = reserve_review(store, attempt_id, bundle, "owner")
    core, _sessions, _projects = _host_fixture(store, attempt_id, bundle, tmp_path)
    reservation_ms = int(
        datetime.fromisoformat(reservation.reserved_at.replace("Z", "+00:00")).timestamp() * 1000
    )
    with sqlite3.connect(core) as conn:
        conn.execute("DELETE FROM task_runs")
    _insert_subagent(core, reservation.label, reservation_ms)

    ack = reconcile_review(store, attempt_id, core)

    assert ack is not None
    assert ack.run_id == "run-1"
    assert ack.child_session_key == "agent:claude:acp:child"


def test_collect_nonterminal_is_pending_and_bundle_mutation_is_rejected(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(store, attempt_id, bundle, tmp_path, status="running")
    with pytest.raises(ReviewPending):
        collect_review(store, attempt_id, core, sessions, projects)
    bundle.chmod(0o755)
    (bundle / "instructions.md").chmod(0o644)
    (bundle / "instructions.md").write_text("changed", encoding="utf-8")
    with pytest.raises(BundleError):
        reserve_review(store, attempt_id, bundle, "owner")


def test_collect_wrong_owner_is_unresolved_and_pauses_campaign(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "wrong-owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, sessions, projects = _host_fixture(store, attempt_id, bundle, tmp_path)
    with pytest.raises(ReviewUnresolved):
        collect_review(store, attempt_id, core, sessions, projects)
    assert store.campaign()[0] == "PAUSED"


@pytest.mark.parametrize(
    "format_name",
    (
        "fenced",
        "prose",
        "trailing",
        "duplicate",
        "extra",
        "multiple",
        "object_findings",
        "tool_call",
    ),
)
def test_collect_rejects_non_bare_or_non_exact_verdicts(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    format_name: str,
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    transcript = _transcript_path(projects)
    event = json.loads(transcript.read_text())
    verdict_text = str(event["message"]["content"][0]["text"])
    if format_name == "fenced":
        verdict_text = f"```json\n{verdict_text}\n```"
    elif format_name == "prose":
        verdict_text = f"Review complete: {verdict_text}"
    elif format_name == "trailing":
        verdict_text += " trailing prose"
    elif format_name == "duplicate":
        verdict_text = verdict_text.replace(
            '"verdict": "PASS"', '"verdict": "PASS", "verdict": "PASS"'
        )
    elif format_name == "extra":
        verdict = json.loads(verdict_text)
        verdict["extra"] = "reject"
        verdict_text = json.dumps(verdict)
    elif format_name == "multiple":
        verdict_text = f"{verdict_text} {verdict_text}"
    elif format_name == "object_findings":
        verdict = json.loads(verdict_text)
        verdict["findings"] = [{"severity": "high", "explanation": "reject"}]
        verdict_text = json.dumps(verdict)
    else:
        event["message"]["content"] = [{"type": "tool_use", "id": "tool"}]
    if format_name != "tool_call":
        event["message"]["content"] = [{"type": "text", "text": verdict_text}]
    transcript.write_text(json.dumps(event) + "\n")

    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )


@pytest.mark.parametrize("field,value", (("model", "claude-sonnet"), ("effort", "low")))
def test_collect_rejects_wrong_model_or_effort(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    transcript = _transcript_path(projects)
    event = json.loads(transcript.read_text())
    if field == "model":
        event["message"][field] = value
    else:
        event[field] = value
    transcript.write_text(json.dumps(event) + "\n")
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )


def test_collect_rejects_non_terminal_final_assistant_event(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    transcript = _transcript_path(projects)
    event = json.loads(transcript.read_text())
    event["message"]["stop_reason"] = "tool_use"
    transcript.write_text(json.dumps(event) + "\n")
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )


def test_identity_or_transcript_host_error_is_unresolved(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    shutil.rmtree(sessions)
    sessions.mkdir()
    (sessions / "malformed.json").write_text("{}")
    with pytest.raises(ReviewUnresolved):
        collect_review(store, attempt_id, core, sessions, projects)
    assert store.get_attempt(attempt_id).state == AttemptState.IMPLEMENTED
    assert any(event.kind == "review_unresolved" for event in store.events())


def test_missing_transcript_is_unresolved(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    _transcript_path(projects).unlink()
    with pytest.raises(ReviewUnresolved):
        collect_review(store, attempt_id, core, sessions, projects)
    assert store.get_attempt(attempt_id).state == AttemptState.IMPLEMENTED


def test_two_matching_tasks_are_unresolved(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    with sqlite3.connect(core) as conn:
        conn.execute("INSERT INTO task_runs SELECT * FROM task_runs")
        conn.commit()
    with pytest.raises(ReviewUnresolved):
        collect_review(store, attempt_id, core, sessions, projects)
    assert store.campaign()[0] == "PAUSED"


def test_cancel_uses_exact_task_and_unknown_response_stays_pending(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, attempt_id, bundle = _setup(campaign, tmp_path)
    reserve_review(store, attempt_id, bundle, "owner")
    acknowledge_review(store, attempt_id, "agent:claude:acp:child", "run-1", "run", None)
    core, _sessions, _projects = _host_fixture(
        store, attempt_id, bundle, tmp_path, status="running"
    )
    calls: list[tuple[str, str]] = []

    async def lost_response(task_id: str, reason: str) -> dict[str, object]:
        calls.append((task_id, reason))
        raise OpenClawTransportError("connection lost after send")

    outcome = cancel_review(store, attempt_id, "stop review", core, lost_response)
    assert calls == [("task-1", "stop review")]
    assert outcome.pending is True
    assert outcome.rpc_result == "UNKNOWN_RESPONSE"
    assert json.loads(store.evidence(attempt_id, "review_cancel"))["task_id"] == "task-1"

    replay = cancel_review(store, attempt_id, "stop review", core, lost_response)
    assert replay.pending is True
    assert calls == [("task-1", "stop review")]


def test_cancel_rejection_is_pending_until_terminal_reread(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, _sessions, _projects = _prepare_review(
        campaign, tmp_path, status="running"
    )

    async def rejected(task_id: str, reason: str) -> dict[str, object]:
        del reason
        return {"taskId": task_id, "status": "rejected"}

    outcome = cancel_review(store, attempt_id, "stop review", core, rejected)
    assert outcome.pending is True
    assert outcome.status == "pending"
    assert outcome.rpc_result == "RPC_REJECTED"


def test_cancel_unexpected_error_uses_terminal_reread(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, _sessions, _projects = _prepare_review(
        campaign, tmp_path, status="running"
    )

    async def errored(task_id: str, reason: str) -> dict[str, object]:
        del reason
        with sqlite3.connect(core) as conn:
            conn.execute(
                "UPDATE task_runs SET status='cancelled', ended_at=created_at+3000 WHERE task_id=?",
                (task_id,),
            )
            conn.commit()
        raise RuntimeError("unexpected transport boundary")

    outcome = cancel_review(store, attempt_id, "stop review", core, errored)
    assert outcome.pending is False
    assert outcome.status == "cancelled"
    assert outcome.rpc_result == "RPC_ERROR:RuntimeError"


def test_cancel_unexpected_error_replay_stays_pending_without_resend(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, _sessions, _projects = _prepare_review(
        campaign, tmp_path, status="running"
    )
    calls = 0

    async def errored(task_id: str, reason: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        del task_id, reason
        raise RuntimeError("unexpected transport boundary")

    outcome = cancel_review(store, attempt_id, "stop review", core, errored)
    replay = cancel_review(store, attempt_id, "stop review", core, errored)
    assert outcome.pending is True
    assert replay.pending is True
    assert replay.rpc_result == "RPC_ERROR:RuntimeError"
    assert calls == 1


def test_bundle_mutation_verification_fails_closed(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, bundle, core, sessions, projects = _prepare_review(campaign, tmp_path)
    bundle.chmod(0o755)
    (bundle / "test-evidence").chmod(0o644)
    (bundle / "test-evidence").write_text("mutated", encoding="utf-8")
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )
    assert (
        json.loads(store.evidence(attempt_id, "review_host_evidence"))["reason"]
        == "bundle_invalid: bundle directory is mutable"
    )


def test_queue_refuses_missing_or_forced_review_host_evidence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, _bundle = _setup(campaign, tmp_path)
    with pytest.raises(StoreConflict):
        _queue(store, attempt_id, "job-missing-review")
    attempt = store.get_attempt(attempt_id)
    forced = replace(
        attempt,
        state=AttemptState.REVIEW_PASSED,
        review_verdict="PASS",
        review_commit=attempt.commit,
        review_spec_sha256=hypothesis.spec_sha256,
    )
    store.set_state(forced, event="test_forced_review_pass")
    with pytest.raises(StoreConflict):
        _queue(store, attempt_id, "job-forced-review")


def test_queue_refuses_forced_pass_with_fail_host_evidence(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, attempt_id, _bundle, core, sessions, projects = _prepare_review(
        campaign, tmp_path, verdict="FAIL"
    )
    assert (
        collect_review(store, attempt_id, core, sessions, projects).state
        == AttemptState.REVIEW_FAILED
    )
    attempt = store.get_attempt(attempt_id)
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    forced = replace(
        attempt,
        state=AttemptState.REVIEW_PASSED,
        review_verdict="PASS",
        review_commit=attempt.commit,
        review_spec_sha256=hypothesis.spec_sha256,
    )
    store.set_state(forced, event="test_forced_review_pass")
    with pytest.raises(StoreConflict):
        _queue(store, attempt_id, "job-fail-review")


def test_self_report_submit_is_a_tombstone(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, hypothesis, attempt_id, _bundle = _setup(campaign, tmp_path)
    attempt = store.get_attempt(attempt_id)
    record = ReviewRecord(
        attempt_id,
        str(attempt.commit),
        hypothesis.spec_sha256,
        "PASS",
        (),
        "claude-opus-5",
        "claude-opus-5",
        "session",
        "2026-01-01T00:00:00Z",
    )
    with pytest.raises(StoreConflict, match="review-submit was removed"):
        store.submit_review(attempt_id, record)  # type: ignore[arg-type]


def test_all_decided_wake_requests_next_hypothesis_authoring(
    campaign: tuple[ResearchStore, Path, HypothesisSpec],
) -> None:
    from gateway.research.wake import compose_wake

    store, _source, hypothesis = campaign
    store.freeze(hypothesis.hypothesis_id)
    store.decide_hypothesis(hypothesis.hypothesis_id, HypothesisDecision.FINISHED, "done")
    plan = compose_wake(store)
    assert plan is not None
    assert plan.state == "ALL_DECIDED"
    assert plan.hypothesis_id == "H0002"
    assert "author" in plan.message
    assert "freeze" in plan.message


def test_implemented_wake_requests_high_effort_review(
    campaign: tuple[ResearchStore, Path, HypothesisSpec], tmp_path: Path
) -> None:
    store, _source, _hypothesis, _attempt_id, _bundle = _setup(campaign, tmp_path)
    from gateway.research.wake import compose_wake

    plan = compose_wake(store)
    assert plan is not None
    assert plan.state == "IMPLEMENTED"
    assert "effort=high" in plan.message
