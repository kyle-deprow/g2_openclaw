"""Bounded, host-observed ACP review evidence.

This module is the review boundary for the research driver.  A reviewer does
not submit a JSON file that changes campaign state: the driver reserves and
freezes a source bundle, correlates one exact ACP task, reads one exact Claude
transcript, and only then asks the existing store to apply a verified verdict.

The host readers trust same-user SQLite/filesystem metadata.  They detect
wrong routing, stale identities, mutation, and model substitution; they are
not hostile-root security.  No network call is made here unless a caller
injects the narrow cancellation RPC callable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from gateway.openclaw_client import OpenClawError, OpenClawTransportError

from .codec import to_json
from .contracts import (
    Attempt,
    AttemptState,
    EvaluationSpecSet,
    ReviewEvidence,
    ReviewRecord,
    RunPlan,
)
from .host_records import (
    AcpIdentityHostRecord,
    HostRecordError,
    TaskRunHostRecord,
    read_exact_acpx_identity,
    read_exact_claude_transcript,
    read_exact_task_run,
)
from .store import ResearchStore, StoreConflict, now_utc

MAX_BUNDLE_FILE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_TEST_EVIDENCE_BYTES = 8 * 1024 * 1024
CLOCK_TOLERANCE_MS = 2_000
REVIEW_MODEL = "claude-opus-5"
REVIEW_EFFORT = "high"
REVIEW_TASK_RUNTIME = "acp"
REVIEW_TASK_SCOPE = "session"
REVIEW_AGENT = "claude"
REVIEW_BACKEND = "acpx"
REVIEW_ACP_MODE = "oneshot"


class ReviewEvidenceError(RuntimeError):
    """Review evidence is malformed, mismatched, or unavailable."""


class ReviewPending(ReviewEvidenceError):
    """The exact task is known but has not reached a terminal state."""


class ReviewUnresolved(ReviewEvidenceError):
    """Host correlation is zero, ambiguous, or otherwise unresolved."""


class BundleError(ReviewEvidenceError):
    """The immutable review bundle cannot be created or verified."""


@dataclass(frozen=True, slots=True)
class ReviewReservation:
    attempt_id: str
    commit: str
    hypothesis_spec_sha256: str
    bundle_dir: str
    bundle_sha256: str
    reserved_at: str
    owner_session_key: str
    label: str
    reservation_nonce: str

    def to_json(self) -> str:
        return to_json(
            {
                "attempt_id": self.attempt_id,
                "commit": self.commit,
                "hypothesis_spec_sha256": self.hypothesis_spec_sha256,
                "bundle_dir": self.bundle_dir,
                "bundle_sha256": self.bundle_sha256,
                "reserved_at": self.reserved_at,
                "owner_session_key": self.owner_session_key,
                "label": self.label,
                "reservation_nonce": self.reservation_nonce,
            }
        )


@dataclass(frozen=True, slots=True)
class ReviewAck:
    child_session_key: str
    run_id: str
    mode: str
    run_timeout_seconds: int | None
    acked_at: str

    def to_json(self) -> str:
        return to_json(
            {
                "child_session_key": self.child_session_key,
                "run_id": self.run_id,
                "mode": self.mode,
                "run_timeout_seconds": self.run_timeout_seconds,
                "acked_at": self.acked_at,
            }
        )


@dataclass(frozen=True, slots=True)
class ReviewVerification:
    review: ReviewEvidence
    host_payload: str


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    attempt_id: str
    task_id: str | None
    status: str
    pending: bool
    rpc_result: str


CancelTransport = Callable[[str, str], Awaitable[Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class _TrackedSource:
    files: tuple[tuple[str, bytes], ...]
    excluded: tuple[str, ...]


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise BundleError(f"{label} must be an absolute non-symlink file")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise BundleError(f"{label} is missing or unreadable") from exc
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode) or first.st_size > limit:
            raise BundleError(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise BundleError(f"{label} exceeds its size limit")
        if os.fstat(descriptor).st_size != total:
            raise BundleError(f"{label} changed while being read")
        return b"".join(chunks)
    except BundleError:
        raise
    except OSError as exc:
        raise BundleError(f"{label} could not be read") from exc
    finally:
        os.close(descriptor)


def _source_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not value
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BundleError("bundle source contains an unsafe path")
    return "/".join(path.parts)


def _is_blocked_source_path(value: str) -> bool:
    path = PurePosixPath(value)
    blocked_names = {
        ".aws",
        ".env",
        ".ssh",
        ".venv",
        "credentials",
        "data",
        "datasets",
        "env",
        "node_modules",
        "secrets",
        "venv",
    }
    blocked_suffixes = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
    for part in path.parts:
        lowered = part.lower()
        if (
            lowered in blocked_names
            or lowered.startswith(".env.")
            or "credential" in lowered
            or "secret" in lowered
            or lowered.endswith(tuple(blocked_suffixes))
            or lowered.endswith(".ipynb")
        ):
            return True
    return False


def _safe_source_path(value: str) -> str:
    normalized = _source_path(value)
    if _is_blocked_source_path(normalized):
        raise BundleError("bundle source contains a data or credential path")
    return normalized


def _git(worktree: Path, *args: str, max_bytes: int = MAX_BUNDLE_BYTES) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BundleError("committed review source could not be read") from exc
    if len(result.stdout) > max_bytes:
        raise BundleError("committed review source exceeds the bundle limit")
    return result.stdout


def _tree_entries(worktree: Path, commit: str) -> dict[str, tuple[str, str, str]]:
    raw = _git(worktree, "ls-tree", "-r", "-z", commit)
    entries: dict[str, tuple[str, str, str]] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        header, separator, raw_path = entry.partition(b"\t")
        fields = header.split()
        if separator == b"" or len(fields) != 3:
            raise BundleError("committed source tree is malformed")
        try:
            mode, entry_type, blob_sha = (field.decode("ascii") for field in fields)
            path = _source_path(raw_path.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BundleError("committed source tree is malformed") from exc
        if path in entries:
            raise BundleError("committed source tree contains a duplicate path")
        entries[path] = (mode, entry_type, blob_sha)
    return entries


def _excluded_bytes(excluded: tuple[str, ...]) -> bytes:
    if not excluded:
        return b""
    return ("\n".join(excluded) + "\n").encode("utf-8")


def _tracked_source(worktree: Path, commit: str, base_commit: str) -> _TrackedSource:
    if not worktree.is_absolute() or not worktree.is_dir() or worktree.is_symlink():
        raise BundleError("committed review source worktree is missing or unsafe")
    current = _tree_entries(worktree, commit)
    baseline = _tree_entries(worktree, base_commit)
    files: list[tuple[str, bytes]] = []
    excluded: list[str] = []
    for path in sorted(current):
        mode, entry_type, blob_sha = current[path]
        if mode not in {"100644", "100755"} or entry_type != "blob":
            raise BundleError("review source contains a symlink or unsupported entry")
        if _is_blocked_source_path(path):
            if mode != "100644" or baseline.get(path) != ("100644", "blob", blob_sha):
                raise BundleError("bundle source contains a changed data or credential path")
            excluded.append(f"{blob_sha}\t{path}")
            continue
        content = _git(worktree, "show", f"{commit}:{path}", max_bytes=MAX_BUNDLE_FILE_BYTES)
        files.append((path, content))
    if not files:
        raise BundleError("review source commit has no tracked files")
    return _TrackedSource(tuple(files), tuple(sorted(excluded)))


def _bundle_digest(root: Path) -> str:
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise BundleError("bundle directory must be an absolute regular directory")
    digest = hashlib.sha256()
    total = 0
    seen: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise BundleError("bundle contains a symlink or non-file entry")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleError("bundle contains a symlink or non-file entry")
        _safe_source_path(relative)
        content = _read_bounded(path, MAX_BUNDLE_FILE_BYTES, f"bundle file {relative}")
        total += len(content)
        if total > MAX_BUNDLE_BYTES:
            raise BundleError("review bundle exceeds its size limit")
        seen.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    if not seen:
        raise BundleError("review bundle is empty")
    return digest.hexdigest()


def _require_immutable(path: Path, label: str) -> None:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise BundleError(f"{label} is missing or unreadable") from exc
    if mode & 0o222:
        raise BundleError(f"{label} is mutable")


def _readonly_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise BundleError("bundle contains a symlink")
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        elif path.is_dir():
            path.chmod(
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
    root.chmod(
        stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )


def _git_diff(worktree: Path, base_commit: str, commit: str) -> bytes:
    return _git(worktree, "diff", "--binary", base_commit, commit)


def build_review_bundle(
    store: ResearchStore,
    attempt_id: str,
    bundle_dir: Path,
    *,
    instructions: str | None = None,
) -> str:
    """Build and freeze a review bundle from the committed implementation.

    The source directory contains the actual committed files, not only a list
    of hashes.  The target is never overwritten: an existing directory is
    verified instead, which makes retries safe and prevents accidental bundle
    replacement.
    """

    attempt = store.get_attempt(attempt_id)
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    if attempt.state != AttemptState.IMPLEMENTED or attempt.commit is None:
        raise BundleError("review bundle requires an IMPLEMENTED attempt")
    if hypothesis.state.value != "FROZEN":
        raise BundleError("review bundle requires a frozen hypothesis")
    if not bundle_dir.is_absolute():
        raise BundleError("bundle directory must be absolute")
    source = Path(attempt.worktree_path)
    if not source.is_absolute() or not source.is_dir() or source.is_symlink():
        raise BundleError("attempt worktree is missing or not a directory")
    spec_bytes = hypothesis.spec_json.encode("utf-8")
    if hashlib.sha256(spec_bytes).hexdigest() != hypothesis.spec_sha256:
        raise BundleError("frozen hypothesis spec digest does not match its bytes")
    implementation = json.loads(store.evidence(attempt_id, "implementation"))
    run_plan_payload = _stored_json(store, attempt_id, "run_plan")
    run_plan = (
        RunPlan.from_json(json.dumps(run_plan_payload, sort_keys=True, separators=(",", ":")))
        if run_plan_payload is not None
        else None
    )
    spec_set: EvaluationSpecSet | None = None
    if run_plan is not None:
        try:
            spec_set = store.evaluation_spec_set(hypothesis.hypothesis_id)
        except ValueError as exc:
            raise BundleError("run plan requires immutable evaluation spec-set evidence") from exc
        if (
            run_plan.evaluation_spec_set_sha256
            != hashlib.sha256(spec_set.to_json().encode()).hexdigest()
        ):
            raise BundleError("run plan evaluation spec-set digest differs from evidence")
    test_path = implementation.get("test_evidence_path")
    if not isinstance(test_path, str) or not test_path:
        raise BundleError("implementation has no bounded test evidence path")
    test_bytes = _read_bounded(Path(test_path), MAX_TEST_EVIDENCE_BYTES, "test evidence")
    tracked = _tracked_source(source, attempt.commit, hypothesis.base_commit)
    diff = _git_diff(source, hypothesis.base_commit, attempt.commit)
    if len(diff) > MAX_BUNDLE_BYTES:
        raise BundleError("implementation diff exceeds the bundle limit")
    text = instructions or (
        "Review only the committed source under source/. Your entire final response must be one "
        "bare JSON object: first character { and last character }, with no markdown or prose. "
        f"with verdict, attempt_id={attempt.attempt_id}, commit={attempt.commit}, "
        f"spec_sha256={hypothesis.spec_sha256}, and findings."
    )
    if tracked.excluded and instructions is None:
        text += (
            " The source/EXCLUDED file records unchanged frozen-baseline paths "
            "omitted from source/."
        )
    instruction_bytes = text.encode("utf-8")
    if len(instruction_bytes) > MAX_BUNDLE_FILE_BYTES:
        raise BundleError("review instructions exceed the bundle file limit")
    target_exists = bundle_dir.exists() or bundle_dir.is_symlink()
    if target_exists:
        if bundle_dir.is_symlink() or not bundle_dir.is_dir():
            raise BundleError("bundle target is not a regular directory")
        reservation_payload = _stored_json(store, attempt_id, "review_reservation")
        expected_digest = (
            None
            if reservation_payload is None
            else _reservation_from_payload(reservation_payload).bundle_sha256
        )
        digest = _validate_bundle(
            store,
            attempt_id,
            bundle_dir,
            expected_digest=expected_digest,
        )
        _readonly_tree(bundle_dir)
        return digest

    parent = bundle_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{bundle_dir.name}.", dir=parent))
    try:
        (temporary / "source").mkdir()
        (temporary / "spec.json").write_bytes(spec_bytes)
        (temporary / "diff.patch").write_bytes(diff)
        (temporary / "test-evidence").write_bytes(test_bytes)
        (temporary / "instructions.md").write_bytes(instruction_bytes)
        (temporary / "source" / "COMMIT").write_text(attempt.commit + "\n", encoding="utf-8")
        if tracked.excluded:
            (temporary / "source" / "EXCLUDED").write_bytes(_excluded_bytes(tracked.excluded))
        if run_plan is not None and spec_set is not None:
            (temporary / "run-plan.json").write_text(run_plan.to_json(), encoding="utf-8")
            (temporary / "evaluation-spec-set.json").write_text(
                spec_set.to_json(), encoding="utf-8"
            )
            for entry in spec_set.specs:
                destination = temporary / "evaluation-specs" / f"{entry.spec_id}.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(Path(entry.path).read_bytes())
        for relative, content in tracked.files:
            destination = temporary / "source" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        _readonly_tree(temporary)
        os.replace(temporary, bundle_dir)
        temporary = Path()
    except Exception:
        if temporary != Path() and temporary.exists():
            shutil.rmtree(temporary)
        raise
    return _bundle_digest(bundle_dir)


def _validate_bundle(
    store: ResearchStore,
    attempt_id: str,
    root: Path,
    *,
    expected_digest: str | None = None,
) -> str:
    attempt = store.get_attempt(attempt_id)
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    if attempt.commit is None:
        raise BundleError("attempt has no implementation commit")
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise BundleError("bundle directory is missing or unsafe")
    _require_immutable(root, "bundle directory")
    allowed = {"spec.json", "diff.patch", "test-evidence", "instructions.md", "source"}
    run_plan_payload = _stored_json(store, attempt_id, "run_plan")
    if run_plan_payload is not None:
        allowed.update({"run-plan.json", "evaluation-spec-set.json", "evaluation-specs"})
    entries = {path.name for path in root.iterdir()}
    if entries != allowed:
        raise BundleError("bundle contains an unexpected file or directory")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BundleError("bundle contains a symlink")
    if (
        _read_bounded(root / "spec.json", MAX_BUNDLE_FILE_BYTES, "bundle spec")
        != hypothesis.spec_json.encode()
    ):
        raise BundleError("bundle spec does not match the frozen hypothesis")
    commit_marker = (
        _read_bounded(root / "source" / "COMMIT", 256, "bundle source commit").decode().strip()
    )
    if commit_marker != attempt.commit:
        raise BundleError("bundle source commit does not match the attempt")
    tracked = _tracked_source(Path(attempt.worktree_path), attempt.commit, hypothesis.base_commit)
    expected = {"COMMIT": (attempt.commit + "\n").encode()}
    if tracked.excluded:
        expected["EXCLUDED"] = _excluded_bytes(tracked.excluded)
    for relative, content in tracked.files:
        expected[relative] = content
    actual: dict[str, bytes] = {}
    for path in (root / "source").rglob("*"):
        if path.is_symlink():
            raise BundleError("bundle source contains a symlink or non-file")
        relative = path.relative_to(root / "source").as_posix()
        _safe_source_path(relative)
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleError("bundle source contains a symlink or non-file")
        _require_immutable(path, f"bundle source {relative}")
        actual[relative] = _read_bounded(path, MAX_BUNDLE_FILE_BYTES, f"bundle source {relative}")
    if actual != expected:
        raise BundleError("bundle source differs from the committed implementation")
    expected_diff = _git_diff(Path(attempt.worktree_path), hypothesis.base_commit, attempt.commit)
    if _read_bounded(root / "diff.patch", MAX_BUNDLE_BYTES, "bundle diff") != expected_diff:
        raise BundleError("bundle diff differs from the committed implementation")
    _require_immutable(root / "test-evidence", "bundle test evidence")
    _read_bounded(root / "test-evidence", MAX_TEST_EVIDENCE_BYTES, "bundle test evidence")
    if run_plan_payload is not None:
        run_plan = RunPlan.from_json((root / "run-plan.json").read_text(encoding="utf-8"))
        stored_plan = RunPlan.from_json(
            json.dumps(run_plan_payload, sort_keys=True, separators=(",", ":"))
        )
        if run_plan.to_json() != stored_plan.to_json():
            raise BundleError("bundle run plan differs from immutable evidence")
        spec_set = store.evaluation_spec_set(hypothesis.hypothesis_id)
        if (root / "evaluation-spec-set.json").read_text(encoding="utf-8") != spec_set.to_json():
            raise BundleError("bundle evaluation spec set differs from immutable evidence")
        expected_specs = {entry.spec_id: Path(entry.path).read_bytes() for entry in spec_set.specs}
        actual_specs = {
            path.stem: _read_bounded(path, MAX_BUNDLE_FILE_BYTES, f"evaluation spec {path.name}")
            for path in (root / "evaluation-specs").glob("*.json")
        }
        if actual_specs != expected_specs:
            raise BundleError("bundle evaluation specs differ from immutable evidence")
        for entry in spec_set.specs:
            if hashlib.sha256(actual_specs[entry.spec_id]).hexdigest() != entry.sha256:
                raise BundleError(f"bundle evaluation spec digest differs: {entry.spec_id}")
    digest = _bundle_digest(root)
    if expected_digest is not None and digest != expected_digest:
        raise BundleError("reserved review bundle was modified")
    return digest


def reserve_review(
    store: ResearchStore,
    attempt_id: str,
    bundle_dir: Path,
    owner_session_key: str,
    *,
    instructions: str | None = None,
) -> ReviewReservation:
    """Build/verify a bundle and reserve one unique reviewer label."""

    if not owner_session_key:
        raise ReviewEvidenceError("owner session key must be non-empty")
    if not bundle_dir.is_absolute():
        raise BundleError("bundle directory must be absolute")
    existing = _stored_json(store, attempt_id, "review_reservation")
    if existing is not None:
        reservation = _reservation_from_payload(existing)
        if (
            reservation.owner_session_key != owner_session_key
            or Path(reservation.bundle_dir) != bundle_dir.resolve()
        ):
            raise StoreConflict("review reservation differs from the stored reservation")
        _validate_bundle(
            store,
            attempt_id,
            Path(reservation.bundle_dir),
            expected_digest=reservation.bundle_sha256,
        )
        return reservation
    digest = build_review_bundle(store, attempt_id, bundle_dir, instructions=instructions)
    attempt = store.get_attempt(attempt_id)
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    reserved = now_utc()
    nonce = secrets.token_hex(12)
    reservation = ReviewReservation(
        attempt_id=attempt_id,
        commit=cast(str, attempt.commit),
        hypothesis_spec_sha256=hypothesis.spec_sha256,
        bundle_dir=str(bundle_dir.resolve()),
        bundle_sha256=digest,
        reserved_at=reserved,
        owner_session_key=owner_session_key,
        label=f"{attempt_id}-{nonce}",
        reservation_nonce=nonce,
    )
    store.insert_review_evidence(
        attempt_id, "review_reservation", reservation.to_json(), "review_reserved"
    )
    return reservation


def acknowledge_review(
    store: ResearchStore,
    attempt_id: str,
    child_session_key: str,
    run_id: str,
    mode: str,
    run_timeout_seconds: int | None,
) -> ReviewAck:
    reservation = _reservation(store, attempt_id)
    if not child_session_key or not run_id:
        raise ReviewEvidenceError("review ACK identity fields must be non-empty")
    if mode != "run":
        raise ReviewEvidenceError("review ACK mode must be run")
    if run_timeout_seconds is not None and (
        type(run_timeout_seconds) is not int or run_timeout_seconds <= 0
    ):
        raise ReviewEvidenceError("run timeout must be a positive integer or null")
    ack = ReviewAck(child_session_key, run_id, mode, run_timeout_seconds, now_utc())
    existing = _stored_json(store, attempt_id, "review_ack")
    if existing is not None:
        requested = json.loads(ack.to_json())
        comparable = {key: value for key, value in requested.items() if key != "acked_at"}
        stored_comparable = {key: value for key, value in existing.items() if key != "acked_at"}
        if stored_comparable != comparable:
            raise StoreConflict("review ACK differs from the stored ACK")
        return _ack_from_payload(existing)
    _ = reservation
    store.insert_review_evidence(attempt_id, "review_ack", ack.to_json(), "review_acknowledged")
    return ack


def reconcile_review(
    store: ResearchStore,
    attempt_id: str,
    core_database: Path,
) -> ReviewAck | None:
    """Recover one lost spawn ACK; ambiguous correlation pauses the campaign."""

    reservation = _reservation(store, attempt_id)
    existing = _stored_json(store, attempt_id, "review_ack")
    if existing is not None:
        return _ack_from_payload(existing)
    try:
        task = read_exact_task_run(
            core_database,
            reservation.owner_session_key,
            reservation.label,
            _epoch_ms(reservation.reserved_at),
            expected_runtime=REVIEW_TASK_RUNTIME,
            expected_scope_kind=REVIEW_TASK_SCOPE,
            expected_agent_id=REVIEW_AGENT,
        )
    except HostRecordError as exc:
        store.pause(f"review_unresolved:{attempt_id}")
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "correlation"})
        raise ReviewUnresolved("review task correlation is unresolved") from exc
    return acknowledge_review(store, attempt_id, task.child_session_key, task.run_id, "run", None)


def _epoch_ms(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewEvidenceError("review reservation timestamp is not canonical UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ReviewEvidenceError("review reservation timestamp must be UTC")
    return int(parsed.timestamp() * 1000)


def _stored_json(store: ResearchStore, attempt_id: str, kind: str) -> dict[str, object] | None:
    try:
        text = store.evidence(attempt_id, kind)
    except ValueError:
        return None
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise StoreConflict(f"{kind} evidence must be an object")
    return cast(dict[str, object], raw)


def _reservation_from_payload(payload: Mapping[str, object]) -> ReviewReservation:
    fields = (
        "attempt_id",
        "commit",
        "hypothesis_spec_sha256",
        "bundle_dir",
        "bundle_sha256",
        "reserved_at",
        "owner_session_key",
        "label",
        "reservation_nonce",
    )
    if set(payload) != set(fields) or any(
        not isinstance(payload[field], str) or not payload[field] for field in fields
    ):
        raise StoreConflict("review reservation payload is malformed")
    return ReviewReservation(*(str(payload[field]) for field in fields))


def _reservation(store: ResearchStore, attempt_id: str) -> ReviewReservation:
    payload = _stored_json(store, attempt_id, "review_reservation")
    if payload is None:
        raise ReviewEvidenceError("review reservation is missing")
    return _reservation_from_payload(payload)


def _ack_from_payload(payload: Mapping[str, object]) -> ReviewAck:
    required = {"child_session_key", "run_id", "mode", "run_timeout_seconds", "acked_at"}
    if set(payload) != required:
        raise StoreConflict("review ACK payload is malformed")
    child = payload["child_session_key"]
    run = payload["run_id"]
    mode = payload["mode"]
    timeout = payload["run_timeout_seconds"]
    at = payload["acked_at"]
    if (
        not isinstance(child, str)
        or not child
        or not isinstance(run, str)
        or not run
        or not isinstance(mode, str)
        or not mode
        or not isinstance(at, str)
        or not at
    ):
        raise StoreConflict("review ACK identity is malformed")
    if mode != "run":
        raise StoreConflict("review ACK mode is not run")
    if timeout is not None and (type(timeout) is not int or timeout <= 0):
        raise StoreConflict("review ACK timeout is malformed")
    return ReviewAck(child, run, mode, timeout, at)


def _assistant_events(content: bytes) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for index, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw: object = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReviewEvidenceError(f"transcript line {index} is not JSON") from exc
        if not isinstance(raw, dict):
            raise ReviewEvidenceError(f"transcript line {index} is not an object")
        if raw.get("type") == "assistant":
            events.append(cast(dict[str, object], raw))
    if not events:
        raise ReviewEvidenceError("transcript has no assistant events")
    return events


def _event_ms(event: Mapping[str, object]) -> int:
    value = event.get("timestamp", event.get("ts"))
    if isinstance(value, bool):
        raise ReviewEvidenceError("assistant event timestamp is invalid")
    if isinstance(value, int):
        return value if value > 10**12 else value * 1000
    if isinstance(value, float) and value >= 0:
        return int(value if value > 10**12 else value * 1000)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReviewEvidenceError("assistant event timestamp is invalid") from exc
        if parsed.tzinfo is None:
            raise ReviewEvidenceError("assistant event timestamp must be timezone-aware")
        return int(parsed.timestamp() * 1000)
    raise ReviewEvidenceError("assistant event timestamp is missing")


def _final_verdict(
    event: Mapping[str, object], reservation: ReviewReservation
) -> tuple[str, tuple[str, ...], str]:
    message = event.get("message")
    if not isinstance(message, dict) or message.get("stop_reason") != "end_turn":
        raise ReviewEvidenceError("final assistant event is not end_turn")
    content = message.get("content")
    if not isinstance(content, list):
        raise ReviewEvidenceError("final assistant message content is not a list")
    pieces: list[str] = []
    for block in content:
        if (
            not isinstance(block, dict)
            or block.get("type") != "text"
            or not isinstance(block.get("text"), str)
        ):
            raise ReviewEvidenceError(
                "final assistant event contains a tool call or non-text block"
            )
        pieces.append(str(block["text"]))
    text = "".join(pieces).strip()
    if not text.startswith("{") or not text.endswith("}"):
        raise ReviewEvidenceError("final review verdict must be bare JSON")
    try:
        raw: object = json.loads(text, object_pairs_hook=_strict_json_object)
    except json.JSONDecodeError as exc:
        raise ReviewEvidenceError("final review verdict is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ReviewEvidenceError("final review verdict must be an object")
    required = {"verdict", "attempt_id", "commit", "spec_sha256", "findings"}
    if set(raw) != required:
        raise ReviewEvidenceError("final review verdict has unexpected keys")
    verdict = raw.get("verdict")
    attempt_id = raw.get("attempt_id")
    commit = raw.get("commit")
    spec = raw.get("spec_sha256")
    findings = raw.get("findings")
    if (
        verdict not in {"PASS", "FAIL"}
        or attempt_id != reservation.attempt_id
        or commit != reservation.commit
        or spec != reservation.hypothesis_spec_sha256
    ):
        raise ReviewEvidenceError("verdict_binding")
    if not isinstance(findings, list) or any(
        not isinstance(item, str) or not item for item in findings
    ):
        raise ReviewEvidenceError("review findings are malformed")
    return str(verdict), tuple(findings), text


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReviewEvidenceError("final review verdict contains duplicate keys")
        result[key] = value
    return result


def verify_review(
    store: ResearchStore,
    attempt_id: str,
    core_database: Path,
    acpx_sessions_dir: Path,
    claude_projects_root: Path,
    *,
    expected_backend: str = REVIEW_BACKEND,
    expected_mode: str = REVIEW_ACP_MODE,
) -> ReviewVerification:
    """Verify exact host records and return a store-ready evidence record."""

    reservation = _reservation(store, attempt_id)
    ack_payload = _stored_json(store, attempt_id, "review_ack")
    if ack_payload is None:
        raise ReviewPending("review ACK is pending")
    ack = _ack_from_payload(ack_payload)
    try:
        task = read_exact_task_run(
            core_database,
            reservation.owner_session_key,
            reservation.label,
            _epoch_ms(reservation.reserved_at),
            expected_runtime=REVIEW_TASK_RUNTIME,
            expected_scope_kind=REVIEW_TASK_SCOPE,
            expected_agent_id=REVIEW_AGENT,
            ack_child_session_key=ack.child_session_key,
            ack_run_id=ack.run_id,
        )
    except HostRecordError as exc:
        store.pause(f"review_unresolved:{attempt_id}")
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "correlation"})
        raise ReviewUnresolved("review task correlation is unresolved") from exc
    if task.is_pending:
        raise ReviewPending(f"review task is pending: {task.status}")
    if task.status != "succeeded":
        return _failed_verification(
            reservation, task.child_session_key, task.status, task.status, task
        )
    if task.ended_at_ms is None:
        return _failed_verification(
            reservation,
            task.child_session_key,
            "task_end_missing",
            "task has no terminal timestamp",
            task,
        )
    identity: AcpIdentityHostRecord | None = None
    try:
        identity = read_exact_acpx_identity(
            acpx_sessions_dir,
            task.child_session_key,
            reservation.bundle_dir,
            expected_backend=expected_backend,
            expected_agent=REVIEW_AGENT,
            expected_mode=expected_mode,
            expected_run_id=task.run_id,
            reservation_at_ms=_epoch_ms(reservation.reserved_at),
            task_started_at_ms=task.started_at_ms,
            task_ended_at_ms=task.ended_at_ms,
        )
        transcript = read_exact_claude_transcript(
            claude_projects_root,
            identity.effective_cwd,
            identity.claude_session_id,
        )
        events = _assistant_events(transcript.content)
        models: set[str] = set()
        efforts: set[str] = set()
        lower = _epoch_ms(reservation.reserved_at) - CLOCK_TOLERANCE_MS
        upper = task.ended_at_ms + 60_000
        for event in events:
            if event.get("sessionId") != identity.claude_session_id:
                raise ReviewEvidenceError("assistant sessionId does not match ACP identity")
            if event.get("cwd") != reservation.bundle_dir:
                raise ReviewEvidenceError("assistant cwd does not match the review bundle")
            event_time = _event_ms(event)
            if event_time < lower or event_time > upper:
                raise ReviewEvidenceError("assistant event timestamp is outside the task window")
            effort = event.get("effort")
            if effort != REVIEW_EFFORT:
                raise ReviewEvidenceError(f"assistant effort is not {REVIEW_EFFORT} on every event")
            message = event.get("message")
            if not isinstance(message, dict) or message.get("model") != REVIEW_MODEL:
                raise ReviewEvidenceError("assistant model is not claude-opus-5 on every event")
            models.add(REVIEW_MODEL)
            efforts.add(REVIEW_EFFORT)
        verdict, findings, verdict_json = _final_verdict(events[-1], reservation)
        try:
            _validate_bundle(
                store,
                attempt_id,
                Path(reservation.bundle_dir),
                expected_digest=reservation.bundle_sha256,
            )
        except BundleError as exc:
            raise ReviewEvidenceError("bundle_mutated") from exc
        host = {
            "task_id": task.task_id,
            "task_owner_key": task.owner_key,
            "child_session_key": task.child_session_key,
            "run_id": task.run_id,
            "task_status": task.status,
            "task_started_at": task.started_at_ms,
            "task_ended_at": task.ended_at_ms,
            "acpx_record_id": identity.acpx_record_id,
            "acpx_record_path": str(identity.acpx_record_path),
            "acpx_record_sha256": identity.acpx_record_sha256,
            "acpx_candidate_count": identity.acpx_candidate_count,
            "acpx_session_id": identity.acp_session_id,
            "acpx_created_at": identity.created_at_ms,
            "acpx_last_used_at": identity.last_used_at_ms,
            "acpx_closed_at": identity.closed_at_ms,
            "acpx_model": identity.model,
            "acpx_effort": identity.effort,
            "acp_session_uuid": identity.canonical_session_uuid,
            "transcript_path": str(transcript.path),
            "transcript_sha256": transcript.sha256,
            "assistant_events": len(events),
            "models_seen": sorted(models),
            "efforts_seen": sorted(efforts),
            "verdict_json": verdict_json,
            "bound_commit": reservation.commit,
            "bound_spec_sha256": reservation.hypothesis_spec_sha256,
            "collected_at": now_utc(),
            "verdict": verdict,
        }
        stored_plan = _stored_json(store, reservation.attempt_id, "run_plan")
        if stored_plan is not None:
            host["bound_run_plan_sha256"] = hashlib.sha256(
                json.dumps(stored_plan, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        review = ReviewEvidence(
            attempt_id,
            reservation.commit,
            reservation.hypothesis_spec_sha256,
            verdict,
            findings,
            REVIEW_MODEL,
            REVIEW_MODEL,
            identity.canonical_session_uuid,
            str(host["collected_at"]),
        )
        return ReviewVerification(review, to_json(host))
    except HostRecordError as exc:
        store.pause(f"review_unresolved:{attempt_id}")
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "host_record"})
        raise ReviewUnresolved("review host correlation is unresolved") from exc
    except ReviewEvidenceError as exc:
        reason = str(exc) or "review_evidence_invalid"
        return _failed_verification(
            reservation,
            identity.canonical_session_uuid if identity is not None else ack.child_session_key,
            reason,
            reason,
            task,
        )


def _failed_verification(
    reservation: ReviewReservation,
    session_id: str,
    reason: str,
    detail: str,
    task: TaskRunHostRecord | None = None,
) -> ReviewVerification:
    collected = now_utc()
    host: dict[str, object] = {
        "task_id": task.task_id if task else "",
        "task_status": task.status if task else "unresolved",
        "task_started_at": task.started_at_ms if task else None,
        "task_ended_at": task.ended_at_ms if task else None,
        "acp_session_uuid": session_id,
        "transcript_path": "",
        "transcript_sha256": "",
        "assistant_events": 0,
        "models_seen": [],
        "efforts_seen": [],
        "verdict_json": "",
        "bound_commit": reservation.commit,
        "bound_spec_sha256": reservation.hypothesis_spec_sha256,
        "collected_at": collected,
        "verdict": "FAIL",
        "reason": reason,
        "detail": detail[:256],
    }
    review = ReviewEvidence(
        reservation.attempt_id,
        reservation.commit,
        reservation.hypothesis_spec_sha256,
        "FAIL",
        (reason,),
        REVIEW_MODEL,
        REVIEW_MODEL,
        session_id,
        collected,
    )
    return ReviewVerification(review, to_json(host))


async def request_cancel(
    request_once: Callable[..., Awaitable[Mapping[str, object]]], task_id: str, reason: str
) -> Mapping[str, object]:
    """Narrow adapter around the existing authenticated OpenClaw RPC client."""

    return await request_once(
        "tasks.cancel",
        {"taskId": task_id, "reason": reason},
        timeout_seconds=30,
    )


def cancel_review(
    store: ResearchStore,
    attempt_id: str,
    reason: str,
    core_database: Path,
    request_once: CancelTransport,
) -> CancelOutcome:
    """Pause, correlate, request cancellation, and report unknown responses pending."""

    store.pause(f"review_cancel:{attempt_id}")
    existing_cancel = _stored_json(store, attempt_id, "review_cancel")
    if existing_cancel is not None:
        task_value = existing_cancel.get("task_id")
        result_value = existing_cancel.get("rpc_result")
        task_id = task_value if isinstance(task_value, str) else None
        rpc_result = result_value if isinstance(result_value, str) else "UNKNOWN_RESPONSE"
        pending = rpc_result in {
            "UNKNOWN_RESPONSE",
            "pending",
            "accepted",
            "MALFORMED_RESPONSE",
            "MISMATCHED_RESPONSE",
            "RPC_REJECTED",
        } or rpc_result.startswith("RPC_ERROR:")
        if pending and task_id is not None:
            try:
                reservation = _reservation(store, attempt_id)
                ack = _ack_from_payload(_stored_json(store, attempt_id, "review_ack") or {})
                return _cancel_after_reread(
                    core_database, reservation, ack, attempt_id, task_id, rpc_result
                )
            except (ReviewEvidenceError, StoreConflict):
                pass
        return CancelOutcome(
            attempt_id,
            task_id,
            "pending" if pending else rpc_result,
            pending,
            rpc_result,
        )
    requested_task = store.review_cancel_requested(attempt_id)
    if requested_task is not None:
        # A process may have stopped after recording the request event but
        # before persisting the RPC result.  Never send the exact cancel twice;
        # the host task remains the authority for terminal confirmation.
        if requested_task[1] != reason:
            raise StoreConflict("review cancellation reason differs from stored request")
        return CancelOutcome(attempt_id, requested_task[0], "pending", True, "UNKNOWN_RESPONSE")
    # Reconcile all reservations that lack ACKs before touching the target.
    for candidate in store.review_reservation_attempts():
        if _stored_json(store, candidate, "review_ack") is None:
            try:
                reconcile_review(store, candidate, core_database)
            except ReviewUnresolved:
                if candidate == attempt_id:
                    return CancelOutcome(attempt_id, None, "unresolved", True, "not_requested")
    reservation = _reservation(store, attempt_id)
    ack_payload = _stored_json(store, attempt_id, "review_ack")
    if ack_payload is None:
        return CancelOutcome(attempt_id, None, "unresolved", True, "not_requested")
    ack = _ack_from_payload(ack_payload)
    try:
        task = read_exact_task_run(
            core_database,
            reservation.owner_session_key,
            reservation.label,
            _epoch_ms(reservation.reserved_at),
            expected_runtime=REVIEW_TASK_RUNTIME,
            expected_scope_kind=REVIEW_TASK_SCOPE,
            expected_agent_id=REVIEW_AGENT,
            ack_child_session_key=ack.child_session_key,
            ack_run_id=ack.run_id,
        )
    except HostRecordError:
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "cancel_correlation"})
        return CancelOutcome(attempt_id, None, "unresolved", True, "not_requested")
    if task.is_terminal:
        result = "terminal:" + task.status
        store.record_review_cancel(attempt_id, task.task_id, reason, result)
        return CancelOutcome(attempt_id, task.task_id, task.status, False, result)
    if not store.record_review_cancel_request(attempt_id, task.task_id, reason):
        return CancelOutcome(attempt_id, task.task_id, "pending", True, "UNKNOWN_RESPONSE")

    async def invoke_cancel() -> Mapping[str, object]:
        return await request_once(task.task_id, reason)

    try:
        response = asyncio.run(invoke_cancel())
        result = _safe_rpc_result(response, task.task_id)
    except (OpenClawTransportError, TimeoutError, OSError):
        result = "UNKNOWN_RESPONSE"
        store.record_review_cancel(attempt_id, task.task_id, reason, result)
        return _cancel_after_reread(
            core_database, reservation, ack, attempt_id, task.task_id, result
        )
    except OpenClawError:
        result = "RPC_REJECTED"
        store.record_review_cancel(attempt_id, task.task_id, reason, result)
        return _cancel_after_reread(
            core_database, reservation, ack, attempt_id, task.task_id, result
        )
    except Exception as exc:
        result = f"RPC_ERROR:{type(exc).__name__}"
        store.record_review_cancel(attempt_id, task.task_id, reason, result)
        return _cancel_after_reread(
            core_database, reservation, ack, attempt_id, task.task_id, result
        )
    store.record_review_cancel(attempt_id, task.task_id, reason, result)
    return _cancel_after_reread(core_database, reservation, ack, attempt_id, task.task_id, result)


def _cancel_after_reread(
    core_database: Path,
    reservation: ReviewReservation,
    ack: ReviewAck,
    attempt_id: str,
    task_id: str,
    result: str,
) -> CancelOutcome:
    try:
        after = read_exact_task_run(
            core_database,
            reservation.owner_session_key,
            reservation.label,
            _epoch_ms(reservation.reserved_at),
            expected_runtime=REVIEW_TASK_RUNTIME,
            expected_scope_kind=REVIEW_TASK_SCOPE,
            expected_agent_id=REVIEW_AGENT,
            ack_child_session_key=ack.child_session_key,
            ack_run_id=ack.run_id,
        )
    except HostRecordError:
        return CancelOutcome(attempt_id, task_id, "pending", True, result)
    if after.task_id != task_id:
        return CancelOutcome(attempt_id, task_id, "pending", True, result)
    if not after.is_terminal:
        return CancelOutcome(attempt_id, task_id, "pending", True, result)
    return CancelOutcome(attempt_id, task_id, after.status, False, result)


def _safe_rpc_result(response: Mapping[str, object], task_id: str) -> str:
    if not isinstance(response, Mapping):
        return "MALFORMED_RESPONSE"
    response_task = response.get("taskId", response.get("task_id"))
    if response_task is not None and response_task != task_id:
        return "MISMATCHED_RESPONSE"
    status = response.get("status", response.get("state"))
    if not isinstance(status, str) or not status:
        return "MALFORMED_RESPONSE"
    normalized = status[:64]
    if normalized.lower() in {"rejected", "denied", "error", "failed"}:
        return "RPC_REJECTED"
    return normalized


def collect_review(
    store: ResearchStore,
    attempt_id: str,
    core_database: Path,
    acpx_sessions_dir: Path,
    claude_projects_root: Path,
    *,
    expected_backend: str = REVIEW_BACKEND,
    expected_mode: str = REVIEW_ACP_MODE,
) -> Attempt:
    """Verify and atomically persist host evidence plus review transition."""

    existing = _stored_json(store, attempt_id, "review_host_evidence")
    if existing is not None:
        current = store.get_attempt(attempt_id)
        review_payload = _stored_json(store, attempt_id, "review")
        if review_payload is not None:
            parsed = ReviewRecord.from_json(to_json(review_payload))
            return store.collect_review_evidence(
                attempt_id,
                ReviewEvidence(
                    parsed.attempt_id,
                    parsed.commit,
                    parsed.spec_sha256,
                    parsed.verdict,
                    parsed.findings,
                    parsed.reported_reviewer_model,
                    parsed.reported_reviewer_actual_model,
                    parsed.acp_session_id,
                    parsed.submitted_at,
                ),
                store.evidence(attempt_id, "review_host_evidence"),
            )
        if current.state in {AttemptState.REVIEW_PASSED, AttemptState.REVIEW_FAILED}:
            raise StoreConflict("review host evidence has no matching review payload")
    verification = verify_review(
        store,
        attempt_id,
        core_database,
        acpx_sessions_dir,
        claude_projects_root,
        expected_backend=expected_backend,
        expected_mode=expected_mode,
    )
    return store.collect_review_evidence(attempt_id, verification.review, verification.host_payload)
