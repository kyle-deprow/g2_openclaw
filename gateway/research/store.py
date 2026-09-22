"""SQLite source of truth and repairable filesystem projections."""

# SQL statements are kept close to their transaction for auditability.
# ruff: noqa: E501

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast

from .admission import AdmissionDecision, CampaignPolicy
from .codec import to_json
from .containment import ContainmentError, runtime_pins
from .contracts import (
    Attempt,
    AttemptDecision,
    AttemptState,
    EvaluationSpecEntry,
    EvaluationSpecSet,
    Event,
    HypothesisDecision,
    HypothesisSpec,
    HypothesisState,
    ImplementationRecord,
    ReviewEvidence,
    RunOutcome,
    RunPlan,
)
from .machine import (
    IllegalTransition,
    decide_hypothesis,
    freeze,
    queue_run,
    start_run,
    submit_implementation,
    submit_review,
)
from .machine import (
    close_attempt as machine_close_attempt,
)
from .machine import (
    open_attempt as machine_open_attempt,
)


class OwnerLockHeld(RuntimeError):
    """Another driver process owns this campaign."""


class StoreConflict(RuntimeError):
    """A repeat operation supplied a different immutable payload."""


MAX_QUEUE_TIMEOUT_SECONDS = 7200.0
MAX_QUEUE_RSS_MB = 8192
MAX_EXPOSURE_LEDGER_BYTES = 8 * 1024 * 1024


def validate_queue_limits(timeout_seconds: int | float, max_rss_mb: int) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
        or timeout_seconds > MAX_QUEUE_TIMEOUT_SECONDS
    ):
        raise ValueError("timeout-seconds must be finite, positive, and at most 7200")
    if (
        isinstance(max_rss_mb, bool)
        or not isinstance(max_rss_mb, int)
        or not 0 < max_rss_mb <= MAX_QUEUE_RSS_MB
    ):
        raise ValueError("max-rss-mb must be positive and at most 8192")


def now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _read_bounded_exposure_ledger(path: Path) -> bytes:
    """Read one immutable, bounded ledger artifact before changing campaign state."""
    if path.is_symlink():
        raise ValueError("exposure ledger must not be a symlink")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError("exposure ledger is missing or unreadable") from exc
    try:
        first = os.fstat(descriptor)
        if not stat.S_ISREG(first.st_mode) or first.st_size > MAX_EXPOSURE_LEDGER_BYTES:
            raise ValueError("exposure ledger is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_EXPOSURE_LEDGER_BYTES - total + 1),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_EXPOSURE_LEDGER_BYTES:
                raise ValueError("exposure ledger exceeds its size limit")
        if os.fstat(descriptor).st_size != total:
            raise ValueError("exposure ledger changed while being read")
        return b"".join(chunks)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("exposure ledger could not be read") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _digest(text: str) -> str:
    return sha256_bytes(text.encode())


class ResearchStore:
    """Durable campaign store rooted at an explicit directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_file: TextIO | None = None
        self._run_lock_file: TextIO | None = None
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.root / "state.sqlite3", timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS hypotheses (
                  hypothesis_id TEXT PRIMARY KEY, state TEXT NOT NULL, title TEXT NOT NULL,
                  spec_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL, panel_path TEXT NOT NULL,
                  receipt_path TEXT NOT NULL, evaluation_spec_path TEXT NOT NULL,
                  evaluation_spec_sha256 TEXT NOT NULL, panel_sha256 TEXT NOT NULL,
                  receipt_sha256 TEXT NOT NULL, dividends_path TEXT NOT NULL,
                  dividends_sha256 TEXT NOT NULL, max_attempts INTEGER NOT NULL,
                  base_commit TEXT NOT NULL, created_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                  attempt_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL REFERENCES hypotheses,
                  number INTEGER NOT NULL, state TEXT NOT NULL, worktree_path TEXT NOT NULL,
                  "commit" TEXT, implementation_sha256 TEXT, review_verdict TEXT,
                  review_commit TEXT, review_spec_sha256 TEXT, reported_reviewer_model TEXT,
                  reported_reviewer_actual_model TEXT, reported_coder_model TEXT, coder_effort TEXT,
                  coder_service_tier TEXT, run_job_id TEXT, run_outcome TEXT, decision TEXT,
                  decision_reason TEXT, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                  UNIQUE(hypothesis_id, number)
                );
                CREATE TABLE IF NOT EXISTS attempt_evidence (
                  attempt_id TEXT NOT NULL REFERENCES attempts, kind TEXT NOT NULL,
                  payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                  PRIMARY KEY(attempt_id, kind)
                );
                CREATE TABLE IF NOT EXISTS hypothesis_evidence (
                  hypothesis_id TEXT NOT NULL REFERENCES hypotheses, kind TEXT NOT NULL,
                  payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                  PRIMARY KEY(hypothesis_id, kind)
                );
                CREATE TABLE IF NOT EXISTS events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                  hypothesis_id TEXT NOT NULL, attempt_id TEXT, kind TEXT NOT NULL,
                  detail TEXT NOT NULL, actor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                  job_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts,
                  state TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS driver_config (
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1), shared_python TEXT NOT NULL,
                  shared_python_sha256 TEXT NOT NULL, evaluator TEXT NOT NULL,
                  evaluator_sha256 TEXT NOT NULL, evaluator_source TEXT NOT NULL,
                  evaluator_source_sha256 TEXT NOT NULL,
                  snapshot_dir TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
                  pyvenv_cfg TEXT NOT NULL, pyvenv_sha256 TEXT NOT NULL,
                  distribution_dir TEXT NOT NULL, shared_python_resolved TEXT NOT NULL,
                  universe TEXT NOT NULL, universe_sha256 TEXT NOT NULL,
                  containment_ready INTEGER NOT NULL CHECK(containment_ready=1)
                );
                CREATE TABLE IF NOT EXISTS campaign (
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1), status TEXT NOT NULL,
                  resume_seq INTEGER NOT NULL,
                  attempt_cap INTEGER,
                  wall_clock_cap_seconds REAL,
                  policy_set_at TEXT,
                  policy_operator_reference TEXT,
                  exposure_ledger_path TEXT,
                  exposure_ledger_sha256 TEXT
                );
                CREATE TABLE IF NOT EXISTS wake_deliveries (
                  pending_key TEXT PRIMARY KEY, attempt_id TEXT, state TEXT NOT NULL,
                  resume_seq INTEGER NOT NULL, sent_at TEXT NOT NULL, run_id TEXT NOT NULL,
                  turn_status TEXT, turn_checked_at TEXT
                );
                CREATE TRIGGER IF NOT EXISTS immutable_frozen_hypothesis
                BEFORE UPDATE ON hypotheses WHEN (OLD.state != 'DRAFT' OR NEW.state != 'DRAFT') AND
                  (NEW.spec_json != OLD.spec_json OR NEW.spec_sha256 != OLD.spec_sha256 OR
                   NEW.panel_sha256 != OLD.panel_sha256 OR NEW.receipt_sha256 != OLD.receipt_sha256 OR
                   NEW.evaluation_spec_sha256 != OLD.evaluation_spec_sha256 OR
                   NEW.dividends_path != OLD.dividends_path OR NEW.dividends_sha256 != OLD.dividends_sha256 OR
                   json_remove(NEW.payload_json, '$.state') != json_remove(OLD.payload_json, '$.state') OR
                   json_extract(NEW.payload_json, '$.hypothesis_id') != NEW.hypothesis_id OR
                   json_extract(NEW.payload_json, '$.state') != NEW.state OR
                   json_extract(NEW.payload_json, '$.title') != NEW.title OR
                   json_extract(NEW.payload_json, '$.spec_json') != NEW.spec_json OR
                   json_extract(NEW.payload_json, '$.spec_sha256') != NEW.spec_sha256 OR
                   json_extract(NEW.payload_json, '$.panel_path') != NEW.panel_path OR
                   json_extract(NEW.payload_json, '$.receipt_path') != NEW.receipt_path OR
                   json_extract(NEW.payload_json, '$.evaluation_spec_path') != NEW.evaluation_spec_path OR
                   json_extract(NEW.payload_json, '$.evaluation_spec_sha256') != NEW.evaluation_spec_sha256 OR
                   json_extract(NEW.payload_json, '$.panel_sha256') != NEW.panel_sha256 OR
                   json_extract(NEW.payload_json, '$.receipt_sha256') != NEW.receipt_sha256 OR
                   json_extract(NEW.payload_json, '$.dividends_path') != NEW.dividends_path OR
                   json_extract(NEW.payload_json, '$.dividends_sha256') != NEW.dividends_sha256 OR
                   json_extract(NEW.payload_json, '$.max_attempts') != NEW.max_attempts OR
                   json_extract(NEW.payload_json, '$.base_commit') != NEW.base_commit OR
                   json_extract(NEW.payload_json, '$.created_at') != NEW.created_at)
                BEGIN SELECT RAISE(ABORT, 'frozen hypothesis is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_closed_attempt
                BEFORE UPDATE ON attempts WHEN OLD.state = 'CLOSED'
                BEGIN SELECT RAISE(ABORT, 'closed attempt is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_attempt_evidence
                BEFORE UPDATE ON attempt_evidence
                BEGIN SELECT RAISE(ABORT, 'attempt evidence is insert-only'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_attempt_evidence_delete
                BEFORE DELETE ON attempt_evidence
                BEGIN SELECT RAISE(ABORT, 'attempt evidence is insert-only'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_hypothesis_evidence
                BEFORE UPDATE ON hypothesis_evidence
                BEGIN SELECT RAISE(ABORT, 'hypothesis evidence is insert-only'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_hypothesis_evidence_delete
                BEFORE DELETE ON hypothesis_evidence
                BEGIN SELECT RAISE(ABORT, 'hypothesis evidence is insert-only'); END;
                """
            )
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(campaign)").fetchall()
            }
            additions = (
                ("attempt_cap", "ALTER TABLE campaign ADD COLUMN attempt_cap INTEGER"),
                (
                    "wall_clock_cap_seconds",
                    "ALTER TABLE campaign ADD COLUMN wall_clock_cap_seconds REAL",
                ),
                ("policy_set_at", "ALTER TABLE campaign ADD COLUMN policy_set_at TEXT"),
                (
                    "policy_operator_reference",
                    "ALTER TABLE campaign ADD COLUMN policy_operator_reference TEXT",
                ),
                (
                    "exposure_ledger_path",
                    "ALTER TABLE campaign ADD COLUMN exposure_ledger_path TEXT",
                ),
                (
                    "exposure_ledger_sha256",
                    "ALTER TABLE campaign ADD COLUMN exposure_ledger_sha256 TEXT",
                ),
            )
            for name, statement in additions:
                if name not in columns:
                    conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO campaign(singleton,status,resume_seq) VALUES (1, 'ACTIVE', 0)"
            )

    def acquire_owner_lock(self) -> None:
        lock = open(self.root / "owner.lock", "a+", encoding="utf-8")  # noqa: SIM115
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise OwnerLockHeld(f"owner lock held: {self.root / 'owner.lock'}") from exc
        self._lock_file = lock

    def release_owner_lock(self) -> None:
        if self._lock_file is not None:
            lock = self._lock_file
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            self._lock_file = None

    def acquire_run_lock(self) -> None:
        lock = open(self.root / "run.lock", "a+", encoding="utf-8")  # noqa: SIM115
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise OwnerLockHeld(f"run lock held: {self.root / 'run.lock'}") from exc
        self._run_lock_file = lock

    def release_run_lock(self) -> None:
        if self._run_lock_file is not None:
            lock = self._run_lock_file
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
            self._run_lock_file = None

    def close(self) -> None:
        self.release_owner_lock()
        self.release_run_lock()

    def configure(
        self,
        shared_python: Path,
        evaluator: Path,
        snapshot_dir: Path,
        universe: Path,
    ) -> None:
        shared_python = shared_python.absolute()
        evaluator = evaluator.resolve()
        for path in (shared_python, evaluator):
            if (
                not path.is_absolute()
                or not path.is_file()
                or not os.access(path.resolve(), os.X_OK)
            ):
                raise ValueError(f"trusted executable is missing or not executable: {path}")
        try:
            pins = runtime_pins(snapshot_dir, shared_python, evaluator, universe)
        except ContainmentError as exc:
            raise ValueError(str(exc)) from exc
        source = evaluator
        values: tuple[object, ...] = (
            str(shared_python),
            sha256_file(shared_python),
            str(evaluator),
            sha256_file(evaluator),
            str(source),
            sha256_file(source),
            str(pins.snapshot_dir),
            pins.snapshot_sha256,
            str(pins.pyvenv_cfg),
            pins.pyvenv_sha256,
            str(pins.distribution_dir),
            str(pins.resolved_python),
            str(pins.universe),
            pins.universe_sha256,
            1,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM driver_config").fetchone() is not None:
                raise StoreConflict("driver configuration already initialized")
            conn.execute(
                "INSERT INTO driver_config(singleton,shared_python,shared_python_sha256,evaluator,evaluator_sha256,evaluator_source,evaluator_source_sha256,snapshot_dir,snapshot_sha256,pyvenv_cfg,pyvenv_sha256,distribution_dir,shared_python_resolved,universe,universe_sha256,containment_ready) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (1, *values),
            )
            self._event(conn, "H0001", None, "driver_configured", {}, "driver")
            conn.commit()

    def config(self) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM driver_config WHERE singleton=1").fetchone()
        if row is None:
            raise ValueError("research init has not configured trusted executables")
        return cast(sqlite3.Row, row)

    def campaign(self) -> tuple[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status,resume_seq FROM campaign WHERE singleton=1"
            ).fetchone()
        assert row is not None
        return str(row[0]), int(row[1])

    def _campaign_row(self) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM campaign WHERE singleton=1").fetchone()
        if row is None:
            raise StoreConflict("campaign singleton is missing")
        return cast(sqlite3.Row, row)

    def campaign_policy(self) -> CampaignPolicy:
        """Return the explicit operator policy, or an unset policy."""
        row = self._campaign_row()
        raw_attempt_cap = row["attempt_cap"]
        raw_wall_clock = row["wall_clock_cap_seconds"]
        if raw_attempt_cap is None:
            if raw_wall_clock is not None:
                raise StoreConflict("campaign policy has a wall-clock cap without an attempt cap")
            return CampaignPolicy(False)
        if isinstance(raw_attempt_cap, bool) or not isinstance(raw_attempt_cap, int):
            raise StoreConflict("campaign attempt cap is malformed")
        try:
            if raw_wall_clock is not None and (
                isinstance(raw_wall_clock, bool) or not isinstance(raw_wall_clock, (int, float))
            ):
                raise ValueError("campaign wall-clock cap is malformed")
            if not isinstance(row["policy_set_at"], str) or not row["policy_set_at"]:
                raise ValueError("campaign policy set-at timestamp is missing")
            if (
                not isinstance(row["policy_operator_reference"], str)
                or not row["policy_operator_reference"]
            ):
                raise ValueError("campaign policy operator reference is missing")
            wall_clock = None if raw_wall_clock is None else float(raw_wall_clock)
            return CampaignPolicy(True, raw_attempt_cap, wall_clock)
        except (TypeError, ValueError) as exc:
            raise StoreConflict(str(exc)) from exc

    def set_campaign_policy(
        self,
        attempt_cap: int,
        wall_clock_cap_seconds: float | None,
        operator_reference: str,
    ) -> CampaignPolicy:
        """Persist one explicit operator policy on the singleton campaign row."""
        policy = CampaignPolicy(True, attempt_cap, wall_clock_cap_seconds)
        if not isinstance(operator_reference, str) or not operator_reference:
            raise ValueError("policy operator reference must be a non-empty string")
        set_at = now_utc()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE campaign SET attempt_cap=?,wall_clock_cap_seconds=?,policy_set_at=?,policy_operator_reference=? WHERE singleton=1",
                (policy.attempt_cap, policy.wall_clock_cap_seconds, set_at, operator_reference),
            )
            row = conn.execute(
                "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
            ).fetchone()
            self._event(
                conn,
                str(row[0]) if row else "H0001",
                None,
                "campaign_policy_set",
                {"operator_reference": operator_reference, "set_at": set_at},
                "operator",
            )
            conn.commit()
        return policy

    def register_exposure_ledger(self, path: Path, sha256: str) -> tuple[str, str]:
        """Register operator-supplied ledger provenance after verifying its bytes."""
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError("exposure ledger SHA256 must be 64 hexadecimal characters")
        try:
            int(sha256, 16)
        except ValueError as exc:
            raise ValueError("exposure ledger SHA256 must be hexadecimal") from exc
        resolved_path = path.absolute()
        ledger_bytes = _read_bounded_exposure_ledger(resolved_path)
        actual_sha256 = hashlib.sha256(ledger_bytes).hexdigest()
        if actual_sha256 != sha256:
            raise ValueError("exposure ledger SHA256 does not match file bytes")
        resolved = str(resolved_path)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE campaign SET exposure_ledger_path=?,exposure_ledger_sha256=? WHERE singleton=1",
                (resolved, sha256),
            )
            row = conn.execute(
                "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
            ).fetchone()
            self._event(
                conn,
                str(row[0]) if row else "H0001",
                None,
                "exposure_ledger_registered",
                {"path": resolved, "sha256": sha256},
                "operator",
            )
            conn.commit()
        return resolved, sha256

    def exposure_ledger_registration(self) -> tuple[str, str] | None:
        row = self._campaign_row()
        path = row["exposure_ledger_path"]
        digest = row["exposure_ledger_sha256"]
        if path is None and digest is None:
            return None
        if not isinstance(path, str) or not isinstance(digest, str):
            raise StoreConflict("exposure ledger registration is incomplete")
        return path, digest

    def _event(
        self,
        conn: sqlite3.Connection,
        hypothesis_id: str,
        attempt_id: str | None,
        kind: str,
        detail: object,
        actor: str,
    ) -> None:
        conn.execute(
            "INSERT INTO events(at,hypothesis_id,attempt_id,kind,detail,actor) VALUES(?,?,?,?,?,?)",
            (
                now_utc(),
                hypothesis_id,
                attempt_id,
                kind,
                to_json(detail) if not isinstance(detail, str) else detail,
                actor,
            ),
        )

    def _projection(self, path: Path, payload: str) -> bool:
        return self._projection_bytes(path, payload.encode())

    def _projection_bytes(self, path: Path, data: bytes) -> bool:
        if path.is_file() and sha256_bytes(path.read_bytes()) == sha256_bytes(data):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return True

    def _validate_owned_spec_set(
        self, hypothesis: HypothesisSpec, spec_set: EvaluationSpecSet
    ) -> None:
        owned_root = (
            self.root / "hypotheses" / hypothesis.hypothesis_id / "evaluation-specs"
        ).resolve()
        primary = next(
            entry for entry in spec_set.specs if entry.spec_id == spec_set.primary_spec_id
        )
        if (
            primary.path != hypothesis.evaluation_spec_path
            or primary.sha256 != hypothesis.evaluation_spec_sha256
        ):
            raise StoreConflict("primary evaluation spec does not match immutable set")
        seen_paths: set[str] = set()
        for entry in spec_set.specs:
            path = Path(entry.path)
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise StoreConflict(f"evaluation spec is missing: {entry.spec_id}") from exc
            if (
                resolved.parent != owned_root
                or path.is_symlink()
                or not path.is_file()
                or path.stat().st_mode & 0o222
            ):
                raise StoreConflict(f"evaluation spec is not store-owned: {entry.spec_id}")
            if str(resolved) in seen_paths:
                raise StoreConflict("evaluation spec paths must be unique")
            seen_paths.add(str(resolved))
            if sha256_file(path) != entry.sha256:
                raise StoreConflict(f"evaluation spec digest mismatch: {entry.spec_id}")

    def _hypothesis_from_row(self, row: sqlite3.Row) -> HypothesisSpec:
        payload = str(row["payload_json"])
        if _digest(payload) != str(row["payload_sha256"]):
            raise StoreConflict("hypothesis payload digest mismatch")
        return HypothesisSpec.from_json(payload)

    def _attempt_from_row(self, row: sqlite3.Row) -> Attempt:
        payload = str(row["payload_json"])
        if _digest(payload) != str(row["payload_sha256"]):
            raise StoreConflict("attempt payload digest mismatch")
        return Attempt.from_json(payload)

    def get_hypothesis(self, hypothesis_id: str) -> HypothesisSpec:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown hypothesis: {hypothesis_id}")
        return self._hypothesis_from_row(row)

    def hypotheses(self) -> list[HypothesisSpec]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM hypotheses ORDER BY hypothesis_id").fetchall()
        return [self._hypothesis_from_row(row) for row in rows]

    def get_attempt(self, attempt_id: str) -> Attempt:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown attempt: {attempt_id}")
        return self._attempt_from_row(row)

    def attempts_for(self, hypothesis_id: str) -> list[Attempt]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE hypothesis_id=? ORDER BY number", (hypothesis_id,)
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    def events(self) -> list[Event]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [
            Event(
                int(row["seq"]),
                str(row["at"]),
                str(row["hypothesis_id"]),
                row["attempt_id"],
                str(row["kind"]),
                str(row["detail"]),
                str(row["actor"]),
            )
            for row in rows
        ]

    def create_hypothesis(
        self,
        title: str,
        spec_file: Path,
        panel: Path,
        receipt: Path,
        eval_spec: Path,
        base_commit: str,
        dividends: Path,
        max_attempts: int = 3,
        evaluation_spec_set: Path | None = None,
    ) -> HypothesisSpec:
        if any(h.state != HypothesisState.DECIDED for h in self.hypotheses()):
            raise ValueError("hypothesis-create requires every existing hypothesis to be DECIDED")
        with self._connect() as conn:
            configured = conn.execute(
                "SELECT containment_ready FROM driver_config WHERE singleton=1"
            ).fetchone()
        if configured is None or not bool(configured[0]):
            raise ValueError("contained runtime must be configured before hypothesis-create")
        if evaluation_spec_set is None:
            raise ValueError("evaluation spec set is required for every new hypothesis")
        if dividends.is_symlink() or not dividends.is_file():
            raise ValueError("dividends must be a regular non-symlink file")
        raw = json.loads(spec_file.read_text(encoding="utf-8"))
        spec_json = to_json(raw)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(CAST(SUBSTR(hypothesis_id,2) AS INTEGER)),0)+1 FROM hypotheses"
            ).fetchone()
            number = int(row[0])
            hid = f"H{number:04d}"
            created = now_utc()
            dividends_path = str(dividends.resolve())
            dividends_sha256 = sha256_file(dividends)
            if evaluation_spec_set.is_symlink() or not evaluation_spec_set.is_file():
                raise ValueError("evaluation spec set must be a regular non-symlink file")
            parsed_set = EvaluationSpecSet.from_json(
                evaluation_spec_set.read_text(encoding="utf-8")
            )
            if parsed_set.hypothesis_id != hid:
                raise ValueError("evaluation spec set hypothesis_id does not match new hypothesis")
            if parsed_set.primary_spec_id not in {entry.spec_id for entry in parsed_set.specs}:
                raise ValueError("evaluation spec set primary entry is missing")
            parsed_primary = next(
                entry for entry in parsed_set.specs if entry.spec_id == parsed_set.primary_spec_id
            )
            if sha256_file(eval_spec) != parsed_primary.sha256:
                raise ValueError("primary evaluation spec does not match immutable set")
            owned_dir = self.root / "hypotheses" / hid / "evaluation-specs"
            owned_entries: list[EvaluationSpecEntry] = []
            for entry in parsed_set.specs:
                source = Path(entry.path)
                content = source.read_bytes()
                if hashlib.sha256(content).hexdigest() != entry.sha256:
                    raise ValueError(f"evaluation spec digest mismatch: {entry.spec_id}")
                owned_path = owned_dir / f"{entry.spec_id}.json"
                self._projection_bytes(owned_path, content)
                owned_path.chmod(0o444)
                owned_entries.append(
                    EvaluationSpecEntry(entry.spec_id, str(owned_path), entry.sha256)
                )
            spec_set = EvaluationSpecSet(
                parsed_set.contract,
                parsed_set.hypothesis_id,
                parsed_set.primary_spec_id,
                tuple(owned_entries),
                parsed_set.created_at,
            )
            primary = next(
                entry for entry in spec_set.specs if entry.spec_id == spec_set.primary_spec_id
            )
            spec = HypothesisSpec(
                hid,
                title,
                spec_json,
                _digest(spec_json),
                str(panel.resolve()),
                str(receipt.resolve()),
                primary.path,
                primary.sha256,
                sha256_file(panel),
                sha256_file(receipt),
                max_attempts,
                base_commit,
                created,
                dividends_path,
                dividends_sha256,
                HypothesisState.DRAFT,
            )
            payload = spec.to_json()
            conn.execute(
                "INSERT INTO hypotheses(hypothesis_id,state,title,spec_json,spec_sha256,panel_path,receipt_path,evaluation_spec_path,evaluation_spec_sha256,panel_sha256,receipt_sha256,dividends_path,dividends_sha256,max_attempts,base_commit,created_at,payload_json,payload_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    hid,
                    spec.state.value,
                    title,
                    spec.spec_json,
                    spec.spec_sha256,
                    spec.panel_path,
                    spec.receipt_path,
                    spec.evaluation_spec_path,
                    spec.evaluation_spec_sha256,
                    spec.panel_sha256,
                    spec.receipt_sha256,
                    spec.dividends_path,
                    spec.dividends_sha256,
                    max_attempts,
                    base_commit,
                    created,
                    payload,
                    _digest(payload),
                ),
            )
            set_payload = spec_set.to_json()
            conn.execute(
                "INSERT INTO hypothesis_evidence VALUES(?,?,?,?)",
                (hid, "evaluation_spec_set", set_payload, _digest(set_payload)),
            )
            self._event(conn, hid, None, "hypothesis_created", {"title": title}, "astra")
            conn.commit()
        self._projection(self.root / "hypotheses" / hid / "spec.json", spec_json)
        self._projection(
            self.root / "hypotheses" / hid / "evaluation-spec-set.json", spec_set.to_json()
        )
        for entry in spec_set.specs:
            self._projection_bytes(Path(entry.path), Path(entry.path).read_bytes())
        return spec

    def _update_hypothesis(
        self, spec: HypothesisSpec, event: str, actor: str = "astra"
    ) -> HypothesisSpec:
        payload = spec.to_json()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE hypotheses SET state=?,payload_json=?,payload_sha256=? WHERE hypothesis_id=?",
                (spec.state.value, payload, _digest(payload), spec.hypothesis_id),
            )
            self._event(conn, spec.hypothesis_id, None, event, {"state": spec.state.value}, actor)
            conn.commit()
        self._projection(
            self.root / "hypotheses" / spec.hypothesis_id / "spec.json", spec.spec_json
        )
        return spec

    def freeze(self, hypothesis_id: str) -> HypothesisSpec:
        current = self.get_hypothesis(hypothesis_id)
        if current.state == HypothesisState.DRAFT:
            self._validate_owned_spec_set(current, self.evaluation_spec_set(hypothesis_id))
        return self._update_hypothesis(freeze(current), "hypothesis_frozen")

    def decide_hypothesis(
        self, hypothesis_id: str, decision: HypothesisDecision, reason: str
    ) -> HypothesisSpec:
        spec = decide_hypothesis(
            self.get_hypothesis(hypothesis_id), decision, self.attempts_for(hypothesis_id)
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            payload = spec.to_json()
            conn.execute(
                "UPDATE hypotheses SET state=?,payload_json=?,payload_sha256=? WHERE hypothesis_id=?",
                (spec.state.value, payload, _digest(payload), hypothesis_id),
            )
            self._event(
                conn,
                hypothesis_id,
                None,
                "hypothesis_decided",
                {"decision": decision.value, "reason": reason},
                "astra",
            )
            conn.commit()
        return spec

    def open_attempt(
        self,
        hypothesis_id: str,
        worktree: Path,
        *,
        admission: AdmissionDecision | None = None,
    ) -> Attempt:
        spec = self.get_hypothesis(hypothesis_id)
        if admission is not None:
            if not admission.admitted:
                raise StoreConflict("attempt admission decision is not admitted")
            if admission.hypothesis_spec.hypothesis_id != hypothesis_id:
                raise StoreConflict("attempt admission decision does not match hypothesis")
        existing = self.attempts_for(hypothesis_id)
        policy = self.campaign_policy()
        attempts = sum(len(self.attempts_for(spec.hypothesis_id)) for spec in self.hypotheses())
        if policy.is_set and policy.attempt_cap is not None and attempts >= policy.attempt_cap:
            raise StoreConflict("campaign attempt cap exceeded")
        attempt = machine_open_attempt(spec, existing, str(worktree.resolve()), now_utc())
        payload = attempt.to_json()
        admission_payload = admission.to_json() if admission is not None else None
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt.attempt_id,
                    attempt.hypothesis_id,
                    attempt.number,
                    attempt.state.value,
                    attempt.worktree_path,
                    attempt.commit,
                    attempt.implementation_sha256,
                    attempt.review_verdict,
                    attempt.review_commit,
                    attempt.review_spec_sha256,
                    attempt.reported_reviewer_model,
                    attempt.reported_reviewer_actual_model,
                    attempt.reported_coder_model,
                    attempt.coder_effort,
                    attempt.coder_service_tier,
                    attempt.run_job_id,
                    attempt.run_outcome,
                    attempt.decision,
                    attempt.decision_reason,
                    attempt.opened_at,
                    attempt.updated_at,
                    payload,
                    _digest(payload),
                ),
            )
            if admission_payload is not None:
                conn.execute(
                    "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
                    (
                        attempt.attempt_id,
                        "admission_decision",
                        admission_payload,
                        _digest(admission_payload),
                    ),
                )
            self._event(
                conn,
                hypothesis_id,
                attempt.attempt_id,
                "attempt_opened",
                {"number": attempt.number},
                "astra",
            )
            if admission is not None:
                self._event(
                    conn,
                    hypothesis_id,
                    attempt.attempt_id,
                    "admission_accepted",
                    {"decision_sha256": admission.sha256},
                    "driver",
                )
            conn.commit()
        if admission_payload is not None:
            self._repair_evidence_projection(attempt, "admission_decision", admission_payload)
        return attempt

    def submit_implementation(
        self,
        attempt_id: str,
        record: ImplementationRecord,
        run_plan: RunPlan | None = None,
    ) -> Attempt:
        attempt = self.get_attempt(attempt_id)
        if run_plan is None:
            raise StoreConflict("immutable run plan evidence is required")
        if run_plan.attempt_id != attempt_id or run_plan.commit != record.commit:
            raise StoreConflict("run plan does not match implementation")
        if run_plan.implementation_sha256 != _digest(record.to_json()):
            raise StoreConflict("run plan implementation digest does not match implementation")
        try:
            spec_set = self.evaluation_spec_set(attempt.hypothesis_id)
        except (StoreConflict, ValueError) as exc:
            raise StoreConflict("evaluation spec set evidence is required") from exc
        self._validate_owned_spec_set(self.get_hypothesis(attempt.hypothesis_id), spec_set)
        if run_plan.evaluation_spec_set_sha256 != _digest(spec_set.to_json()):
            raise StoreConflict("run plan evaluation spec set digest does not match evidence")
        primary = next(
            scenario
            for scenario in run_plan.scenarios
            if scenario.scenario_id == run_plan.primary_scenario_id
        )
        if primary.targets_argv != record.targets_argv:
            raise StoreConflict("primary scenario argv does not match implementation")
        entries = {entry.spec_id: entry.sha256 for entry in spec_set.specs}
        if any(
            scenario.spec_id not in entries
            or scenario.evaluation_spec_sha256 != entries[scenario.spec_id]
            for scenario in run_plan.scenarios
        ):
            raise StoreConflict("run plan scenario spec does not match evidence")
        run_plan_payload = run_plan.to_json()
        with self._connect() as conn:
            old = conn.execute(
                "SELECT payload_json FROM attempt_evidence WHERE attempt_id=? AND kind='implementation'",
                (attempt_id,),
            ).fetchone()
        if old is not None:
            if old[0] != record.to_json():
                raise StoreConflict("implementation payload differs from stored payload")
            existing_plan = conn.execute(
                "SELECT payload_json FROM attempt_evidence WHERE attempt_id=? AND kind='run_plan'",
                (attempt_id,),
            ).fetchone()
            if existing_plan is None or str(existing_plan[0]) != run_plan_payload:
                raise StoreConflict("run plan payload differs from stored payload")
            self._repair_evidence_projection(attempt, "implementation", record.to_json())
            self._repair_evidence_projection(attempt, "run_plan", run_plan_payload)
            return attempt
        updated = submit_implementation(attempt, record, now_utc())
        payload = updated.to_json()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
                (attempt_id, "implementation", record.to_json(), _digest(record.to_json())),
            )
            conn.execute(
                "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
                (attempt_id, "run_plan", run_plan_payload, _digest(run_plan_payload)),
            )
            conn.execute(
                'UPDATE attempts SET state=?,"commit"=?,implementation_sha256=?,review_verdict=NULL,review_commit=NULL,review_spec_sha256=NULL,reported_reviewer_model=NULL,reported_reviewer_actual_model=NULL,reported_coder_model=?,coder_effort=?,coder_service_tier=?,updated_at=?,payload_json=?,payload_sha256=? WHERE attempt_id=?',
                (
                    updated.state.value,
                    updated.commit,
                    updated.implementation_sha256,
                    updated.reported_coder_model,
                    updated.coder_effort,
                    updated.coder_service_tier,
                    updated.updated_at,
                    payload,
                    _digest(payload),
                    attempt_id,
                ),
            )
            self._event(
                conn,
                attempt.hypothesis_id,
                attempt_id,
                "implementation_submitted",
                {"commit": record.commit},
                "astra",
            )
            conn.commit()
        self._projection(
            self.root
            / "hypotheses"
            / attempt.hypothesis_id
            / "attempts"
            / attempt_id
            / "implementation.json",
            record.to_json(),
        )
        self._projection(
            self.root
            / "hypotheses"
            / attempt.hypothesis_id
            / "attempts"
            / attempt_id
            / "run-plan.json",
            run_plan_payload,
        )
        return updated

    def insert_review_evidence(self, attempt_id: str, kind: str, payload: str, event: str) -> None:
        """Insert one immutable review lifecycle payload, idempotently."""
        if kind not in {"review_reservation", "review_ack", "review_cancel"}:
            raise ValueError("unsupported review lifecycle evidence kind")
        attempt = self.get_attempt(attempt_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "SELECT payload_json,payload_sha256 FROM attempt_evidence WHERE attempt_id=? AND kind=?",
                (attempt_id, kind),
            ).fetchone()
            if old is not None:
                if _digest(str(old[0])) != str(old[1]) or str(old[0]) != payload:
                    raise StoreConflict(f"{kind} payload differs from stored payload")
                conn.commit()
                # The SQLite row is authoritative.  A crash after the row
                # commit but before the filesystem projection must be
                # repaired by an idempotent retry.
                self._repair_evidence_projection(attempt, kind, payload)
                return
            current_row = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if current_row is None:
                raise ValueError(f"unknown attempt: {attempt_id}")
            current = self._attempt_from_row(current_row)
            if current.state == AttemptState.CLOSED:
                raise StoreConflict("cannot insert review lifecycle evidence on closed attempt")
            if kind == "review_reservation" and current.state != AttemptState.IMPLEMENTED:
                raise StoreConflict("review reservation requires an implemented attempt")
            conn.execute(
                "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
                (attempt_id, kind, payload, _digest(payload)),
            )
            self._event(
                conn, attempt.hypothesis_id, attempt_id, event, json.loads(payload), "driver"
            )
            conn.commit()
        self._repair_evidence_projection(attempt, kind, payload)

    def insert_admission_decision(self, attempt_id: str, decision: AdmissionDecision) -> None:
        """Persist one accepted typed admission decision as insert-only evidence."""
        if not isinstance(decision, AdmissionDecision) or not decision.admitted:
            raise ValueError("only an admitted decision can be persisted")
        attempt = self.get_attempt(attempt_id)
        if decision.hypothesis_spec.hypothesis_id != attempt.hypothesis_id:
            raise StoreConflict("admission decision does not match attempt hypothesis")
        try:
            spec_set_digest = _digest(self.evaluation_spec_set(attempt.hypothesis_id).to_json())
        except (StoreConflict, ValueError) as exc:
            raise StoreConflict("admission decision requires immutable spec-set evidence") from exc
        if decision.evaluation_spec_set_sha256 != spec_set_digest:
            raise StoreConflict("admission decision spec-set digest does not match evidence")
        payload = decision.to_json()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "SELECT payload_json,payload_sha256 FROM attempt_evidence WHERE attempt_id=? AND kind='admission_decision'",
                (attempt_id,),
            ).fetchone()
            if old is not None:
                if _digest(str(old[0])) != str(old[1]) or str(old[0]) != payload:
                    raise StoreConflict("admission decision differs from stored payload")
                conn.commit()
                self._repair_evidence_projection(attempt, "admission_decision", payload)
                return
            conn.execute(
                "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
                (attempt_id, "admission_decision", payload, _digest(payload)),
            )
            self._event(
                conn,
                attempt.hypothesis_id,
                attempt_id,
                "admission_accepted",
                {"decision_sha256": decision.sha256},
                "driver",
            )
            conn.commit()
        self._repair_evidence_projection(attempt, "admission_decision", payload)

    def record_admission_refusal(
        self,
        hypothesis_id: str,
        decision: AdmissionDecision,
        *,
        attempt_id: str | None = None,
    ) -> None:
        """Record the latest typed refusal for status and owner wake surfaces."""
        if decision.admitted or decision.hypothesis_spec.hypothesis_id != hypothesis_id:
            raise ValueError("admission refusal does not match hypothesis")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._event(
                conn,
                hypothesis_id,
                attempt_id,
                "admission_refused",
                {
                    "detail": decision.detail,
                    "reason": decision.reason.value if decision.reason is not None else None,
                },
                "driver",
            )
            conn.commit()

    def record_admission_failure(
        self,
        hypothesis_id: str,
        reason: str,
        detail: str,
        *,
        attempt_id: str | None = None,
    ) -> None:
        """Record a fail-closed input failure before typed admission can run."""
        if not reason or not detail:
            raise ValueError("admission failure reason and detail are required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._event(
                conn,
                hypothesis_id,
                attempt_id,
                "admission_refused",
                {"detail": detail, "reason": reason},
                "driver",
            )
            conn.commit()

    def latest_admission_refusal(self, hypothesis_id: str) -> tuple[str, str | None] | None:
        """Return the latest refusal, clearing only reasons fixed by updates."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind,detail FROM events WHERE hypothesis_id=? "
                "OR kind IN ('campaign_policy_set','exposure_ledger_registered') "
                "ORDER BY seq",
                (hypothesis_id,),
            ).fetchall()
        refusal: tuple[str, str | None] | None = None
        for row in rows:
            kind = str(row["kind"])
            if kind in {"campaign_policy_set", "exposure_ledger_registered"}:
                if refusal is None:
                    continue
                reason = refusal[0]
                if (
                    kind == "campaign_policy_set"
                    and reason
                    in {
                        "CAMPAIGN_POLICY_UNSET",
                        "CAMPAIGN_ATTEMPT_CAP_EXCEEDED",
                    }
                ) or (
                    kind == "exposure_ledger_registered"
                    and reason
                    in {
                        "EXPOSURE_LEDGER_INVALID",
                        "HOLDOUT_KNOWN_EXPOSED",
                        "HOLDOUT_HISTORY_UNKNOWN",
                    }
                ):
                    refusal = None
                continue
            if kind != "admission_refused":
                continue
            try:
                detail = json.loads(str(row["detail"]))
            except json.JSONDecodeError as exc:
                raise StoreConflict("admission refusal event is malformed") from exc
            if not isinstance(detail, dict) or not isinstance(detail.get("reason"), str):
                raise StoreConflict("admission refusal event is malformed")
            reason = cast(str, detail["reason"])
            refusal_detail = detail.get("detail")
            refusal = (reason, refusal_detail if isinstance(refusal_detail, str) else None)
        return refusal

    def record_review_event(self, attempt_id: str, kind: str, detail: object) -> None:
        attempt = self.get_attempt(attempt_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._event(conn, attempt.hypothesis_id, attempt_id, kind, detail, "driver")
            conn.commit()

    def record_review_cancel_request(self, attempt_id: str, task_id: str, reason: str) -> bool:
        """Record one exact cancellation request, returning whether it is new."""
        attempt = self.get_attempt(attempt_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute(
                "SELECT detail FROM events WHERE attempt_id=? AND kind='review_cancel_requested' ORDER BY seq DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            if old is not None:
                try:
                    detail = json.loads(str(old[0]))
                except json.JSONDecodeError as exc:
                    raise StoreConflict("review cancellation request event is malformed") from exc
                if (
                    not isinstance(detail, dict)
                    or detail.get("task_id") != task_id
                    or detail.get("reason") != reason
                ):
                    raise StoreConflict("review cancellation task differs from stored request")
                conn.commit()
                return False
            self._event(
                conn,
                attempt.hypothesis_id,
                attempt_id,
                "review_cancel_requested",
                {"task_id": task_id, "reason": reason},
                "driver",
            )
            conn.commit()
        return True

    def record_review_cancel(
        self, attempt_id: str, task_id: str, reason: str, rpc_result: str
    ) -> None:
        payload = to_json(
            {
                "task_id": task_id,
                "reason": reason,
                "requested_at": now_utc(),
                "rpc_result": rpc_result,
            }
        )
        self.insert_review_evidence(attempt_id, "review_cancel", payload, "review_cancel_recorded")

    def review_reservation_attempts(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT attempt_id FROM attempt_evidence WHERE kind='review_reservation' ORDER BY attempt_id"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def review_cancel_requested(self, attempt_id: str) -> tuple[str, str] | None:
        """Return the exact task/reason for an unfinalized cancellation request."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT detail FROM events WHERE attempt_id=? AND kind='review_cancel_requested' ORDER BY seq DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            detail = json.loads(str(row[0]))
        except json.JSONDecodeError as exc:
            raise StoreConflict("review cancellation request event is malformed") from exc
        if (
            not isinstance(detail, dict)
            or not isinstance(detail.get("task_id"), str)
            or not isinstance(detail.get("reason"), str)
        ):
            raise StoreConflict("review cancellation request event is malformed")
        return cast(str, detail["task_id"]), cast(str, detail["reason"])

    def collect_review_evidence(
        self, attempt_id: str, record: ReviewEvidence, host_payload: str
    ) -> Attempt:
        """Atomically insert host evidence and apply the verified transition."""
        if not isinstance(record, ReviewEvidence):
            raise TypeError("review collection requires ReviewEvidence")
        try:
            host_object = json.loads(host_payload)
        except json.JSONDecodeError as exc:
            raise StoreConflict("review host evidence is not JSON") from exc
        if not isinstance(host_object, dict):
            raise StoreConflict("review host evidence must be an object")
        if (
            host_object.get("bound_commit") != record.commit
            or host_object.get("bound_spec_sha256") != record.spec_sha256
            or host_object.get("verdict") != record.verdict
        ):
            raise StoreConflict("review host evidence binding differs from verdict")
        review_payload = record.to_json()
        attempt = self.get_attempt(attempt_id)
        hypothesis = self.get_hypothesis(attempt.hypothesis_id)
        if record.attempt_id != attempt_id or record.commit != attempt.commit:
            raise StoreConflict("review verdict does not match attempt")
        if record.spec_sha256 != hypothesis.spec_sha256:
            raise StoreConflict("review verdict does not match frozen hypothesis")
        host_digest = _digest(host_payload)
        replay = False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if current_row is None:
                raise ValueError(f"unknown attempt: {attempt_id}")
            current = self._attempt_from_row(current_row)
            old_host = conn.execute(
                "SELECT payload_json,payload_sha256 FROM attempt_evidence WHERE attempt_id=? AND kind='review_host_evidence'",
                (attempt_id,),
            ).fetchone()
            old_review = conn.execute(
                "SELECT payload_json,payload_sha256 FROM attempt_evidence WHERE attempt_id=? AND kind='review'",
                (attempt_id,),
            ).fetchone()
            if old_host is not None:
                if (
                    _digest(str(old_host[0])) != str(old_host[1])
                    or str(old_host[0]) != host_payload
                ):
                    raise StoreConflict("review host evidence differs from stored payload")
                if old_review is not None and (
                    _digest(str(old_review[0])) != str(old_review[1])
                    or str(old_review[0]) != review_payload
                ):
                    raise StoreConflict("review payload differs from stored payload")
                if current.state in {AttemptState.REVIEW_PASSED, AttemptState.REVIEW_FAILED}:
                    conn.commit()
                    replay = True
            if not replay:
                updated = submit_review(current, record, hypothesis.spec_sha256, now_utc())
                updated_payload = updated.to_json()
                if old_host is None:
                    conn.execute(
                        "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
                        (attempt_id, "review_host_evidence", host_payload, host_digest),
                    )
                if old_review is None:
                    conn.execute(
                        "INSERT INTO attempt_evidence VALUES(?,?,?,?)",
                        (attempt_id, "review", review_payload, _digest(review_payload)),
                    )
                conn.execute(
                    "UPDATE attempts SET state=?,review_verdict=?,review_commit=?,review_spec_sha256=?,reported_reviewer_model=?,reported_reviewer_actual_model=?,updated_at=?,payload_json=?,payload_sha256=? WHERE attempt_id=?",
                    (
                        updated.state.value,
                        updated.review_verdict,
                        updated.review_commit,
                        updated.review_spec_sha256,
                        updated.reported_reviewer_model,
                        updated.reported_reviewer_actual_model,
                        updated.updated_at,
                        updated_payload,
                        _digest(updated_payload),
                        attempt_id,
                    ),
                )
                self._event(
                    conn,
                    attempt.hypothesis_id,
                    attempt_id,
                    "review_collected",
                    {"verdict": record.verdict},
                    "driver",
                )
                conn.commit()
        if replay:
            self._repair_evidence_projection(attempt, "review_host_evidence", host_payload)
            self._repair_evidence_projection(attempt, "review", review_payload)
            return current
        self._projection(
            self.root
            / "hypotheses"
            / attempt.hypothesis_id
            / "attempts"
            / attempt_id
            / "review_host_evidence.json",
            host_payload,
        )
        self._projection(
            self.root
            / "hypotheses"
            / attempt.hypothesis_id
            / "attempts"
            / attempt_id
            / "review.json",
            review_payload,
        )
        return updated

    def submit_review(self, attempt_id: str, record: ReviewEvidence) -> Attempt:
        """Reject the old self-report path; collection must include host evidence."""
        del attempt_id, record
        raise StoreConflict("review-submit was removed; use review-collect")

    def set_state(
        self, attempt: Attempt, *, event: str, actor: str = "driver", detail: object | None = None
    ) -> Attempt:
        payload = attempt.to_json()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE attempts SET state=?,run_job_id=?,run_outcome=?,decision=?,decision_reason=?,updated_at=?,payload_json=?,payload_sha256=? WHERE attempt_id=?",
                (
                    attempt.state.value,
                    attempt.run_job_id,
                    attempt.run_outcome,
                    attempt.decision,
                    attempt.decision_reason,
                    attempt.updated_at,
                    payload,
                    _digest(payload),
                    attempt.attempt_id,
                ),
            )
            self._event(
                conn,
                attempt.hypothesis_id,
                attempt.attempt_id,
                event,
                detail or {"state": attempt.state.value},
                actor,
            )
            conn.commit()
        return attempt

    def close_attempt(self, attempt_id: str, decision: AttemptDecision, reason: str) -> Attempt:
        attempt = self.get_attempt(attempt_id)
        if attempt.state == AttemptState.IMPLEMENTED and decision == AttemptDecision.RETRY:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown attempt: {attempt_id}")
                current = self._attempt_from_row(row)
                if current.state != AttemptState.IMPLEMENTED:
                    raise StoreConflict("attempt is no longer implemented")
                if current.run_job_id is not None:
                    raise StoreConflict("implemented attempt already has a run binding")
                if (
                    conn.execute(
                        "SELECT 1 FROM jobs WHERE attempt_id=? LIMIT 1", (attempt_id,)
                    ).fetchone()
                    is not None
                ):
                    raise StoreConflict("implemented attempt already has a job row")
                if (
                    conn.execute(
                        "SELECT 1 FROM attempt_evidence "
                        "WHERE attempt_id=? AND (kind='review' OR kind LIKE 'review_%') LIMIT 1",
                        (attempt_id,),
                    ).fetchone()
                    is not None
                ):
                    raise StoreConflict("implemented attempt already has review evidence")
                if (
                    conn.execute(
                        "SELECT 1 FROM events "
                        "WHERE attempt_id=? AND (kind='review' OR kind LIKE 'review_%') LIMIT 1",
                        (attempt_id,),
                    ).fetchone()
                    is not None
                ):
                    raise StoreConflict("implemented attempt already has review lifecycle activity")
                updated = machine_close_attempt(current, decision, reason, now_utc())
                payload = updated.to_json()
                changed = conn.execute(
                    "UPDATE attempts SET state=?,run_job_id=?,run_outcome=?,decision=?,"
                    "decision_reason=?,updated_at=?,payload_json=?,payload_sha256=? "
                    "WHERE attempt_id=? AND state=?",
                    (
                        updated.state.value,
                        updated.run_job_id,
                        updated.run_outcome,
                        updated.decision,
                        updated.decision_reason,
                        updated.updated_at,
                        payload,
                        _digest(payload),
                        attempt_id,
                        AttemptState.IMPLEMENTED.value,
                    ),
                ).rowcount
                if changed != 1:
                    raise StoreConflict("attempt changed before retry closure")
                self._event(
                    conn,
                    current.hypothesis_id,
                    attempt_id,
                    "attempt_closed",
                    {"decision": decision.value, "reason": reason},
                    "astra",
                )
                conn.commit()
            return updated
        updated = machine_close_attempt(attempt, decision, reason, now_utc())
        if decision == AttemptDecision.PAUSE:
            payload = updated.to_json()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE attempts SET state=?,decision=?,decision_reason=?,updated_at=?,payload_json=?,payload_sha256=? WHERE attempt_id=?",
                    (
                        updated.state.value,
                        updated.decision,
                        updated.decision_reason,
                        updated.updated_at,
                        payload,
                        _digest(payload),
                        attempt_id,
                    ),
                )
                self._event(
                    conn,
                    attempt.hypothesis_id,
                    attempt_id,
                    "attempt_closed",
                    {"decision": decision.value, "reason": reason},
                    "astra",
                )
                conn.execute("UPDATE campaign SET status='PAUSED' WHERE singleton=1")
                self._event(
                    conn,
                    attempt.hypothesis_id,
                    None,
                    "campaign_paused",
                    {"reason": reason},
                    "operator",
                )
                conn.commit()
            return updated
        result = self.set_state(
            updated,
            event="attempt_closed",
            actor="astra",
            detail={"decision": decision.value, "reason": reason},
        )
        return result

    def pause(self, reason: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE campaign SET status='PAUSED' WHERE singleton=1")
            row = conn.execute(
                "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
            ).fetchone()
            self._event(
                conn,
                str(row[0]) if row else "H0001",
                None,
                "campaign_paused",
                {"reason": reason},
                "operator",
            )
            conn.commit()

    def pause_for_failure(self, reason: str) -> None:
        """Persist a fatal owner failure and pause the campaign atomically."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE campaign SET status='PAUSED' WHERE singleton=1")
            row = conn.execute(
                "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
            ).fetchone()
            hypothesis_id = str(row[0]) if row else "H0001"
            self._event(
                conn,
                hypothesis_id,
                None,
                "serve_failed",
                {"reason": reason},
                "driver",
            )
            self._event(
                conn,
                hypothesis_id,
                None,
                "campaign_paused",
                {"reason": reason, "fatal": True},
                "driver",
            )
            conn.commit()

    def resume(self, reason: str) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE campaign SET status='ACTIVE',resume_seq=resume_seq+1 WHERE singleton=1"
            )
            row = conn.execute("SELECT resume_seq FROM campaign WHERE singleton=1").fetchone()
            row_h = conn.execute(
                "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
            ).fetchone()
            self._event(
                conn,
                str(row_h[0]) if row_h else "H0001",
                None,
                "campaign_resumed",
                {"reason": reason},
                "operator",
            )
            conn.commit()
        assert row is not None
        return int(row[0])

    def repair_projections(self) -> int:
        repaired = 0
        for spec in self.hypotheses():
            if self._projection(
                self.root / "hypotheses" / spec.hypothesis_id / "spec.json", spec.spec_json
            ):
                repaired += 1
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._event(
                        conn,
                        spec.hypothesis_id,
                        None,
                        "projection_repaired",
                        {"path": str(self.root / "hypotheses" / spec.hypothesis_id / "spec.json")},
                        "driver",
                    )
                    conn.commit()
            with self._connect() as conn:
                hypothesis_evidence = conn.execute(
                    "SELECT kind,payload_json,payload_sha256 FROM hypothesis_evidence WHERE hypothesis_id=?",
                    (spec.hypothesis_id,),
                ).fetchall()
            for row in hypothesis_evidence:
                payload = str(row["payload_json"])
                if _digest(payload) != str(row["payload_sha256"]):
                    raise StoreConflict(f"{row['kind']} evidence payload digest mismatch")
                if row["kind"] == "evaluation_spec_set":
                    path = (
                        self.root / "hypotheses" / spec.hypothesis_id / "evaluation-spec-set.json"
                    )
                else:
                    path = self.root / "hypotheses" / spec.hypothesis_id / f"{row['kind']}.json"
                if self._projection(path, payload):
                    repaired += 1
                    with self._connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        self._event(
                            conn,
                            spec.hypothesis_id,
                            None,
                            "projection_repaired",
                            {"path": str(path)},
                            "driver",
                        )
                        conn.commit()
                if row["kind"] == "evaluation_spec_set":
                    spec_set = EvaluationSpecSet.from_json(payload)
                    for entry in spec_set.specs:
                        source = Path(entry.path)
                        if (
                            not source.is_file()
                            or source.is_symlink()
                            or sha256_file(source) != entry.sha256
                        ):
                            raise StoreConflict(
                                f"evaluation spec evidence file changed: {entry.spec_id}"
                            )
                        spec_path = (
                            self.root
                            / "hypotheses"
                            / spec.hypothesis_id
                            / "evaluation-specs"
                            / f"{entry.spec_id}.json"
                        )
                        if self._projection(spec_path, source.read_text(encoding="utf-8")):
                            repaired += 1
            for attempt in self.attempts_for(spec.hypothesis_id):
                with self._connect() as conn:
                    evidence = conn.execute(
                        "SELECT kind,payload_json,payload_sha256 FROM attempt_evidence WHERE attempt_id=?",
                        (attempt.attempt_id,),
                    ).fetchall()
                for row in evidence:
                    if _digest(str(row["payload_json"])) != str(row["payload_sha256"]):
                        raise StoreConflict(f"{row['kind']} evidence payload digest mismatch")
                    path = (
                        self.root
                        / "hypotheses"
                        / spec.hypothesis_id
                        / "attempts"
                        / attempt.attempt_id
                        / f"{row['kind']}.json"
                    )
                    if self._projection(path, str(row["payload_json"])):
                        repaired += 1
                        with self._connect() as conn:
                            conn.execute("BEGIN IMMEDIATE")
                            self._event(
                                conn,
                                spec.hypothesis_id,
                                attempt.attempt_id,
                                "projection_repaired",
                                {"path": str(path)},
                                "driver",
                            )
                            conn.commit()
        return repaired

    def _repair_evidence_projection(self, attempt: Attempt, kind: str, payload: str) -> None:
        path = (
            self.root
            / "hypotheses"
            / attempt.hypothesis_id
            / "attempts"
            / attempt.attempt_id
            / f"{kind}.json"
        )
        if self._projection(path, payload):
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._event(
                    conn,
                    attempt.hypothesis_id,
                    attempt.attempt_id,
                    "projection_repaired",
                    {"path": str(path)},
                    "driver",
                )
                conn.commit()

    def reserve_wake(
        self, pending_key: str, attempt_id: str | None, state: str, resume_seq: int
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT 1 FROM wake_deliveries WHERE pending_key=?", (pending_key,)
            ).fetchone()
            if row is not None:
                return False
            conn.execute(
                "INSERT INTO wake_deliveries(pending_key,attempt_id,state,resume_seq,sent_at,run_id) VALUES(?,?,?,?,?,?)",
                (pending_key, attempt_id, state, resume_seq, now_utc(), "PENDING"),
            )
            attempt_row = (
                conn.execute(
                    "SELECT hypothesis_id FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if attempt_id
                else None
            )
            first = conn.execute(
                "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
            ).fetchone()
            self._event(
                conn,
                str(attempt_row[0]) if attempt_row else (str(first[0]) if first else "H0001"),
                attempt_id,
                "wake_reserved",
                {"pending_key": pending_key},
                "driver",
            )
            conn.commit()
        return True

    def wake_row(self, pending_key: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    "SELECT * FROM wake_deliveries WHERE pending_key=?", (pending_key,)
                ).fetchone(),
            )

    def complete_wake(self, pending_key: str, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE wake_deliveries SET run_id=?,sent_at=? WHERE pending_key=?",
                (run_id, now_utc(), pending_key),
            )
            row = conn.execute(
                "SELECT attempt_id FROM wake_deliveries WHERE pending_key=?", (pending_key,)
            ).fetchone()
            attempt_row = (
                conn.execute(
                    "SELECT hypothesis_id FROM attempts WHERE attempt_id=?", (row[0],)
                ).fetchone()
                if row and row[0]
                else None
            )
            first = conn.execute(
                "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
            ).fetchone()
            self._event(
                conn,
                str(attempt_row[0]) if attempt_row else (str(first[0]) if first else "H0001"),
                row[0] if row else None,
                "wake_delivered",
                {"pending_key": pending_key, "run_id": run_id},
                "driver",
            )
            conn.commit()

    def release_wake(self, pending_key: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempt_id FROM wake_deliveries WHERE pending_key=?", (pending_key,)
            ).fetchone()
            conn.execute(
                "DELETE FROM wake_deliveries WHERE pending_key=? AND run_id='PENDING'",
                (pending_key,),
            )
            attempt_row = (
                conn.execute(
                    "SELECT hypothesis_id FROM attempts WHERE attempt_id=?", (row[0],)
                ).fetchone()
                if row and row[0]
                else None
            )
            first = conn.execute(
                "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
            ).fetchone()
            self._event(
                conn,
                str(attempt_row[0]) if attempt_row else (str(first[0]) if first else "H0001"),
                row[0] if row else None,
                "wake_delivery_released",
                {"pending_key": pending_key},
                "driver",
            )
            conn.commit()

    def update_wake_status(self, pending_key: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT attempt_id,turn_status FROM wake_deliveries WHERE pending_key=?",
                (pending_key,),
            ).fetchone()
            previous_status = row["turn_status"] if row is not None else None
            conn.execute(
                "UPDATE wake_deliveries SET turn_status=?,turn_checked_at=? WHERE pending_key=?",
                (status, now_utc(), pending_key),
            )
            if row is not None and status == "error" and previous_status != "error":
                attempt_row = (
                    conn.execute(
                        "SELECT hypothesis_id FROM attempts WHERE attempt_id=?",
                        (row["attempt_id"],),
                    ).fetchone()
                    if row["attempt_id"]
                    else None
                )
                first = conn.execute(
                    "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
                ).fetchone()
                hyp = str(attempt_row[0]) if attempt_row else (str(first[0]) if first else "H0001")
                self._event(
                    conn,
                    hyp,
                    row["attempt_id"],
                    "owner_turn_failed",
                    {"status": status},
                    "driver",
                )
            elif row is not None and status != previous_status:
                row = conn.execute(
                    "SELECT attempt_id FROM wake_deliveries WHERE pending_key=?", (pending_key,)
                ).fetchone()
                first = conn.execute(
                    "SELECT hypothesis_id FROM hypotheses ORDER BY hypothesis_id LIMIT 1"
                ).fetchone()
                self._event(
                    conn,
                    str(first[0]) if first else "H0001",
                    row[0] if row else None,
                    "owner_turn_status",
                    {"status": status},
                    "driver",
                )
            conn.commit()

    def wake_rows(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM wake_deliveries ORDER BY sent_at").fetchall()

    def owner_lock_held(self) -> bool:
        return self._lock_file is not None

    def queued_jobs(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE state='QUEUED' ORDER BY rowid").fetchall()
        for row in rows:
            if _digest(str(row["payload_json"])) != str(row["payload_sha256"]):
                raise StoreConflict("queued job payload digest mismatch")
        return list(rows)

    def running_job_rows(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT jobs.* FROM jobs JOIN attempts ON attempts.attempt_id=jobs.attempt_id "
                "WHERE attempts.state='RUNNING' ORDER BY jobs.rowid"
            ).fetchall()
        for row in rows:
            if _digest(str(row["payload_json"])) != str(row["payload_sha256"]):
                raise StoreConflict("running job payload digest mismatch")
        return list(rows)

    def queue_run_request(
        self,
        attempt_id: str,
        job_id: str,
        run_dir: Path,
        timeout_seconds: int | float,
        max_rss_mb: int,
        run_plan: RunPlan | None = None,
    ) -> Attempt:
        """Persist one validated run request without starting a process."""
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be a non-empty string")
        validate_queue_limits(timeout_seconds, max_rss_mb)
        attempt = self.get_attempt(attempt_id)
        hypothesis = self.get_hypothesis(attempt.hypothesis_id)
        expected_run_dir = (
            self.root / "hypotheses" / hypothesis.hypothesis_id / "attempts" / attempt_id / "run"
        ).resolve()
        if run_dir.resolve() != expected_run_dir:
            raise StoreConflict("run directory is not the canonical attempt directory")
        implementation = ImplementationRecord.from_json(self.evidence(attempt_id, "implementation"))
        if run_plan is None:
            raise StoreConflict("immutable run plan evidence is required")
        try:
            stored_plan = RunPlan.from_json(self.evidence(attempt_id, "run_plan"))
        except (StoreConflict, ValueError) as exc:
            raise StoreConflict("stored immutable run plan is malformed") from exc
        if run_plan.to_json() != stored_plan.to_json():
            raise StoreConflict("supplied run plan differs from stored run plan")
        if hypothesis.state != HypothesisState.FROZEN:
            raise StoreConflict("hypothesis must be frozen before queueing a reviewed plan")
        try:
            host_review = json.loads(self.evidence(attempt_id, "review_host_evidence"))
        except (ValueError, json.JSONDecodeError) as exc:
            raise StoreConflict("verified host review evidence is required") from exc
        config = self.config()
        if attempt.commit is None or attempt.implementation_sha256 is None:
            raise StoreConflict("implementation binding is incomplete")
        if implementation.commit != attempt.commit:
            raise StoreConflict("implementation commit changed")
        if (
            attempt.review_verdict != "PASS"
            or not isinstance(host_review, dict)
            or host_review.get("verdict") != "PASS"
            or host_review.get("bound_commit") != attempt.commit
            or host_review.get("bound_spec_sha256") != hypothesis.spec_sha256
            or (host_review.get("bound_run_plan_sha256") != _digest(stored_plan.to_json()))
        ):
            raise StoreConflict("verified host review evidence is required")
        artifact_paths = {
            "spec": self.root / "hypotheses" / hypothesis.hypothesis_id / "spec.json",
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
        evaluation_spec_paths: dict[str, str] = {}
        evaluation_spec_digests: dict[str, str] = {}
        spec_set = self.evaluation_spec_set(hypothesis.hypothesis_id)
        self._validate_owned_spec_set(hypothesis, spec_set)
        set_digest = _digest(spec_set.to_json())
        if stored_plan.evaluation_spec_set_sha256 != set_digest:
            raise StoreConflict("run plan spec-set digest changed")
        entries = {entry.spec_id: entry for entry in spec_set.specs}
        for scenario in stored_plan.scenarios:
            entry = entries.get(scenario.spec_id)
            if entry is None or entry.sha256 != scenario.evaluation_spec_sha256:
                raise StoreConflict("run plan references an unbound evaluation spec")
            evaluation_spec_paths[scenario.spec_id] = entry.path
            evaluation_spec_digests[scenario.spec_id] = entry.sha256
        if stored_plan.primary_scenario_id not in {s.scenario_id for s in stored_plan.scenarios}:
            raise StoreConflict("run plan primary scenario is missing")
        if (
            stored_plan.scenario_timeout_seconds
            * (len(stored_plan.scenarios) + len(spec_set.specs))
            + stored_plan.analysis_timeout_seconds
            > timeout_seconds
        ):
            raise StoreConflict("run plan stage timeouts exceed the queued job timeout")
        payload: dict[str, object] = {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "run_dir": str(run_dir.resolve()),
            "state": "QUEUED",
            "requested_at": now_utc(),
            "timeout_seconds": float(timeout_seconds),
            "max_rss_mb": max_rss_mb,
            "worktree": attempt.worktree_path,
            "targets_argv": list(implementation.targets_argv),
            "expected_commit": attempt.commit,
            "implementation_sha256": attempt.implementation_sha256,
            "review_commit": attempt.review_commit,
            "review_spec_sha256": attempt.review_spec_sha256,
            "artifact_paths": {key: str(value.resolve()) for key, value in artifact_paths.items()},
            "artifact_digests": artifact_digests,
            "shared_python": str(config["shared_python"]),
            "shared_python_sha256": str(config["shared_python_sha256"]),
            "evaluator_source": str(config["evaluator_source"]),
            "evaluator_source_sha256": str(config["evaluator_source_sha256"]),
            "evaluator_sha256": str(config["evaluator_sha256"]),
            "snapshot_dir": str(config["snapshot_dir"]),
            "snapshot_sha256": str(config["snapshot_sha256"]),
            "resolved_python": str(config["shared_python_resolved"]),
            "shared_python_resolved": str(config["shared_python_resolved"]),
            "pyvenv_cfg": str(config["pyvenv_cfg"]),
            "pyvenv_sha256": str(config["pyvenv_sha256"]),
            "distribution_dir": str(config["distribution_dir"]),
            "universe": str(config["universe"]),
            "universe_sha256": str(config["universe_sha256"]),
            "dividends_path": hypothesis.dividends_path,
            "dividends_sha256": hypothesis.dividends_sha256,
        }
        payload.update(
            {
                "run_plan": json.loads(stored_plan.to_json()),
                "run_plan_sha256": _digest(stored_plan.to_json()),
                "evaluation_spec_set_sha256": stored_plan.evaluation_spec_set_sha256,
                "evaluation_spec_paths": evaluation_spec_paths,
                "evaluation_spec_digests": evaluation_spec_digests,
            }
        )
        # A retried request with the same caller-selected id is idempotent.  The
        # timestamp is observational and therefore excluded from this replay
        # comparison; all execution-affecting fields must still match.
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT state,payload_json,payload_sha256,attempt_id FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if existing is not None:
            if _digest(str(existing["payload_json"])) != str(existing["payload_sha256"]):
                raise StoreConflict("replayed job payload digest mismatch")
            if str(existing["attempt_id"]) != attempt_id:
                raise StoreConflict("job id is already used by another attempt")
            old_payload = json.loads(str(existing["payload_json"]))
            comparable = {
                key: value for key, value in payload.items() if key not in {"requested_at", "state"}
            }
            old_comparable = {key: old_payload.get(key) for key in comparable}
            if old_comparable == comparable:
                return self.get_attempt(attempt_id)
            raise StoreConflict("replayed job id has a different payload")
        try:
            queued = replace(queue_run(attempt, now_utc()), run_job_id=job_id)
        except IllegalTransition as exc:
            raise StoreConflict("attempt already has a queued or running job") from exc
        attempt_payload = queued.to_json()
        job_text = to_json(payload)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT state,run_job_id FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if current is None or current["state"] != AttemptState.REVIEW_PASSED.value:
                raise StoreConflict("attempt is no longer review-passed")
            existing = conn.execute(
                "SELECT state,payload_json FROM jobs WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            if existing is not None and str(existing["state"]) != "ADMISSION_REFUSED":
                raise StoreConflict("attempt already has a queued or running job")
            conn.execute(
                "UPDATE attempts SET state=?,run_job_id=?,updated_at=?,payload_json=?,payload_sha256=? WHERE attempt_id=? AND state=?",
                (
                    queued.state.value,
                    queued.run_job_id,
                    queued.updated_at,
                    attempt_payload,
                    _digest(attempt_payload),
                    attempt_id,
                    AttemptState.REVIEW_PASSED.value,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise StoreConflict("attempt changed while queueing run")
            conn.execute(
                "INSERT INTO jobs(job_id,attempt_id,state,payload_json,payload_sha256) VALUES(?,?,?,?,?)",
                (job_id, attempt_id, "QUEUED", job_text, _digest(job_text)),
            )
            self._event(
                conn, attempt.hypothesis_id, attempt_id, "run_queued", {"job_id": job_id}, "driver"
            )
            conn.commit()
        return queued

    def release_queued_run(self, attempt_id: str, job_id: str, reason: str) -> Attempt:
        """Return a refused queued run to its review-passed owner-pending state."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("admission release reason must be a non-empty string")
        attempt = self.get_attempt(attempt_id)
        if attempt.state != AttemptState.RUN_QUEUED or attempt.run_job_id != job_id:
            raise StoreConflict("queued attempt is no longer pending")
        updated = replace(
            attempt,
            state=AttemptState.REVIEW_PASSED,
            run_job_id=None,
            run_outcome=None,
            updated_at=now_utc(),
        )
        payload = updated.to_json()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE attempts SET state=?,run_job_id=NULL,run_outcome=NULL,updated_at=?,payload_json=?,payload_sha256=? WHERE attempt_id=? AND state=? AND run_job_id=?",
                (
                    updated.state.value,
                    updated.updated_at,
                    payload,
                    _digest(payload),
                    attempt_id,
                    AttemptState.RUN_QUEUED.value,
                    job_id,
                ),
            ).rowcount
            if changed != 1:
                raise StoreConflict("queued attempt changed before admission release")
            self._set_job_state(conn, job_id, "ADMISSION_REFUSED")
            self._event(
                conn,
                attempt.hypothesis_id,
                attempt_id,
                "admission_dispatch_released",
                {"job_id": job_id, "reason": reason},
                "driver",
            )
            conn.commit()
        return updated

    def claim_queued_job(self, attempt_id: str, job_id: str) -> Attempt:
        """Atomically claim a queued request for one host launch."""
        attempt = self.get_attempt(attempt_id)
        if attempt.state != AttemptState.RUN_QUEUED or attempt.run_job_id != job_id:
            raise StoreConflict("queued attempt changed before launch claim")
        running = start_run(attempt, job_id, now_utc())
        payload = running.to_json()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE attempts SET state=?,run_job_id=?,updated_at=?,payload_json=?,payload_sha256=? "
                "WHERE attempt_id=? AND state=? AND run_job_id=?",
                (
                    running.state.value,
                    running.run_job_id,
                    running.updated_at,
                    payload,
                    _digest(payload),
                    attempt_id,
                    AttemptState.RUN_QUEUED.value,
                    job_id,
                ),
            ).rowcount
            if changed != 1:
                raise StoreConflict("queued attempt changed before launch claim")
            job_payload = json.loads(self._job_payload(conn, job_id))
            job_payload["state"] = "LAUNCH_RESERVED"
            job_text = to_json(job_payload)
            changed = conn.execute(
                "UPDATE jobs SET state=?,payload_json=?,payload_sha256=? WHERE job_id=? AND attempt_id=? AND state=?",
                ("LAUNCH_RESERVED", job_text, _digest(job_text), job_id, attempt_id, "QUEUED"),
            ).rowcount
            if changed != 1:
                raise StoreConflict("queued job changed before launch claim")
            self._event(
                conn,
                attempt.hypothesis_id,
                attempt_id,
                "job_launch_reserved",
                {"job_id": job_id},
                "driver",
            )
            conn.commit()
        return running

    @staticmethod
    def _job_payload(conn: sqlite3.Connection, job_id: str) -> str:
        row = conn.execute("SELECT payload_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise StoreConflict(f"unknown job: {job_id}")
        return str(row[0])

    def finalize_queued_run(self, attempt_id: str, outcome: RunOutcome, job_state: str) -> Attempt:
        if outcome.attempt_id != attempt_id:
            raise ValueError("run outcome does not match attempt")
        attempt = self.get_attempt(attempt_id)
        if attempt.state != AttemptState.RUN_QUEUED:
            raise StoreConflict("queued attempt is no longer pending")
        updated = replace(
            attempt,
            state=AttemptState.RUN_FAILED,
            run_outcome=outcome.to_json(),
            updated_at=now_utc(),
        )
        payload = updated.to_json()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE attempts SET state=?,run_outcome=?,updated_at=?,payload_json=?,payload_sha256=? WHERE attempt_id=? AND state=? AND run_job_id=?",
                (
                    updated.state.value,
                    outcome.to_json(),
                    updated.updated_at,
                    payload,
                    _digest(payload),
                    attempt_id,
                    AttemptState.RUN_QUEUED.value,
                    outcome.job_id,
                ),
            ).rowcount
            if changed != 1:
                raise StoreConflict("queued attempt changed before finalization")
            self._set_job_state(conn, outcome.job_id, job_state)
            self._event(
                conn,
                attempt.hypothesis_id,
                attempt_id,
                "run_finished",
                {"status": outcome.status},
                "driver",
            )
            conn.commit()
        return updated

    def _set_job_state(self, conn: sqlite3.Connection, job_id: str, state: str) -> None:
        row = conn.execute("SELECT payload_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise StoreConflict(f"unknown job: {job_id}")
        payload = json.loads(str(row[0]))
        payload["state"] = state
        text = to_json(payload)
        conn.execute(
            "UPDATE jobs SET state=?,payload_json=?,payload_sha256=? WHERE job_id=?",
            (state, text, _digest(text), job_id),
        )

    def save_job(self, job_id: str, attempt_id: str, state: str, payload: object) -> None:
        text = to_json(payload)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO jobs(job_id,attempt_id,state,payload_json,payload_sha256) VALUES(?,?,?,?,?)",
                (job_id, attempt_id, state, text, _digest(text)),
            )
            row = conn.execute(
                "SELECT hypothesis_id FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            self._event(
                conn,
                str(row[0]) if row else "H0001",
                attempt_id,
                "job_saved",
                {"job_id": job_id, "state": state},
                "driver",
            )
            conn.commit()

    def update_job(self, job: object, attempt_id: str, *, event: str = "job_launched") -> None:
        text = to_json(job)
        if not isinstance(job, dict) or not isinstance(job.get("job_id"), str):
            raise ValueError("job payload must include job_id")
        job_id = str(job["job_id"])
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                "UPDATE jobs SET state=?,payload_json=?,payload_sha256=? WHERE job_id=? AND attempt_id=?",
                (str(job.get("state", "LAUNCHED")), text, _digest(text), job_id, attempt_id),
            ).rowcount
            if updated != 1:
                raise StoreConflict(f"unknown reserved job: {job_id}")
            row = conn.execute(
                "SELECT hypothesis_id FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            self._event(
                conn,
                str(row[0]) if row else "H0001",
                attempt_id,
                event,
                {"job_id": job_id},
                "driver",
            )
            conn.commit()

    def job_for(self, attempt_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = cast(
                sqlite3.Row | None,
                conn.execute(
                    "SELECT * FROM jobs WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1",
                    (attempt_id,),
                ).fetchone(),
            )
        if row is not None and _digest(str(row["payload_json"])) != str(row["payload_sha256"]):
            raise StoreConflict("job payload digest mismatch")
        return row

    def evidence(self, attempt_id: str, kind: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json,payload_sha256 FROM attempt_evidence WHERE attempt_id=? AND kind=?",
                (attempt_id, kind),
            ).fetchone()
        if row is None:
            raise ValueError(f"missing {kind} evidence for {attempt_id}")
        payload = str(row["payload_json"])
        if _digest(payload) != str(row["payload_sha256"]):
            raise StoreConflict(f"{kind} evidence payload digest mismatch")
        return payload

    def hypothesis_evidence(self, hypothesis_id: str, kind: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json,payload_sha256 FROM hypothesis_evidence WHERE hypothesis_id=? AND kind=?",
                (hypothesis_id, kind),
            ).fetchone()
        if row is None:
            raise ValueError(f"missing {kind} evidence for {hypothesis_id}")
        payload = str(row["payload_json"])
        if _digest(payload) != str(row["payload_sha256"]):
            raise StoreConflict(f"{kind} evidence payload digest mismatch")
        return payload

    def evaluation_spec_set(self, hypothesis_id: str) -> EvaluationSpecSet:
        return EvaluationSpecSet.from_json(
            self.hypothesis_evidence(hypothesis_id, "evaluation_spec_set")
        )

    def finish_run(self, attempt_id: str, outcome: object) -> Attempt:
        from .contracts import RunOutcome
        from .machine import finish_run as machine_finish_run

        if not isinstance(outcome, RunOutcome):
            raise TypeError("outcome must be RunOutcome")
        attempt = self.get_attempt(attempt_id)
        updated = machine_finish_run(attempt, outcome, now_utc())
        return self.set_state(
            updated,
            event="run_finished",
            detail={
                "exit_code": outcome.exit_code,
                "status": outcome.status,
                "state": updated.state.value,
            },
        )
