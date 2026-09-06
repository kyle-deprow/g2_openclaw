from __future__ import annotations

import hashlib
import json

import pytest
from gateway.research.contracts import (
    Attempt,
    AttemptState,
    Event,
    HypothesisSpec,
    HypothesisState,
    ImplementationRecord,
    ReviewRecord,
    RunOutcome,
)


def test_hypothesis_round_trip_is_strict() -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    value = HypothesisSpec(
        "H0001",
        "title",
        '{"x":1}',
        digest,
        "/panel",
        "/receipt",
        "/eval",
        digest,
        digest,
        digest,
        3,
        "a" * 40,
        "2026-01-01T00:00:00Z",
    )
    assert HypothesisSpec.from_json(value.to_json()) == value
    raw = json.loads(value.to_json())
    raw["unknown"] = True
    with pytest.raises(ValueError):
        HypothesisSpec.from_json(json.dumps(raw))
    assert value.state == HypothesisState.DRAFT


def test_all_wire_records_round_trip_and_validate_utc() -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    attempt = Attempt(
        "H0001-A001",
        "H0001",
        1,
        AttemptState.OPENED,
        "/worktree",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
    )
    implementation = ImplementationRecord(
        attempt.attempt_id,
        "a" * 40,
        ("/usr/bin/python3", "-m", "target"),
        "/evidence",
        "coder",
        "high",
        "standard",
        "2026-01-01T00:00:00Z",
    )
    review = ReviewRecord(
        attempt.attempt_id,
        "a" * 40,
        digest,
        "PASS",
        (),
        "reviewer",
        "actual",
        "session",
        "2026-01-01T00:00:00Z",
    )
    outcome = RunOutcome(
        attempt.attempt_id,
        "job-1",
        0,
        True,
        False,
        True,
        "accepted",
        "driver",
        "/run/result.json",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:01Z",
    )
    event = Event(1, "2026-01-01T00:00:00Z", "H0001", None, "kind", "{}", "driver")
    records: tuple[Attempt | ImplementationRecord | ReviewRecord | RunOutcome | Event, ...] = (
        attempt,
        implementation,
        review,
        outcome,
        event,
    )
    for record in records:
        assert type(record).from_json(record.to_json()) == record
    with pytest.raises(ValueError):
        Event(1, "2026-01-01T00:00:00+01:00", "H0001", None, "kind", "{}", "driver")
