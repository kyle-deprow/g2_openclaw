"""Bounded, host-observed native OpenAI Sol review evidence.

This module is the review boundary for the research driver.  A reviewer does
not submit a JSON file that changes campaign state: the driver reserves and
freezes a source bundle, correlates one exact native Codex child across the
official OpenClaw and Codex stores, and only then asks the existing store to
apply a verified verdict.

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
import re
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
    HostRecordError,
    HostRecordNotRecorded,
    HostRecordPending,
    NativeChildHostRecord,
    NativeOwnerHostRecord,
    read_exact_native_child,
    read_exact_native_owner,
    read_native_owner_writer,
)
from .store import ResearchStore, StoreConflict, canonical_wake_key, now_utc

MAX_BUNDLE_FILE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_TEST_EVIDENCE_BYTES = 8 * 1024 * 1024
REVIEW_MODEL = "gpt-5.6-sol"
REVIEW_EFFORT = "xhigh"
REVIEW_TASK_RUNTIME = "native"
REVIEW_TASK_SCOPE = "session"
REVIEW_AGENT = "reviewer"
REVIEW_BACKEND = "native"
REVIEW_NATIVE_MODE = "run"
# chat.abort addresses only an active owner run.  The owner yields right after
# spawning, so once a child is running its run is inactive and OpenClaw has no
# supported way to stop the child; it ends on its own terminal evidence.
CANCEL_ABORTED = "owner_run_aborted"
CANCEL_OWNER_INACTIVE = "owner_run_inactive_no_supported_child_cancel"
_NATIVE_TASK_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BUNDLE_ENTRIES = {
    "spec.json",
    "diff.patch",
    "test-evidence",
    "instructions.md",
    "source",
    "run-plan.json",
    "containment-provenance.json",
    "evaluation-spec-set.json",
    "evaluation-specs",
}
_LEGACY_BUNDLE_ENTRIES = _BUNDLE_ENTRIES - {"containment-provenance.json"}


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
    owner_run_id: str | None = None
    owner_thread_id: str | None = None
    task_name: str | None = None
    prompt_sha256: str | None = None
    spawn_arguments_json: str | None = None

    @property
    def spawn_arguments(self) -> dict[str, str]:
        """Return the exact native spawn arguments, never a caller retyping."""

        if self.spawn_arguments_json is None:
            return {}
        try:
            raw: object = json.loads(self.spawn_arguments_json)
        except json.JSONDecodeError as exc:
            raise StoreConflict("native spawn arguments are malformed") from exc
        if not isinstance(raw, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in raw.items()
        ):
            raise StoreConflict("native spawn arguments are malformed")
        return dict(raw)

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
                **(
                    {
                        "owner_run_id": self.owner_run_id,
                        "owner_thread_id": self.owner_thread_id,
                        "task_name": self.task_name,
                        "prompt_sha256": self.prompt_sha256,
                        "spawn_arguments": self.spawn_arguments,
                    }
                    if self.spawn_arguments_json is not None
                    else {}
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class ReviewAck:
    """Review ACK.  ``child_session_key``/``run_id`` exist only on historical ACP ACKs.

    The installed host writes no OpenClaw session or run for a native child,
    so a native ACK identifies the child by its Codex thread id alone.
    """

    child_session_key: str | None
    run_id: str | None
    mode: str
    run_timeout_seconds: int | None
    acked_at: str
    owner_session_key: str | None = None
    owner_run_id: str | None = None
    owner_thread_id: str | None = None
    child_thread_id: str | None = None

    def to_json(self) -> str:
        payload: dict[str, object] = {
            "mode": self.mode,
            "run_timeout_seconds": self.run_timeout_seconds,
            "acked_at": self.acked_at,
        }
        for key, value in (
            ("child_session_key", self.child_session_key),
            ("run_id", self.run_id),
            ("owner_session_key", self.owner_session_key),
            ("owner_run_id", self.owner_run_id),
            ("owner_thread_id", self.owner_thread_id),
            ("child_thread_id", self.child_thread_id),
        ):
            if value is not None:
                payload[key] = value
        return to_json(payload)


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


CancelTransport = Callable[..., Awaitable[Mapping[str, object]]]


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
        limit = MAX_BUNDLE_BYTES if relative == "diff.patch" else MAX_BUNDLE_FILE_BYTES
        content = _read_bounded(path, limit, f"bundle file {relative}")
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


@dataclass(frozen=True, slots=True)
class BundleCandidate:
    """Implementation evidence that is not stored yet (the preflight dry-build input).

    ``implementation_json`` is the exact ``ImplementationRecord`` JSON,
    ``containment_provenance`` the canonical provenance text that
    ``validate_provenance_evidence`` returns.
    """

    commit: str
    implementation_json: str
    run_plan: RunPlan
    containment_provenance: str


@dataclass(frozen=True, slots=True)
class _BundleParts:
    """Everything the bundle writer needs, gathered and bound-checked once."""

    attempt_id: str
    commit: str
    spec_bytes: bytes
    diff: bytes
    test_bytes: bytes
    instruction_bytes: bytes
    run_plan: RunPlan
    containment_provenance: dict[str, object]
    spec_set: EvaluationSpecSet
    tracked: _TrackedSource


@dataclass(frozen=True, slots=True)
class BundleReport:
    """Size accounting of a dry-built review bundle."""

    commit: str
    digest: str | None
    error: str | None
    total_bytes: int
    source_bytes: int
    diff_bytes: int
    deployed_estimate_bytes: int
    max_bundle_bytes: int
    files: tuple[tuple[str, int], ...]

    @property
    def largest_files(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self.files, key=lambda item: (-item[1], item[0]))[:5])

    @property
    def within_limits(self) -> bool:
        return (
            self.error is None
            and self.total_bytes <= self.max_bundle_bytes
            and self.deployed_estimate_bytes <= self.max_bundle_bytes
        )


# Fixed allowance on top of tracked source + diff for the spec, plan, evidence and
# instruction files when estimating the deployed bundle size.
DEPLOYED_OVERHEAD_BYTES = 2 * 1024 * 1024


def _prepare_bundle(
    store: ResearchStore,
    attempt_id: str,
    bundle_dir: Path,
    *,
    instructions: str | None,
    candidate: BundleCandidate | None,
) -> _BundleParts:
    """Gather and bound-check bundle inputs from the store (or from a candidate)."""

    attempt = store.get_attempt(attempt_id)
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    if candidate is None:
        if attempt.state != AttemptState.IMPLEMENTED or attempt.commit is None:
            raise BundleError("review bundle requires an IMPLEMENTED attempt")
        commit = attempt.commit
    else:
        commit = candidate.commit
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
    implementation = json.loads(
        store.evidence(attempt_id, "implementation")
        if candidate is None
        else candidate.implementation_json
    )
    run_plan_payload = (
        _stored_json(store, attempt_id, "run_plan")
        if candidate is None
        else cast(dict[str, object], json.loads(candidate.run_plan.to_json()))
    )
    if run_plan_payload is None:
        raise BundleError("review bundle requires immutable run-plan evidence")
    containment_provenance = (
        _stored_containment_provenance(store, attempt_id)
        if candidate is None
        else _checked_containment_provenance(json.loads(candidate.containment_provenance))
    )
    if containment_provenance is None:
        raise BundleError("review bundle requires containment provenance evidence")
    try:
        run_plan = RunPlan.from_json(
            json.dumps(run_plan_payload, sort_keys=True, separators=(",", ":"))
        )
        spec_set = store.evaluation_spec_set(hypothesis.hypothesis_id)
    except (StoreConflict, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise BundleError("review bundle requires valid immutable plan evidence") from exc
    if (
        run_plan.evaluation_spec_set_sha256
        != hashlib.sha256(spec_set.to_json().encode()).hexdigest()
    ):
        raise BundleError("run plan evaluation spec-set digest differs from evidence")
    test_path = implementation.get("test_evidence_path")
    if not isinstance(test_path, str) or not test_path:
        raise BundleError("implementation has no bounded test evidence path")
    test_bytes = _read_bounded(Path(test_path), MAX_TEST_EVIDENCE_BYTES, "test evidence")
    tracked = _tracked_source(source, commit, hypothesis.base_commit)
    diff = _git_diff(source, hypothesis.base_commit, commit)
    if len(diff) > MAX_BUNDLE_BYTES:
        raise BundleError("implementation diff exceeds the bundle limit")
    verdict_example = json.dumps(
        {
            "verdict": "FAIL",
            "attempt_id": attempt.attempt_id,
            "commit": commit,
            "spec_sha256": hypothesis.spec_sha256,
            "findings": [
                "severity=high; location=source/example.py:1; explanation=Example finding."
            ],
        },
        separators=(",", ":"),
    )
    text = instructions or (
        "Review only the committed source under source/. Your terminal response must be exactly "
        "one bare JSON object with exactly these keys: verdict, attempt_id, commit, spec_sha256, "
        "findings. Do not include markdown or surrounding prose. Verdict must be PASS or FAIL; "
        f"use these exact bindings: attempt_id={attempt.attempt_id}, commit={commit}, "
        f"spec_sha256={hypothesis.spec_sha256}. Findings must be an array of nonempty strings, "
        "never objects, nested arrays, or null; encode severity, location, and explanation within "
        "each string, or use [] when there are no findings.\n"
        "containment-provenance.json is the host-verified in-sandbox import provenance "
        f"for the tested commit. The immutable bundle is at bundle_dir={bundle_dir.resolve()}; "
        "the native spawn has no cwd, so use that absolute path exactly.\n"
        f"Valid JSON example:\n{verdict_example}"
    )
    if tracked.excluded and instructions is None:
        text += (
            " The source/EXCLUDED file records unchanged frozen-baseline paths "
            "omitted from source/."
        )
    instruction_bytes = text.encode("utf-8")
    if len(instruction_bytes) > MAX_BUNDLE_FILE_BYTES:
        raise BundleError("review instructions exceed the bundle file limit")
    return _BundleParts(
        attempt_id=attempt.attempt_id,
        commit=commit,
        spec_bytes=spec_bytes,
        diff=diff,
        test_bytes=test_bytes,
        instruction_bytes=instruction_bytes,
        run_plan=run_plan,
        containment_provenance=containment_provenance,
        spec_set=spec_set,
        tracked=tracked,
    )


def _write_bundle_tree(root: Path, parts: _BundleParts) -> None:
    """Write the bundle files (not yet frozen) into the existing directory ``root``."""

    (root / "source").mkdir()
    (root / "spec.json").write_bytes(parts.spec_bytes)
    (root / "diff.patch").write_bytes(parts.diff)
    (root / "test-evidence").write_bytes(parts.test_bytes)
    (root / "instructions.md").write_bytes(parts.instruction_bytes)
    (root / "source" / "COMMIT").write_text(parts.commit + "\n", encoding="utf-8")
    if parts.tracked.excluded:
        (root / "source" / "EXCLUDED").write_bytes(_excluded_bytes(parts.tracked.excluded))
    (root / "run-plan.json").write_text(parts.run_plan.to_json(), encoding="utf-8")
    (root / "containment-provenance.json").write_text(
        to_json(parts.containment_provenance), encoding="utf-8"
    )
    (root / "evaluation-spec-set.json").write_text(parts.spec_set.to_json(), encoding="utf-8")
    for entry in parts.spec_set.specs:
        destination = root / "evaluation-specs" / f"{entry.spec_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(Path(entry.path).read_bytes())
    for relative, content in parts.tracked.files:
        destination = root / "source" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


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

    parts = _prepare_bundle(
        store, attempt_id, bundle_dir, instructions=instructions, candidate=None
    )
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
        _write_bundle_tree(temporary, parts)
        _bundle_digest(temporary)
        _readonly_tree(temporary)
        os.replace(temporary, bundle_dir)
        temporary = Path()
    except Exception:
        if temporary != Path() and temporary.exists():
            shutil.rmtree(temporary)
        raise
    return _bundle_digest(bundle_dir)


def dry_build_review_bundle(
    store: ResearchStore,
    attempt_id: str,
    candidate: BundleCandidate,
    *,
    nominal_bundle_dir: Path | None = None,
) -> BundleReport:
    """Build the review bundle for a not-yet-stored candidate and account its size.

    The inputs are gathered and the files written by the same code ``review-bundle`` uses;
    only the source of the implementation evidence differs.  Nothing outside a private
    temporary directory is written, and that directory is always removed.  A size overrun
    is reported (``error`` / ``within_limits``) rather than raised, but every other
    ``BundleError`` propagates.  ``nominal_bundle_dir`` only fixes the absolute path quoted
    in the review instructions so the byte count can match a real bundle exactly.
    """

    scratch = Path(tempfile.mkdtemp(prefix="submission-preflight-")).resolve()
    try:
        root = scratch / "bundle"
        parts = _prepare_bundle(
            store,
            attempt_id,
            nominal_bundle_dir or root,
            instructions=None,
            candidate=candidate,
        )
        root.mkdir()
        _write_bundle_tree(root, parts)
        files = tuple(
            (path.relative_to(root).as_posix(), path.stat().st_size)
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
        source_bytes = sum(len(content) for _path, content in parts.tracked.files)
        digest: str | None = None
        error: str | None = None
        try:
            digest = _bundle_digest(root)
        except BundleError as exc:
            error = str(exc)
        return BundleReport(
            commit=parts.commit,
            digest=digest,
            error=error,
            total_bytes=sum(size for _name, size in files),
            source_bytes=source_bytes,
            diff_bytes=len(parts.diff),
            deployed_estimate_bytes=source_bytes + len(parts.diff) + DEPLOYED_OVERHEAD_BYTES,
            max_bundle_bytes=MAX_BUNDLE_BYTES,
            files=files,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


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
    run_plan_payload = _stored_json(store, attempt_id, "run_plan")
    if run_plan_payload is None:
        raise BundleError("review bundle requires immutable run-plan evidence")
    containment_provenance = _stored_containment_provenance(store, attempt_id)
    if containment_provenance is None:
        raise BundleError("review bundle requires containment provenance evidence")
    entries = {path.name for path in root.iterdir()}
    if entries != _BUNDLE_ENTRIES:
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
    try:
        run_plan = RunPlan.from_json((root / "run-plan.json").read_text(encoding="utf-8"))
        stored_plan = RunPlan.from_json(
            json.dumps(run_plan_payload, sort_keys=True, separators=(",", ":"))
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BundleError("bundle run plan is invalid") from exc
    _require_immutable(root / "run-plan.json", "bundle run plan")
    if run_plan.to_json() != stored_plan.to_json():
        raise BundleError("bundle run plan differs from immutable evidence")
    provenance_path = root / "containment-provenance.json"
    _require_immutable(provenance_path, "bundle containment provenance")
    try:
        stored_provenance = json.loads(
            _read_bounded(
                provenance_path,
                MAX_BUNDLE_FILE_BYTES,
                "bundle containment provenance",
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError("bundle containment provenance is invalid") from exc
    if stored_provenance != containment_provenance:
        raise BundleError("bundle containment provenance differs from immutable evidence")
    spec_set = store.evaluation_spec_set(hypothesis.hypothesis_id)
    _require_immutable(root / "evaluation-spec-set.json", "bundle evaluation spec set")
    if (root / "evaluation-spec-set.json").read_text(encoding="utf-8") != spec_set.to_json():
        raise BundleError("bundle evaluation spec set differs from immutable evidence")
    expected_specs = {entry.spec_id: Path(entry.path).read_bytes() for entry in spec_set.specs}
    spec_paths = sorted((root / "evaluation-specs").iterdir())
    expected_names = {f"{entry.spec_id}.json" for entry in spec_set.specs}
    if {path.name for path in spec_paths} != expected_names:
        raise BundleError("bundle evaluation spec set has missing or extra files")
    actual_specs: dict[str, bytes] = {}
    for path in spec_paths:
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise BundleError("bundle evaluation specs contain an unsafe entry")
        _require_immutable(path, f"bundle evaluation spec {path.name}")
        actual_specs[path.stem] = _read_bounded(
            path, MAX_BUNDLE_FILE_BYTES, f"evaluation spec {path.name}"
        )
    if actual_specs != expected_specs:
        raise BundleError("bundle evaluation specs differ from immutable evidence")
    for entry in spec_set.specs:
        if hashlib.sha256(actual_specs[entry.spec_id]).hexdigest() != entry.sha256:
            raise BundleError(f"bundle evaluation spec digest differs: {entry.spec_id}")
    digest = _bundle_digest(root)
    if expected_digest is not None and digest != expected_digest:
        raise BundleError("reserved review bundle was modified")
    return digest


def _verify_reserved_bundle(root: Path, expected_digest: str) -> str:
    """Verify the frozen review snapshot without reopening its source worktree."""

    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise BundleError("reserved review bundle digest is malformed")
    try:
        int(expected_digest, 16)
    except ValueError as exc:
        raise BundleError("reserved review bundle digest is malformed") from exc
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise BundleError("bundle directory is missing or unsafe")
    _require_immutable(root, "bundle directory")
    entries = {path.name for path in root.iterdir()}
    if entries not in (_BUNDLE_ENTRIES, _LEGACY_BUNDLE_ENTRIES):
        raise BundleError("bundle contains an unexpected file or directory")
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise BundleError("bundle contains a symlink or non-file entry")
        _require_immutable(path, f"bundle entry {path.relative_to(root).as_posix()}")
    digest = _bundle_digest(root)
    if digest != expected_digest:
        raise BundleError("reserved review bundle was modified")
    return digest


def reserve_review(
    store: ResearchStore,
    attempt_id: str,
    bundle_dir: Path,
    owner_session_key: str,
    *,
    instructions: str | None = None,
    wake_pending_key: str,
    openclaw_database: Path,
) -> ReviewReservation:
    """Build/verify a bundle and reserve one unique reviewer label."""

    if not owner_session_key:
        raise ReviewEvidenceError("owner session key must be non-empty")
    if not wake_pending_key:
        raise ReviewEvidenceError("native review reserve requires an exact completed wake key")
    wake = store.wake_row(wake_pending_key)
    attempt = store.get_attempt(attempt_id)
    _campaign_status, current_resume_seq = store.campaign()
    expected_wake_key = canonical_wake_key(
        attempt.hypothesis_id, attempt_id, AttemptState.IMPLEMENTED.value, current_resume_seq
    )
    if wake_pending_key != expected_wake_key:
        raise ReviewUnresolved("native review wake key is not the current canonical key")
    if wake is None:
        raise ReviewUnresolved("native review wake reservation is missing")
    if wake["attempt_id"] != attempt_id or wake["state"] != AttemptState.IMPLEMENTED.value:
        raise ReviewUnresolved(
            "native review wake reservation does not bind the IMPLEMENTED attempt"
        )
    wake_run_id = wake["run_id"]
    if not isinstance(wake_run_id, str) or not wake_run_id or wake_run_id == "PENDING":
        raise ReviewUnresolved("native review wake has no completed owner run")
    if wake["resume_seq"] != current_resume_seq:
        raise ReviewUnresolved("native review wake reservation is stale")
    if attempt.state is not AttemptState.IMPLEMENTED:
        raise ReviewUnresolved("native review attempt is no longer IMPLEMENTED")
    # The reserve command runs inside the owner run it binds, and OpenClaw
    # flushes a run's runtime events to SQLite only after that run.  The wake
    # row's run id is therefore the canonical owner run; its thread is bound
    # now only if the run's boundary events happen to be flushed already, and
    # is otherwise deferred to reconcile/verify/cancel.
    owner_run_id = wake_run_id
    owner: NativeOwnerHostRecord | None
    try:
        owner = read_exact_native_owner(
            openclaw_database,
            owner_session_key,
            _epoch_ms(str(wake["sent_at"])),
            expected_run_id=wake_run_id,
        )
    except HostRecordNotRecorded:
        owner = None
    except (HostRecordError, ReviewEvidenceError) as exc:
        raise ReviewUnresolved("native review owner run/thread is unresolved") from exc
    owner_thread_id = owner.thread_id if owner is not None else None
    if not bundle_dir.is_absolute():
        raise BundleError("bundle directory must be absolute")
    reserved = now_utc()
    if owner is not None and owner.started_at_ms > _epoch_ms(reserved):
        raise ReviewUnresolved("native review owner run starts after the reservation")
    existing = _stored_json(store, attempt_id, "review_reservation")
    if existing is None:
        # A new reservation must be made from inside the wake run itself.  A
        # recorded run must still be open.  An unrecorded run must be the
        # session's persisted active writer (written at admission, not flush).
        if owner is not None:
            if owner.ended_at_ms is not None:
                raise ReviewUnresolved("native review owner run has already ended")
        else:
            try:
                writer = read_native_owner_writer(openclaw_database, owner_session_key)
            except HostRecordError as exc:
                raise ReviewUnresolved("native review owner writer is unresolved") from exc
            if writer.status != "running" or writer.active_writer_run_id != wake_run_id:
                raise ReviewUnresolved("native review owner run is not the active running writer")
    if existing is not None:
        reservation = _reservation_from_payload(existing)
        if (
            reservation.owner_session_key != owner_session_key
            or Path(reservation.bundle_dir) != bundle_dir.resolve()
        ):
            raise StoreConflict("review reservation differs from the stored reservation")
        if (
            owner is not None
            and reservation.spawn_arguments_json is not None
            and owner.started_at_ms > _epoch_ms(reservation.reserved_at)
        ):
            raise ReviewUnresolved("native review owner run starts after the reservation")
        if reservation.spawn_arguments_json is not None and (
            reservation.owner_run_id != owner_run_id
            or (
                reservation.owner_thread_id is not None
                and reservation.owner_thread_id != owner_thread_id
            )
        ):
            # A stored resolved thread must still resolve to itself; a stored
            # deferred thread may now be resolvable without conflict.
            raise StoreConflict(
                "review reservation owner run/thread differs from stored reservation"
            )
        _validate_bundle(
            store,
            attempt_id,
            Path(reservation.bundle_dir),
            expected_digest=reservation.bundle_sha256,
        )
        return reservation
    digest = build_review_bundle(store, attempt_id, bundle_dir, instructions=instructions)
    hypothesis = store.get_hypothesis(attempt.hypothesis_id)
    nonce = secrets.token_hex(12)
    task_name = f"review_{attempt_id.lower().replace('-', '_')}_{nonce}"
    if _NATIVE_TASK_NAME.fullmatch(task_name) is None:
        raise ReviewEvidenceError("native review task name is not canonical")
    prompt = (bundle_dir / "instructions.md").read_text(encoding="utf-8")
    if str(bundle_dir.resolve()) not in prompt:
        raise BundleError("native review prompt must contain the immutable absolute bundle path")
    # Reservation-side evidence only: the installed host stores the spawn
    # message as ciphertext, so this digest can never be compared with any
    # rollout or host record.  Host binding rests on the nonce task name.
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    reservation = ReviewReservation(
        attempt_id=attempt_id,
        commit=cast(str, attempt.commit),
        hypothesis_spec_sha256=hypothesis.spec_sha256,
        bundle_dir=str(bundle_dir.resolve()),
        bundle_sha256=digest,
        reserved_at=reserved,
        owner_session_key=owner_session_key,
        label=task_name,
        reservation_nonce=nonce,
        owner_run_id=owner_run_id,
        owner_thread_id=owner_thread_id,
        task_name=task_name,
        prompt_sha256=prompt_sha256,
        spawn_arguments_json=to_json(
            {
                "agent_type": REVIEW_AGENT,
                "fork_turns": "none",
                "message": prompt,
                "task_name": task_name,
            }
        ),
    )
    store.insert_review_evidence(
        attempt_id, "review_reservation", reservation.to_json(), "review_reserved"
    )
    return reservation


def acknowledge_review(
    store: ResearchStore,
    attempt_id: str,
    mode: str,
    run_timeout_seconds: int | None,
    *,
    owner_session_key: str | None = None,
    owner_run_id: str | None = None,
    owner_thread_id: str | None = None,
    child_thread_id: str | None = None,
    _verified_native: bool = False,
) -> ReviewAck:
    reservation = _reservation(store, attempt_id)
    if mode != "run":
        raise ReviewEvidenceError("review ACK mode must be run")
    if run_timeout_seconds is not None and (
        type(run_timeout_seconds) is not int or run_timeout_seconds <= 0
    ):
        raise ReviewEvidenceError("run timeout must be a positive integer or null")
    if reservation.spawn_arguments_json is None:
        raise ReviewEvidenceError("historical ACP review cannot be acknowledged")
    if not _verified_native:
        raise ReviewEvidenceError("native ACK must be produced by official child reconciliation")
    if owner_session_key != reservation.owner_session_key:
        raise ReviewEvidenceError("native ACK owner session does not match reservation")
    if reservation.owner_run_id is not None and owner_run_id != reservation.owner_run_id:
        raise ReviewEvidenceError("native ACK owner run does not match reservation")
    if reservation.owner_thread_id is not None and owner_thread_id != reservation.owner_thread_id:
        raise ReviewEvidenceError("native ACK owner thread does not match reservation")
    if not child_thread_id:
        raise ReviewEvidenceError("native ACK child thread is required")
    ack = ReviewAck(
        None,
        None,
        mode,
        run_timeout_seconds,
        now_utc(),
        owner_session_key,
        owner_run_id,
        owner_thread_id,
        child_thread_id,
    )
    existing = _stored_json(store, attempt_id, "review_ack")
    if existing is not None:
        requested = json.loads(ack.to_json())
        comparable = {key: value for key, value in requested.items() if key != "acked_at"}
        stored_comparable = {key: value for key, value in existing.items() if key != "acked_at"}
        if stored_comparable != comparable:
            raise StoreConflict("review ACK differs from the stored ACK")
        return _ack_from_payload(existing)
    store.insert_review_evidence(attempt_id, "review_ack", ack.to_json(), "review_acknowledged")
    return ack


def _bound_owner_thread_id(reservation: ReviewReservation, openclaw_database: Path) -> str:
    """Return the reservation's owner thread, resolving a deferred one.

    A reservation made inside the owner run cannot bind that run's thread
    because the host flushes the run's events only afterwards.  Such a
    reservation stores ``owner_thread_id = None``; here the thread comes from
    the bound owner run's own ``session.started`` event (never written back to
    the immutable reservation).  A run that is still unflushed raises
    ``HostRecordNotRecorded`` (pending); the open-interval and spawn-time
    rules are enforced by ``read_exact_native_child`` with the resolved thread.
    """

    if reservation.owner_thread_id is not None:
        return reservation.owner_thread_id
    if reservation.owner_run_id is None:
        raise HostRecordError("native review reservation has no owner run")
    reserved_at_ms = _epoch_ms(reservation.reserved_at)
    owner = read_exact_native_owner(
        openclaw_database,
        reservation.owner_session_key,
        reserved_at_ms,
        expected_run_id=reservation.owner_run_id,
    )
    if owner.started_at_ms > reserved_at_ms:
        raise HostRecordError("native owner run starts after the reservation")
    return owner.thread_id


def reconcile_review(
    store: ResearchStore,
    attempt_id: str,
    openclaw_database: Path,
    codex_state_database: Path | None = None,
) -> ReviewAck | None:
    """Recover one lost spawn ACK; ambiguous correlation pauses the campaign."""

    reservation = _reservation(store, attempt_id)
    existing = _stored_json(store, attempt_id, "review_ack")
    if existing is not None:
        return _ack_from_payload(existing)
    if reservation.spawn_arguments_json is not None:
        if reservation.owner_run_id is None or codex_state_database is None:
            store.pause(f"review_unresolved:{attempt_id}")
            store.record_review_event(attempt_id, "review_unresolved", {"reason": "owner_identity"})
            raise ReviewUnresolved("native owner run/thread identity is unresolved")
        try:
            owner_thread_id = _bound_owner_thread_id(reservation, openclaw_database)
            child = read_exact_native_child(
                openclaw_database,
                codex_state_database,
                reservation.owner_session_key,
                reservation.owner_run_id,
                owner_thread_id,
                reservation.task_name or "",
                _epoch_ms(reservation.reserved_at),
            )
        except HostRecordNotRecorded as exc:
            if reservation.owner_thread_id is None:
                # Deferred reservation, no ACK yet: the owner run is not flushed.
                raise ReviewPending("native owner run is not yet recorded") from exc
            store.pause(f"review_unresolved:{attempt_id}")
            store.record_review_event(attempt_id, "review_unresolved", {"reason": "owner_missing"})
            raise ReviewUnresolved("native owner run vanished after reservation") from exc
        except HostRecordPending as exc:
            raise ReviewPending("native owner turn has not reached its official end") from exc
        except HostRecordError as exc:
            store.pause(f"review_unresolved:{attempt_id}")
            store.record_review_event(attempt_id, "review_unresolved", {"reason": "correlation"})
            raise ReviewUnresolved("native review correlation is unresolved") from exc
        return acknowledge_review(
            store,
            attempt_id,
            REVIEW_NATIVE_MODE,
            None,
            owner_session_key=child.owner_session_key,
            owner_run_id=child.owner_run_id,
            owner_thread_id=child.parent_thread_id,
            child_thread_id=child.child_thread_id,
            _verified_native=True,
        )
    # Historical ACP reservations remain readable by the store, but there is
    # no active ACP reconciliation path.  Reusing their old task projection
    # here would silently revive the retired runtime.
    store.pause(f"review_unresolved:{attempt_id}")
    store.record_review_event(attempt_id, "review_unresolved", {"reason": "legacy_review"})
    raise ReviewUnresolved("historical ACP review cannot be reconciled")


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


def _stored_containment_provenance(
    store: ResearchStore, attempt_id: str
) -> dict[str, object] | None:
    try:
        payload = _stored_json(store, attempt_id, "containment_provenance")
    except (StoreConflict, json.JSONDecodeError) as exc:
        raise BundleError("containment provenance evidence is malformed") from exc
    if payload is None:
        return None
    return _checked_containment_provenance(payload)


def _checked_containment_provenance(payload: object) -> dict[str, object]:
    if (
        not isinstance(payload, dict)
        or payload.get("contract") != "research-provenance-evidence-v1"
        or not isinstance(payload.get("stages"), list)
        or not payload["stages"]
    ):
        raise BundleError("containment provenance evidence is malformed")
    return cast(dict[str, object], payload)


def _reservation_from_payload(payload: Mapping[str, object]) -> ReviewReservation:
    legacy_fields = (
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
    native_fields = set(legacy_fields) | {
        "owner_run_id",
        "owner_thread_id",
        "task_name",
        "prompt_sha256",
        "spawn_arguments",
    }
    if set(payload) not in (set(legacy_fields), native_fields) or any(
        not isinstance(payload[field], str) or not payload[field] for field in legacy_fields
    ):
        raise StoreConflict("review reservation payload is malformed")
    if set(payload) == set(legacy_fields):
        return ReviewReservation(*(str(payload[field]) for field in legacy_fields))
    optional = {
        key: payload[key]
        for key in ("owner_run_id", "owner_thread_id", "task_name", "prompt_sha256")
    }
    if any(
        key in {"task_name", "prompt_sha256"} and (not isinstance(value, str) or not value)
        for key, value in optional.items()
    ) or any(
        key in {"owner_run_id", "owner_thread_id"}
        and value is not None
        and (not isinstance(value, str) or not value)
        for key, value in optional.items()
    ):
        raise StoreConflict("native review reservation identity is malformed")
    arguments = payload["spawn_arguments"]
    if not isinstance(arguments, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in arguments.items()
    ):
        raise StoreConflict("native spawn arguments are malformed")
    if set(arguments) != {"agent_type", "fork_turns", "message", "task_name"}:
        raise StoreConflict("native spawn arguments have an unexpected key set")
    canonical_arguments = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    message = arguments.get("message")
    if (
        not isinstance(message, str)
        or hashlib.sha256(message.encode()).hexdigest() != optional["prompt_sha256"]
    ):
        raise StoreConflict("native spawn prompt digest is malformed")
    if (
        arguments.get("agent_type") != REVIEW_AGENT
        or arguments.get("fork_turns") != "none"
        or arguments.get("task_name") != optional["task_name"]
        or str(payload["bundle_dir"]) not in message
        or _NATIVE_TASK_NAME.fullmatch(str(optional["task_name"])) is None
    ):
        raise StoreConflict("native spawn arguments do not match reservation")
    return ReviewReservation(
        attempt_id=str(payload["attempt_id"]),
        commit=str(payload["commit"]),
        hypothesis_spec_sha256=str(payload["hypothesis_spec_sha256"]),
        bundle_dir=str(payload["bundle_dir"]),
        bundle_sha256=str(payload["bundle_sha256"]),
        reserved_at=str(payload["reserved_at"]),
        owner_session_key=str(payload["owner_session_key"]),
        label=str(payload["label"]),
        reservation_nonce=str(payload["reservation_nonce"]),
        owner_run_id=(
            optional["owner_run_id"] if isinstance(optional["owner_run_id"], str) else None
        ),
        owner_thread_id=(
            optional["owner_thread_id"] if isinstance(optional["owner_thread_id"], str) else None
        ),
        task_name=str(optional["task_name"]),
        prompt_sha256=str(optional["prompt_sha256"]),
        spawn_arguments_json=canonical_arguments,
    )


def _reservation(store: ResearchStore, attempt_id: str) -> ReviewReservation:
    payload = _stored_json(store, attempt_id, "review_reservation")
    if payload is None:
        raise ReviewEvidenceError("review reservation is missing")
    return _reservation_from_payload(payload)


def _ack_from_payload(payload: Mapping[str, object]) -> ReviewAck:
    historical = {"child_session_key", "run_id", "mode", "run_timeout_seconds", "acked_at"}
    native = {
        "mode",
        "run_timeout_seconds",
        "acked_at",
        "owner_session_key",
        "owner_run_id",
        "owner_thread_id",
        "child_thread_id",
    }
    keys = set(payload)
    if keys not in (historical, native):
        raise StoreConflict("review ACK payload is malformed")
    mode = payload["mode"]
    timeout = payload["run_timeout_seconds"]
    at = payload["acked_at"]
    if not isinstance(mode, str) or not mode or not isinstance(at, str) or not at:
        raise StoreConflict("review ACK identity is malformed")
    if mode != "run":
        raise StoreConflict("review ACK mode is not run")
    if timeout is not None and (type(timeout) is not int or timeout <= 0):
        raise StoreConflict("review ACK timeout is malformed")
    identity = sorted(keys - {"mode", "run_timeout_seconds", "acked_at"})
    for field in identity:
        value = payload[field]
        if not isinstance(value, str) or not value:
            raise StoreConflict("review ACK identity is malformed")
    if keys == historical:
        return ReviewAck(
            str(payload["child_session_key"]), str(payload["run_id"]), mode, timeout, at
        )
    return ReviewAck(
        None,
        None,
        mode,
        timeout,
        at,
        str(payload["owner_session_key"]),
        str(payload["owner_run_id"]),
        str(payload["owner_thread_id"]),
        str(payload["child_thread_id"]),
    )


def _final_verdict_text(
    text: str, reservation: ReviewReservation
) -> tuple[str, tuple[str, ...], str]:
    """Parse the native task_complete last-agent message strictly."""

    if (
        not isinstance(text, str)
        or not text.strip().startswith("{")
        or not text.strip().endswith("}")
    ):
        raise ReviewEvidenceError("final review verdict must be bare JSON")
    try:
        raw: object = json.loads(text.strip(), object_pairs_hook=_strict_json_object)
    except json.JSONDecodeError as exc:
        raise ReviewEvidenceError("final review verdict is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ReviewEvidenceError("final review verdict must be an object")
    required = {"verdict", "attempt_id", "commit", "spec_sha256", "findings"}
    if set(raw) != required:
        raise ReviewEvidenceError("final review verdict has unexpected keys")
    if (
        raw.get("verdict") not in {"PASS", "FAIL"}
        or raw.get("attempt_id") != reservation.attempt_id
        or raw.get("commit") != reservation.commit
        or raw.get("spec_sha256") != reservation.hypothesis_spec_sha256
    ):
        raise ReviewEvidenceError("verdict_binding")
    findings = raw.get("findings")
    if not isinstance(findings, list) or any(
        not isinstance(item, str) or not item for item in findings
    ):
        raise ReviewEvidenceError("review findings are malformed")
    return str(raw["verdict"]), tuple(findings), text.strip()


def verify_native_review(
    store: ResearchStore,
    attempt_id: str,
    openclaw_database: Path,
    codex_state_database: Path,
) -> ReviewVerification:
    """Verify one exact native Sol child and persist no mutable host state."""

    reservation = _reservation(store, attempt_id)
    if reservation.spawn_arguments_json is None:
        raise ReviewEvidenceError("native review reservation is missing spawn arguments")
    if reservation.owner_run_id is None or reservation.task_name is None:
        raise ReviewUnresolved("native review reservation is missing owner identity")
    ack_payload = _stored_json(store, attempt_id, "review_ack")
    if ack_payload is None:
        raise ReviewPending("review ACK is pending")
    ack = _ack_from_payload(ack_payload)
    try:
        owner_thread_id = _bound_owner_thread_id(reservation, openclaw_database)
    except HostRecordNotRecorded as exc:
        # An ACK exists, so the owner run was recorded once: it cannot vanish.
        store.pause(f"review_unresolved:{attempt_id}")
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "owner_missing"})
        raise ReviewUnresolved("native owner run vanished after the review ACK") from exc
    except HostRecordPending as exc:
        raise ReviewPending("native owner turn has not reached its official end") from exc
    except HostRecordError as exc:
        store.pause(f"review_unresolved:{attempt_id}")
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "correlation"})
        raise ReviewUnresolved("native review correlation is unresolved") from exc
    if (
        ack.owner_session_key != reservation.owner_session_key
        or ack.owner_run_id != reservation.owner_run_id
        or ack.owner_thread_id != owner_thread_id
        or not ack.child_thread_id
    ):
        store.pause(f"review_unresolved:{attempt_id}")
        raise ReviewUnresolved("native review ACK identity does not match reservation")
    try:
        child = read_exact_native_child(
            openclaw_database,
            codex_state_database,
            reservation.owner_session_key,
            reservation.owner_run_id,
            owner_thread_id,
            reservation.task_name,
            _epoch_ms(reservation.reserved_at),
            corroborate_completion_callback=True,
        )
    except HostRecordNotRecorded as exc:
        store.pause(f"review_unresolved:{attempt_id}")
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "owner_missing"})
        raise ReviewUnresolved("native owner run vanished after the review ACK") from exc
    except HostRecordPending as exc:
        raise ReviewPending("native owner turn has not reached its official end") from exc
    except HostRecordError as exc:
        store.pause(f"review_unresolved:{attempt_id}")
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "correlation"})
        raise ReviewUnresolved("native review correlation is unresolved") from exc
    if child.child_thread_id != ack.child_thread_id:
        store.pause(f"review_unresolved:{attempt_id}")
        raise ReviewUnresolved("native review child differs from ACK")
    if child.terminal_state == "pending":
        # An unfinished child or a mid-append rollout tail: never a FAIL,
        # never a verdict.
        raise ReviewPending("native review completion is pending")
    if child.terminal_state != "succeeded":
        # failed/cancelled child: the host-failure path below records a FAIL
        # with the terminal state as the reason; the rollout is never parsed.
        return _failed_verification(
            reservation,
            child.terminal_state,
            child.terminal_state,
            native_child=child,
        )
    if child.last_agent_message is None:
        return _failed_verification(
            reservation,
            "terminal_verdict_missing",
            "native task_complete has no last_agent_message",
            native_child=child,
        )
    try:
        verdict, findings, verdict_json = _final_verdict_text(child.last_agent_message, reservation)
        _verify_reserved_bundle(Path(reservation.bundle_dir), reservation.bundle_sha256)
    except BundleError as exc:
        reason = (
            "bundle_mutated"
            if str(exc) == "reserved review bundle was modified"
            else f"bundle_invalid: {exc}"
        )
        return _failed_verification(
            reservation,
            reason,
            reason,
            native_child=child,
            verdict_json=child.last_agent_message,
        )
    except ReviewEvidenceError as exc:
        reason = str(exc) or "review_evidence_invalid"
        return _failed_verification(
            reservation,
            reason,
            reason,
            native_child=child,
            verdict_json=child.last_agent_message,
        )
    host = {
        "owner_session_key": reservation.owner_session_key,
        "owner_run_id": reservation.owner_run_id,
        "owner_thread_id": child.parent_thread_id,
        "child_thread_id": child.child_thread_id,
        "spawn_call_id": child.spawn_call_id,
        "announce_run_id": child.announce_run_id,
        "announce_status": child.announce_status,
        "task_name": child.task_name,
        "task_status": child.terminal_state,
        "rollout_path": str(child.rollout_path),
        "rollout_sha256": child.rollout_sha256,
        "model_observed": child.model,
        "reasoning_effort_observed": child.reasoning_effort,
        "agent_role_observed": child.agent_role,
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
        child.child_thread_id,
        str(host["collected_at"]),
    )
    return ReviewVerification(review, to_json(host))


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
    openclaw_database: Path,
    codex_state_database: Path,
) -> ReviewVerification:
    """Verify native Sol evidence from official OpenClaw/Codex stores."""

    if not codex_state_database.is_file() or codex_state_database.is_symlink():
        raise ReviewUnresolved("native Codex state database is missing or unsafe")
    try:
        return verify_native_review(store, attempt_id, openclaw_database, codex_state_database)
    except ReviewUnresolved:
        raise


def _failed_verification(
    reservation: ReviewReservation,
    reason: str,
    detail: str,
    *,
    verdict_json: str = "",
    native_child: NativeChildHostRecord,
) -> ReviewVerification:
    """Record a host-side FAIL.  No reviewer verdict is parsed or invented.

    The recorded finding is the host reason itself (for example ``failed`` or
    ``bundle_mutated``), and ``task_status`` is ``unresolved``.
    """

    collected = now_utc()
    host: dict[str, object] = {
        "child_thread_id": native_child.child_thread_id,
        "task_status": "unresolved",
        "verdict_json": verdict_json,
        "bound_commit": reservation.commit,
        "bound_spec_sha256": reservation.hypothesis_spec_sha256,
        "collected_at": collected,
        "verdict": "FAIL",
        "reason": reason,
        "detail": detail[:256],
        "owner_session_key": native_child.owner_session_key,
        "owner_run_id": native_child.owner_run_id,
        "owner_thread_id": native_child.parent_thread_id,
        "spawn_call_id": native_child.spawn_call_id,
        "announce_run_id": native_child.announce_run_id,
        "announce_status": native_child.announce_status,
        "child_terminal_state": native_child.terminal_state,
        "task_name": native_child.task_name,
        "rollout_path": str(native_child.rollout_path),
        "rollout_sha256": native_child.rollout_sha256,
        "model_observed": native_child.model,
        "reasoning_effort_observed": native_child.reasoning_effort,
        "agent_role_observed": native_child.agent_role,
    }
    review = ReviewEvidence(
        reservation.attempt_id,
        reservation.commit,
        reservation.hypothesis_spec_sha256,
        "FAIL",
        (reason,),
        REVIEW_MODEL,
        REVIEW_MODEL,
        native_child.child_thread_id,
        collected,
    )
    return ReviewVerification(review, to_json(host))


async def request_cancel(
    request_once: Callable[..., Awaitable[Mapping[str, object]]],
    owner_session_key: str,
    agent_id: str,
    run_id: str,
) -> Mapping[str, object]:
    """Abort one exact native owner run and its controlled subagents."""

    return await request_once(
        "chat.abort",
        {"sessionKey": owner_session_key, "agentId": agent_id, "runId": run_id},
        timeout_seconds=30,
    )


def cancel_review(
    store: ResearchStore,
    attempt_id: str,
    reason: str,
    openclaw_database: Path,
    request_once: CancelTransport,
    codex_state_database: Path | None = None,
) -> CancelOutcome:
    """Pause, abort one exact native owner run, and reread child evidence."""
    store.pause(f"review_cancel:{attempt_id}")
    existing_cancel = _stored_json(store, attempt_id, "review_cancel")
    reservation = _reservation(store, attempt_id)
    if reservation.spawn_arguments_json is None:
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "legacy_review"})
        return CancelOutcome(attempt_id, None, "unresolved", True, "not_requested")
    if (
        codex_state_database is None
        or reservation.owner_run_id is None
        or reservation.task_name is None
    ):
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "owner_identity"})
        return CancelOutcome(attempt_id, None, "unresolved", True, "not_requested")
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
            CANCEL_ABORTED,
            CANCEL_OWNER_INACTIVE,
        } or rpc_result.startswith("RPC_ERROR:")
        if pending and task_id is not None:
            return _cancel_native_after_reread(
                store,
                openclaw_database,
                codex_state_database,
                reservation,
                attempt_id,
                task_id,
                rpc_result,
            )
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
        return _cancel_native_after_reread(
            store,
            openclaw_database,
            codex_state_database,
            reservation,
            attempt_id,
            requested_task[0],
            "UNKNOWN_RESPONSE",
        )
    try:
        child = read_exact_native_child(
            openclaw_database,
            codex_state_database,
            reservation.owner_session_key,
            reservation.owner_run_id,
            _bound_owner_thread_id(reservation, openclaw_database),
            reservation.task_name,
            _epoch_ms(reservation.reserved_at),
            corroborate_completion_callback=True,
        )
    except HostRecordNotRecorded:
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "owner_missing"})
        return CancelOutcome(attempt_id, None, "unresolved", True, "not_requested")
    except HostRecordPending:
        return CancelOutcome(attempt_id, None, "pending", True, "not_requested")
    except HostRecordError:
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "cancel_correlation"})
        return CancelOutcome(attempt_id, None, "unresolved", True, "not_requested")
    task_id = child.child_thread_id
    if child.terminal_state != "pending":
        result = "terminal:" + child.terminal_state
        store.record_review_cancel(attempt_id, task_id, reason, result)
        return CancelOutcome(attempt_id, task_id, child.terminal_state, False, result)
    if not store.record_review_cancel_request(attempt_id, task_id, reason):
        return CancelOutcome(attempt_id, task_id, "pending", True, "UNKNOWN_RESPONSE")

    async def invoke_cancel() -> Mapping[str, object]:
        # chat.abort addresses the owner run.  ``reviewer`` is the child role,
        # not the owning OpenClaw agent id; derive the owner id from the exact
        # persisted session key instead of trusting a caller-supplied value.
        owner_parts = reservation.owner_session_key.split(":")
        if len(owner_parts) < 2 or owner_parts[0] != "agent" or not owner_parts[1]:
            raise ReviewUnresolved("owner session key has no canonical agent id")
        owner_agent_id = owner_parts[1]
        return await request_once(
            reservation.owner_session_key,
            owner_agent_id,
            reservation.owner_run_id or "",
        )

    try:
        response = asyncio.run(invoke_cancel())
        result = _safe_rpc_result(response, reservation.owner_run_id)
    except (OpenClawTransportError, TimeoutError, OSError):
        result = "UNKNOWN_RESPONSE"
        store.record_review_cancel(attempt_id, task_id, reason, result)
        return _cancel_native_after_reread(
            store, openclaw_database, codex_state_database, reservation, attempt_id, task_id, result
        )
    except OpenClawError:
        result = "RPC_REJECTED"
        store.record_review_cancel(attempt_id, task_id, reason, result)
        return _cancel_native_after_reread(
            store, openclaw_database, codex_state_database, reservation, attempt_id, task_id, result
        )
    except Exception as exc:
        result = f"RPC_ERROR:{type(exc).__name__}"
        store.record_review_cancel(attempt_id, task_id, reason, result)
        return _cancel_native_after_reread(
            store, openclaw_database, codex_state_database, reservation, attempt_id, task_id, result
        )
    store.record_review_cancel(attempt_id, task_id, reason, result)
    return _cancel_native_after_reread(
        store, openclaw_database, codex_state_database, reservation, attempt_id, task_id, result
    )


def _cancel_native_after_reread(
    store: ResearchStore,
    openclaw_database: Path,
    codex_state_database: Path,
    reservation: ReviewReservation,
    attempt_id: str,
    task_id: str,
    result: str,
) -> CancelOutcome:
    try:
        after = read_exact_native_child(
            openclaw_database,
            codex_state_database,
            reservation.owner_session_key,
            reservation.owner_run_id or "",
            _bound_owner_thread_id(reservation, openclaw_database),
            reservation.task_name or "",
            _epoch_ms(reservation.reserved_at),
            corroborate_completion_callback=True,
        )
    except HostRecordNotRecorded:
        store.record_review_event(attempt_id, "review_unresolved", {"reason": "owner_missing"})
        return CancelOutcome(attempt_id, task_id, "unresolved", True, result)
    except HostRecordError:
        return CancelOutcome(attempt_id, task_id, "pending", True, result)
    if after.child_thread_id != task_id:
        return CancelOutcome(attempt_id, task_id, "pending", True, result)
    if after.terminal_state == "pending":
        return CancelOutcome(attempt_id, task_id, "pending", True, result)
    return CancelOutcome(attempt_id, task_id, after.terminal_state, False, result)


def _safe_rpc_result(response: Mapping[str, object], owner_run_id: str) -> str:
    """Classify the installed ``chat.abort`` reply, which has exactly three fields.

    ``{ok: true, aborted: false, runIds: []}`` means the run was not active;
    ``{ok: true, aborted: true, runIds: [...]}`` means it was aborted.  Any
    other shape is malformed and any run id other than the owner's mismatched.
    """

    if not isinstance(response, Mapping) or set(response) != {"ok", "aborted", "runIds"}:
        return "MALFORMED_RESPONSE"
    aborted = response["aborted"]
    run_ids = response["runIds"]
    if (
        response["ok"] is not True
        or not isinstance(aborted, bool)
        or not isinstance(run_ids, list)
        or any(not isinstance(item, str) or not item for item in run_ids)
    ):
        return "MALFORMED_RESPONSE"
    if not aborted:
        return CANCEL_OWNER_INACTIVE if not run_ids else "MALFORMED_RESPONSE"
    if not run_ids:
        return "MALFORMED_RESPONSE"
    if owner_run_id not in run_ids:
        return "MISMATCHED_RESPONSE"
    return CANCEL_ABORTED


def collect_review(
    store: ResearchStore,
    attempt_id: str,
    openclaw_database: Path,
    codex_state_database: Path,
) -> Attempt:
    """Verify and atomically persist host evidence plus review transition."""

    existing = _stored_json(store, attempt_id, "review_host_evidence")
    if existing is not None:
        current = store.get_attempt(attempt_id)
        review_payload = _stored_json(store, attempt_id, "review")
        if review_payload is not None:
            parsed = ReviewRecord.from_json(to_json(review_payload))
            stored = ReviewEvidence(
                parsed.attempt_id,
                parsed.commit,
                parsed.spec_sha256,
                parsed.verdict,
                parsed.findings,
                parsed.reported_reviewer_model,
                parsed.reported_reviewer_actual_model,
                parsed.acp_session_id,
                parsed.submitted_at,
            )
            return store.collect_review_evidence(
                attempt_id, stored, store.evidence(attempt_id, "review_host_evidence")
            )
        if current.state in {AttemptState.REVIEW_PASSED, AttemptState.REVIEW_FAILED}:
            raise StoreConflict("review host evidence has no matching review payload")
    verification = verify_review(
        store,
        attempt_id,
        openclaw_database,
        codex_state_database,
    )
    return store.collect_review_evidence(attempt_id, verification.review, verification.host_payload)
