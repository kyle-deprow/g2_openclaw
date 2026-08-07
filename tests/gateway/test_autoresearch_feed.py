"""Tests for the autoresearch display feed."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

import gateway.autoresearch_feed as feed_module
import pytest
from gateway.autoresearch_checkpoint import SupervisorCheckpoint
from gateway.autoresearch_feed import (
    AutoresearchFeedPublisher,
    AutoresearchSnapshot,
    FeedEntry,
    build_feed_frame,
    build_status_frame,
    read_snapshot,
)
from gateway.protocol import validate_outbound
from gateway.session_history import HistoryEntry
from gateway.task_status import TaskInfo
from websockets.exceptions import ConnectionClosed


def _empty_history(
    *, session_key: str, agent_id: str, limit: int, base_path: Path | None
) -> list[HistoryEntry]:
    del session_key, agent_id, limit, base_path
    return []


def _empty_task_status(session_key: str, agent_id: str = "claw") -> TaskInfo | None:
    del session_key, agent_id
    return None


def _patch_empty_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed_module, "read_history", _empty_history)
    monkeypatch.setattr(feed_module, "read_task_status", _empty_task_status)


def _write_state(path: Path, values: dict[str, object]) -> None:
    path.write_text(json.dumps(values), encoding="utf-8")


def _snapshot(
    *,
    phase: str = "verification",
    iteration: int = 3,
    header_ok: bool = True,
    feed: tuple[FeedEntry, ...] | None = (),
    supervisor_outcome: str | None = None,
    supervisor_detail: str | None = None,
    last_cycle_at_ms: int | None = None,
    task_headline: str | None = None,
) -> AutoresearchSnapshot:
    return AutoresearchSnapshot(
        running=True,
        header_ok=header_ok,
        phase=phase,
        iteration=iteration,
        suspended=False,
        campaign_review_required=False,
        supervisor_outcome=supervisor_outcome,
        supervisor_detail=supervisor_detail,
        last_cycle_at_ms=last_cycle_at_ms,
        task_headline=task_headline,
        feed=feed,
    )


def test_read_snapshot_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "quantipy-state.json"
    checkpoint_path = tmp_path / "owner-recovery.json"
    _write_state(
        state_path,
        {
            "phase": "verification",
            "iteration": 7,
            "suspended": True,
            "campaign_review_required": True,
        },
    )
    SupervisorCheckpoint(
        last_cycle_outcome="renudged",
        last_cycle_detail="D" * 200,
        last_cycle_at=123.5,
    ).save(checkpoint_path)

    history = [
        HistoryEntry(role="user", text="question", ts=1),
        HistoryEntry(role="assistant", text="first answer", ts=2),
        HistoryEntry(role="user", text="follow-up", ts=3),
        HistoryEntry(role="assistant", text="latest answer", ts=4),
    ]
    task_info = TaskInfo(status="running", description="Verify the latest result")
    history_kwargs: dict[str, object] = {}
    task_status_kwargs: dict[str, object] = {}

    def fake_read_history(
        *, session_key: str, agent_id: str, limit: int, base_path: Path | None
    ) -> list[HistoryEntry]:
        history_kwargs.update(
            session_key=session_key,
            agent_id=agent_id,
            limit=limit,
            base_path=base_path,
        )
        return history

    def fake_read_task_status(session_key: str, agent_id: str = "claw") -> TaskInfo:
        task_status_kwargs.update(session_key=session_key, agent_id=agent_id)
        return task_info

    monkeypatch.setattr(feed_module, "read_history", fake_read_history)
    monkeypatch.setattr(feed_module, "read_task_status", fake_read_task_status)

    snapshot = read_snapshot(
        state_path=state_path,
        checkpoint_path=checkpoint_path,
        session_key="agent:test:autoresearch",
        agent_id="autoresearch-pm",
        base_path=tmp_path,
        now=123.5,
    )

    assert snapshot.running is True
    assert snapshot.header_ok is True
    assert snapshot.phase == "verification"
    assert snapshot.iteration == 7
    assert snapshot.suspended is True
    assert snapshot.campaign_review_required is True
    assert snapshot.supervisor_outcome == "renudged"
    assert snapshot.supervisor_detail == "D" * 160
    assert snapshot.last_cycle_at_ms == 123500
    assert snapshot.task_headline == "[RUNNING] Verify the latest result"
    assert snapshot.feed == (
        FeedEntry(role="assistant", text="first answer", ts=2),
        FeedEntry(role="assistant", text="latest answer", ts=4),
    )
    assert history_kwargs == {
        "session_key": "agent:test:autoresearch",
        "agent_id": "autoresearch-pm",
        "limit": feed_module.HISTORY_READ_LIMIT,
        "base_path": tmp_path,
    }
    assert task_status_kwargs == {
        "session_key": "agent:test:autoresearch",
        "agent_id": "autoresearch-pm",
    }


def test_read_snapshot_stale_checkpoint_is_not_running(tmp_path: Path) -> None:
    state_path = tmp_path / "quantipy-state.json"
    checkpoint_path = tmp_path / "owner-recovery.json"
    _write_state(state_path, {"phase": "review", "iteration": 9})
    SupervisorCheckpoint(last_cycle_at=100.0).save(checkpoint_path)

    snapshot = read_snapshot(
        state_path=state_path,
        checkpoint_path=checkpoint_path,
        now=100.0 + feed_module.LIVENESS_THRESHOLD_SECONDS + 1.0,
    )

    assert snapshot.running is False
    assert snapshot.header_ok is True
    assert snapshot.phase == "review"
    assert snapshot.iteration == 9


def test_read_snapshot_no_checkpoint_is_not_running(tmp_path: Path) -> None:
    state_path = tmp_path / "quantipy-state.json"
    _write_state(state_path, {"phase": "verification", "iteration": 6})

    snapshot = read_snapshot(
        state_path=state_path,
        checkpoint_path=tmp_path / "missing-checkpoint.json",
        now=100.0,
    )

    assert snapshot.running is False
    assert snapshot.header_ok is True
    assert snapshot.phase == "verification"
    assert snapshot.iteration == 6


def test_read_snapshot_missing_state_degrades_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_empty_sources(monkeypatch)

    snapshot = read_snapshot(
        state_path=tmp_path / "missing-state.json",
        checkpoint_path=tmp_path / "missing-checkpoint.json",
        base_path=tmp_path,
    )

    assert snapshot.running is False
    assert snapshot.header_ok is True
    assert snapshot.phase == "not running"
    assert snapshot.iteration == 0
    assert snapshot.suspended is False
    assert snapshot.campaign_review_required is False


def test_read_snapshot_corrupt_state_degrades_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_empty_sources(monkeypatch)
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text("{not-json", encoding="utf-8")

    snapshot = read_snapshot(
        state_path=state_path,
        checkpoint_path=tmp_path / "missing-checkpoint.json",
        base_path=tmp_path,
    )

    assert snapshot.running is False
    assert snapshot.header_ok is False
    assert snapshot.phase == "not running"
    assert snapshot.iteration == 0
    assert snapshot.suspended is False
    assert snapshot.campaign_review_required is False


def test_read_snapshot_corrupt_checkpoint_keeps_other_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_empty_sources(monkeypatch)
    state_path = tmp_path / "quantipy-state.json"
    checkpoint_path = tmp_path / "owner-recovery.json"
    _write_state(
        state_path,
        {
            "phase": "review",
            "iteration": 4,
            "suspended": False,
            "campaign_review_required": False,
        },
    )
    checkpoint_path.write_text("{not-json", encoding="utf-8")

    snapshot = read_snapshot(
        state_path=state_path,
        checkpoint_path=checkpoint_path,
        base_path=tmp_path,
    )

    assert snapshot.running is False
    assert snapshot.header_ok is False
    assert snapshot.phase == "review"
    assert snapshot.iteration == 4
    assert snapshot.supervisor_outcome is None
    assert snapshot.supervisor_detail is None
    assert snapshot.last_cycle_at_ms is None


def test_read_snapshot_wrong_typed_state_values_use_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_empty_sources(monkeypatch)
    state_path = tmp_path / "quantipy-state.json"
    _write_state(
        state_path,
        {
            "phase": 123,
            "iteration": "3",
            "suspended": "yes",
            "campaign_review_required": 1,
        },
    )

    snapshot = read_snapshot(
        state_path=state_path,
        checkpoint_path=tmp_path / "missing-checkpoint.json",
        base_path=tmp_path,
    )

    assert snapshot.running is False
    assert snapshot.header_ok is True
    assert snapshot.phase == "unknown"
    assert snapshot.iteration == 0
    assert snapshot.suspended is False
    assert snapshot.campaign_review_required is False


def test_read_snapshot_truncates_and_limits_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_empty_sources(monkeypatch)
    state_path = tmp_path / "quantipy-state.json"
    _write_state(state_path, {})
    history = [HistoryEntry(role="assistant", text=f"{i}: " + "x" * 600, ts=i) for i in range(15)]

    def fake_read_history(
        *, session_key: str, agent_id: str, limit: int, base_path: Path | None
    ) -> list[HistoryEntry]:
        del session_key, agent_id, limit, base_path
        return history

    monkeypatch.setattr(feed_module, "read_history", fake_read_history)

    snapshot = read_snapshot(
        state_path=state_path,
        checkpoint_path=tmp_path / "missing-checkpoint.json",
        base_path=tmp_path,
    )

    assert snapshot.feed is not None
    assert len(snapshot.feed) == 10
    assert [entry.ts for entry in snapshot.feed] == list(range(5, 15))
    assert snapshot.feed[0].text == "5: " + "x" * 497
    assert all(len(entry.text) == 500 for entry in snapshot.feed)


def test_read_snapshot_history_failure_returns_none_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_empty_sources(monkeypatch)
    state_path = tmp_path / "quantipy-state.json"
    _write_state(state_path, {})

    def failing_read_history(
        *, session_key: str, agent_id: str, limit: int, base_path: Path | None
    ) -> list[HistoryEntry]:
        del session_key, agent_id, limit, base_path
        raise OSError("transient history failure")

    monkeypatch.setattr(feed_module, "read_history", failing_read_history)

    snapshot = read_snapshot(
        state_path=state_path,
        checkpoint_path=tmp_path / "missing-checkpoint.json",
        base_path=tmp_path,
    )

    assert snapshot.feed is None


def test_frame_builders_validate_and_omit_none_optionals() -> None:
    snapshot = _snapshot(feed=(FeedEntry(role="assistant", text="hello", ts=1),))

    status_frame = build_status_frame(snapshot)
    feed_frame = build_feed_frame(snapshot)

    validate_outbound(status_frame)
    validate_outbound(feed_frame)
    assert "supervisorOutcome" not in status_frame
    assert "supervisorDetail" not in status_frame
    assert "lastCycleAt" not in status_frame
    assert "taskHeadline" not in status_frame
    assert feed_frame == {
        "type": "autoresearch_feed",
        "entries": [{"role": "assistant", "text": "hello", "ts": 1}],
    }


def test_status_builder_emits_all_optional_fields_exactly() -> None:
    snapshot = _snapshot(
        supervisor_outcome="healthy",
        supervisor_detail="cycle complete",
        last_cycle_at_ms=123500,
        task_headline="[RUNNING] Verify results",
    )

    frame = build_status_frame(snapshot)

    assert frame == {
        "type": "autoresearch_status",
        "running": True,
        "phase": "verification",
        "iteration": 3,
        "suspended": False,
        "campaignReviewRequired": False,
        "supervisorOutcome": "healthy",
        "supervisorDetail": "cycle complete",
        "lastCycleAt": 123500,
        "taskHeadline": "[RUNNING] Verify results",
    }
    validate_outbound(frame)


async def _wait_for_frame_count(frames: list[dict[str, Any]], count: int) -> None:
    for _ in range(1000):
        if len(frames) >= count:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"expected {count} frames, got {len(frames)}")


async def _recording_send(frames: list[dict[str, Any]], frame: dict[str, Any]) -> None:
    frames.append(frame)


@pytest.mark.asyncio
async def test_publisher_initial_push_and_start_are_idempotent() -> None:
    frames: list[dict[str, Any]] = []
    snapshot = _snapshot()

    async def send(frame: dict[str, Any]) -> None:
        await _recording_send(frames, frame)

    publisher = AutoresearchFeedPublisher(send, poll_interval=0.01, read=lambda: snapshot)
    publisher.start()
    publisher.start()
    await _wait_for_frame_count(frames, 2)
    await publisher.stop()

    assert [frame["type"] for frame in frames] == ["autoresearch_status", "autoresearch_feed"]


@pytest.mark.asyncio
async def test_publisher_identical_snapshot_pushes_nothing_after_initial() -> None:
    frames: list[dict[str, Any]] = []
    snapshot = _snapshot()

    async def send(frame: dict[str, Any]) -> None:
        frames.append(frame)

    publisher = AutoresearchFeedPublisher(send, poll_interval=0.01, read=lambda: snapshot)
    publisher.start()
    await _wait_for_frame_count(frames, 2)
    frames.clear()
    await asyncio.sleep(0.04)
    await publisher.stop()

    assert frames == []


@pytest.mark.asyncio
async def test_publisher_header_only_change_pushes_status() -> None:
    frames: list[dict[str, Any]] = []
    snapshots = iter([_snapshot(), _snapshot(iteration=4)])
    last_snapshot = _snapshot()

    def read() -> AutoresearchSnapshot:
        nonlocal last_snapshot
        with suppress(StopIteration):
            last_snapshot = next(snapshots)
        return last_snapshot

    async def send(frame: dict[str, Any]) -> None:
        frames.append(frame)

    publisher = AutoresearchFeedPublisher(send, poll_interval=0.01, read=read)
    publisher.start()
    await _wait_for_frame_count(frames, 3)
    await publisher.stop()

    assert [frame["type"] for frame in frames] == [
        "autoresearch_status",
        "autoresearch_feed",
        "autoresearch_status",
    ]


@pytest.mark.asyncio
async def test_publisher_feed_only_change_pushes_feed() -> None:
    frames: list[dict[str, Any]] = []
    initial = _snapshot()
    changed = replace(initial, feed=(FeedEntry(role="assistant", text="new", ts=2),))
    snapshots = iter([initial, changed])
    last_snapshot = initial

    def read() -> AutoresearchSnapshot:
        nonlocal last_snapshot
        with suppress(StopIteration):
            last_snapshot = next(snapshots)
        return last_snapshot

    async def send(frame: dict[str, Any]) -> None:
        frames.append(frame)

    publisher = AutoresearchFeedPublisher(send, poll_interval=0.01, read=read)
    publisher.start()
    await _wait_for_frame_count(frames, 3)
    await publisher.stop()

    assert [frame["type"] for frame in frames] == [
        "autoresearch_status",
        "autoresearch_feed",
        "autoresearch_feed",
    ]


@pytest.mark.asyncio
async def test_publisher_skips_corrupt_state_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_empty_sources(monkeypatch)
    state_path = tmp_path / "quantipy-state.json"
    state_path.write_text("{not-json", encoding="utf-8")
    frames: list[dict[str, Any]] = []
    snapshot = read_snapshot(
        state_path=state_path,
        checkpoint_path=tmp_path / "missing-checkpoint.json",
        base_path=tmp_path,
    )
    assert snapshot.header_ok is False

    async def send(frame: dict[str, Any]) -> None:
        frames.append(frame)

    publisher = AutoresearchFeedPublisher(send, poll_interval=0.01, read=lambda: snapshot)
    publisher.start()
    await _wait_for_frame_count(frames, 1)
    await publisher.stop()

    assert [frame["type"] for frame in frames] == ["autoresearch_feed"]


@pytest.mark.asyncio
async def test_publisher_does_not_clobber_good_feed_after_read_failure() -> None:
    frames: list[dict[str, Any]] = []
    old_feed = (FeedEntry(role="assistant", text="old", ts=1),)
    new_feed = (FeedEntry(role="assistant", text="new", ts=2),)
    good_snapshot = _snapshot(feed=old_feed)
    failed_snapshot = replace(good_snapshot, feed=None)
    changed_snapshot = replace(good_snapshot, feed=new_feed)
    snapshots = iter([good_snapshot, failed_snapshot, changed_snapshot])
    last_snapshot = good_snapshot

    def read() -> AutoresearchSnapshot:
        nonlocal last_snapshot
        with suppress(StopIteration):
            last_snapshot = next(snapshots)
        return last_snapshot

    async def send(frame: dict[str, Any]) -> None:
        frames.append(frame)

    publisher = AutoresearchFeedPublisher(send, poll_interval=0.01, read=read)
    publisher.start()
    await _wait_for_frame_count(frames, 3)
    await publisher.stop()

    assert [frame["type"] for frame in frames] == [
        "autoresearch_status",
        "autoresearch_feed",
        "autoresearch_feed",
    ]
    assert frames[-1] == {
        "type": "autoresearch_feed",
        "entries": [{"role": "assistant", "text": "new", "ts": 2}],
    }


@pytest.mark.asyncio
async def test_publisher_connection_closed_ends_task() -> None:
    async def send(_frame: dict[str, Any]) -> None:
        raise ConnectionClosed(None, None)

    publisher = AutoresearchFeedPublisher(send, poll_interval=0.01, read=lambda: _snapshot())
    publisher.start()
    task = publisher._task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)
    await publisher.stop()


@pytest.mark.asyncio
async def test_publisher_read_warning_does_not_kill_loop(caplog: pytest.LogCaptureFixture) -> None:
    frames: list[dict[str, Any]] = []
    calls = 0

    def read() -> AutoresearchSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary read failure")
        return _snapshot()

    async def send(frame: dict[str, Any]) -> None:
        frames.append(frame)

    with caplog.at_level(logging.WARNING, logger=feed_module.logger.name):
        publisher = AutoresearchFeedPublisher(send, poll_interval=0.01, read=read)
        publisher.start()
        await _wait_for_frame_count(frames, 2)
        await publisher.stop()

    assert "temporary read failure" in caplog.text
    assert [frame["type"] for frame in frames] == ["autoresearch_status", "autoresearch_feed"]


@pytest.mark.asyncio
async def test_publisher_stop_is_clean_and_idempotent() -> None:
    frames: list[dict[str, Any]] = []

    async def send(frame: dict[str, Any]) -> None:
        frames.append(frame)

    publisher = AutoresearchFeedPublisher(send, poll_interval=0.01, read=lambda: _snapshot())
    publisher.start()
    await _wait_for_frame_count(frames, 2)
    await publisher.stop()
    await publisher.stop()
    frame_count = len(frames)
    await asyncio.sleep(0.03)

    assert len(frames) == frame_count
