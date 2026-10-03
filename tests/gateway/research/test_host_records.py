"""Read-only generic rollout and native host-record coverage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from gateway.research.host_records import (
    HostRecordError,
    read_exact_rollout,
    read_native_parent_rollout,
)


def _event(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _rollout(
    path: Path,
    *,
    terminal: str | None = "task_complete",
    model: str = "gpt-5.6-sol",
    effort: str = "xhigh",
) -> bytes:
    events: list[str] = [
        _event(
            {
                "type": "session_meta",
                "payload": {
                    "id": "thread-1",
                    "model": model,
                    "reasoning_effort": effort,
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "parent-1",
                                "agent_role": "reviewer",
                                "originator": "owner",
                            }
                        }
                    },
                },
            }
        ),
        _event(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 12, "model_context_window": 1000}
                    },
                },
            }
        ),
    ]
    if terminal is not None:
        events.append(
            _event(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": terminal,
                        "last_agent_message": '{"verdict":"PASS"}',
                    },
                }
            )
        )
    raw = ("\n".join(events) + "\n").encode()
    path.write_bytes(raw)
    return raw


def test_rollout_reads_observed_metadata_usage_and_hash(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    raw = _rollout(path)
    record = read_exact_rollout(path)
    assert record.thread_id == "thread-1"
    assert record.agent_role == "reviewer"
    assert record.parent_thread_id == "parent-1"
    assert record.originator == "owner"
    assert record.model == "gpt-5.6-sol"
    assert record.reasoning_effort == "xhigh"
    assert record.terminal_state == "succeeded"
    assert record.last_agent_message == '{"verdict":"PASS"}'
    assert record.latest_usage is not None
    assert record.latest_usage.total_tokens == 12
    assert record.sha256 == hashlib.sha256(raw).hexdigest()


def test_rollout_pending_and_failed_states_are_distinguishable(tmp_path: Path) -> None:
    pending = tmp_path / "pending.jsonl"
    failed = tmp_path / "failed.jsonl"
    _rollout(pending, terminal=None)
    _rollout(failed, terminal="task_failed")
    assert read_exact_rollout(pending).terminal_state == "pending"
    assert read_exact_rollout(failed).terminal_state == "failed"
    assert read_exact_rollout(failed).last_agent_message is None


def test_rollout_reads_turn_context_and_rejects_disagreement(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _rollout(path, terminal=None)
    with path.open("ab") as stream:
        stream.write(
            (
                _event(
                    {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "xhigh"}}
                )
                + "\n"
            ).encode()
        )
    record = read_exact_rollout(path)
    assert record.model == "gpt-5.6-sol"
    assert record.reasoning_effort == "xhigh"
    with path.open("ab") as stream:
        stream.write(
            (_event({"type": "turn_context", "payload": {"model": "gpt-5.4"}}) + "\n").encode()
        )
    with pytest.raises(HostRecordError, match="values disagree"):
        read_exact_rollout(path)


def test_rollout_rejects_missing_metadata_duplicate_terminal_and_bad_json(tmp_path: Path) -> None:
    missing = tmp_path / "missing.jsonl"
    missing.write_text(_event({"type": "event_msg", "payload": {}}) + "\n")
    with pytest.raises(HostRecordError, match="missing session_meta"):
        read_exact_rollout(missing)
    duplicate = tmp_path / "duplicate.jsonl"
    raw = _rollout(duplicate)
    duplicate.write_bytes(raw + raw.splitlines()[-1] + b"\n")
    with pytest.raises(HostRecordError, match="multiple terminal"):
        read_exact_rollout(duplicate)
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text("not-json\n")
    with pytest.raises(HostRecordError, match="not valid JSON"):
        read_exact_rollout(malformed)


def test_rollout_rejects_relative_symlink_and_oversized_inputs(tmp_path: Path) -> None:
    with pytest.raises(HostRecordError, match="absolute"):
        read_exact_rollout(Path("relative-rollout.jsonl"))
    target = tmp_path / "target.jsonl"
    _rollout(target)
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(HostRecordError):
        read_exact_rollout(link)
    oversized = tmp_path / "oversized.jsonl"
    oversized.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(HostRecordError):
        read_exact_rollout(oversized)


def _spawn_events(
    call_id: str, task_name: str, agent_type: str = "reviewer", *, message: str = "gAAAAABx"
) -> list[str]:
    return [
        _event(
            {
                "timestamp": "2026-01-01T00:00:01.250Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "namespace": "collaboration",
                    "name": "spawn_agent",
                    "call_id": call_id,
                    "arguments": json.dumps(
                        {
                            "task_name": task_name,
                            "agent_type": agent_type,
                            "fork_turns": "none",
                            "message": message,
                        }
                    ),
                },
            }
        ),
        _event(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"task_name": f"/root/{task_name}"}),
                },
            }
        ),
    ]


def _parent(path: Path, *events: str, tail: str = "") -> None:
    header = _event({"type": "session_meta", "payload": {"id": "parent", "source": "vscode"}})
    path.write_text("\n".join([header, *events]) + "\n" + tail)


def test_parent_rollout_binds_exact_task_and_ignores_other_spawns(tmp_path: Path) -> None:
    path = tmp_path / "parent.jsonl"
    _parent(
        path,
        *_spawn_events("call-0", "h0001_earlier", "implementer"),
        *_spawn_events("call-1", "h0001_a001"),
        *_spawn_events("call-2", "h0002_later", "implementer"),
    )
    record = read_native_parent_rollout(path, "h0001_a001")
    assert record.thread_id == "parent"
    assert len(record.spawn_calls) == 1
    call = record.spawn_calls[0]
    assert call.call_id == "call-1"
    assert call.agent_type == "reviewer"
    assert call.task_name == "h0001_a001"
    assert call.result_task_name == "/root/h0001_a001"
    assert call.observed_at_ms == 1_767_225_601_250
    assert not hasattr(call, "prompt_sha256")


def test_parent_rollout_does_not_compare_or_expose_the_ciphertext_message(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parent.jsonl"
    _parent(path, *_spawn_events("call-1", "h0001_a001", message="not-the-reserved-prompt"))
    assert len(read_native_parent_rollout(path, "h0001_a001").spawn_calls) == 1


def test_parent_rollout_rejects_duplicate_spawn_call_ids_for_the_reserved_task(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parent.jsonl"
    calls = _spawn_events("duplicate", "h0001_a001")
    _parent(path, calls[0], calls[0])
    with pytest.raises(HostRecordError, match="duplicate call ids"):
        read_native_parent_rollout(path, "h0001_a001")


def test_parent_rollout_rejects_a_call_id_reused_by_an_unrelated_spawn(tmp_path: Path) -> None:
    path = tmp_path / "parent.jsonl"
    _parent(
        path,
        _spawn_events("shared", "h0001_other", "implementer")[0],
        _spawn_events("shared", "h0001_a001")[0],
    )
    with pytest.raises(HostRecordError, match="duplicate call ids"):
        read_native_parent_rollout(path, "h0001_a001")


def test_parent_rollout_rejects_duplicate_outputs_for_the_reserved_task(tmp_path: Path) -> None:
    path = tmp_path / "parent.jsonl"
    calls = _spawn_events("call-1", "h0001_a001")
    _parent(path, *calls, calls[1])
    with pytest.raises(HostRecordError, match="duplicate outputs"):
        read_native_parent_rollout(path, "h0001_a001")


def test_parent_rollout_rejects_two_calls_with_the_reserved_task_name(tmp_path: Path) -> None:
    path = tmp_path / "parent.jsonl"
    _parent(path, *_spawn_events("call-1", "h0001_a001"), *_spawn_events("call-2", "h0001_a001"))
    with pytest.raises(HostRecordError, match="multiple calls"):
        read_native_parent_rollout(path, "h0001_a001")


def test_parent_rollout_strict_checks_apply_only_to_the_reserved_call(tmp_path: Path) -> None:
    path = tmp_path / "parent.jsonl"
    odd = json.loads(_spawn_events("call-odd", "h0001_other", "implementer")[0])
    odd["payload"]["arguments"] = json.dumps({"task_name": "h0001_other", "extra": True})
    _parent(path, _event(odd), *_spawn_events("call-1", "h0001_a001"))
    assert len(read_native_parent_rollout(path, "h0001_a001").spawn_calls) == 1
    wrong_keys = json.loads(_spawn_events("call-1", "h0001_a001")[0])
    wrong_keys["payload"]["arguments"] = json.dumps({"task_name": "h0001_a001", "extra": True})
    _parent(path, _event(wrong_keys))
    with pytest.raises(HostRecordError, match="unexpected key set"):
        read_native_parent_rollout(path, "h0001_a001")
    _parent(path, *_spawn_events("call-1", "h0001_a001", "implementer"))
    with pytest.raises(HostRecordError, match="role or fork_turns"):
        read_native_parent_rollout(path, "h0001_a001")


def test_parent_rollout_ignores_unterminated_tail_and_reads_only_the_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parent.jsonl"
    _parent(path, *_spawn_events("call-1", "h0001_a001"), tail='{"type":"respon')
    assert len(read_native_parent_rollout(path, "h0001_a001").spawn_calls) == 1


def test_parent_rollout_reads_beyond_the_generic_eight_mebibyte_bound(tmp_path: Path) -> None:
    path = tmp_path / "parent.jsonl"
    filler = _event(
        {"type": "response_item", "payload": {"type": "reasoning", "x": "y" * (1024 * 1024)}}
    )
    _parent(path, *([filler] * 9), *_spawn_events("call-1", "h0001_a001"))
    assert path.stat().st_size > 8 * 1024 * 1024
    assert len(read_native_parent_rollout(path, "h0001_a001").spawn_calls) == 1
    with pytest.raises(HostRecordError, match="exceeds the bounded size limit"):
        read_native_parent_rollout(path, "h0001_a001", max_bytes=8 * 1024 * 1024)


def test_generic_rollout_keeps_the_eight_mebibyte_bound_and_child_bound_is_explicit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    _rollout(path)
    with path.open("ab") as stream:
        stream.write(b" " * (9 * 1024 * 1024) + b"\n")
    with pytest.raises(HostRecordError, match="exceeds the bounded size limit"):
        read_exact_rollout(path)
    assert read_exact_rollout(path, max_bytes=64 * 1024 * 1024).terminal_state == "succeeded"


def test_rollout_unterminated_tail_is_pending_when_ignored_and_parsed_otherwise(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    raw = _rollout(path)
    terminal = raw.splitlines()[-1]
    path.write_bytes(raw.rsplit(b"\n", 2)[0] + b"\n" + terminal)  # no final newline
    assert read_exact_rollout(path).terminal_state == "succeeded"
    ignored = read_exact_rollout(path, unterminated_tail="ignore")
    assert ignored.terminal_state == "pending"
    assert ignored.unterminated_tail is True
    assert ignored.last_agent_message is None


def test_rollout_digest_through_terminal_ignores_later_appends(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    raw = _rollout(path)
    with path.open("ab") as stream:
        stream.write(_event({"type": "event_msg", "payload": {"type": "agent_message"}}).encode())
        stream.write(b"\n")
    assert read_exact_rollout(path, digest_through_terminal=True).sha256 == (
        hashlib.sha256(raw).hexdigest()
    )
    assert read_exact_rollout(path).sha256 != hashlib.sha256(raw).hexdigest()


def test_rollout_terminal_markers_error_and_abort_classification(tmp_path: Path) -> None:
    errored = tmp_path / "errored.jsonl"
    _rollout(errored, terminal=None)
    marker_payload: dict[str, object] = {
        "type": "task_complete",
        "last_agent_message": None,
        "error": {"message": "usage", "codex_error_info": "usage_limit_exceeded"},
    }
    marker: dict[str, object] = {"type": "event_msg", "payload": marker_payload}
    with errored.open("a") as stream:
        stream.write(_event(marker) + "\n")
    record = read_exact_rollout(errored)
    assert record.terminal_state == "failed"
    assert record.last_agent_message is None
    null_error = tmp_path / "null-error.jsonl"
    _rollout(null_error, terminal=None)
    marker_payload["error"] = None
    marker_payload["last_agent_message"] = "done"
    with null_error.open("a") as stream:
        stream.write(_event(marker) + "\n")
    assert read_exact_rollout(null_error).terminal_state == "succeeded"
    aborted = tmp_path / "aborted.jsonl"
    _rollout(aborted, terminal="turn_aborted")
    assert read_exact_rollout(aborted).terminal_state == "cancelled"
    assert read_exact_rollout(aborted).last_agent_message is None


def test_rollout_token_count_without_info_keeps_the_last_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    _rollout(path, terminal=None)
    with path.open("a") as stream:
        stream.write(
            _event({"type": "event_msg", "payload": {"type": "token_count", "info": None}})
        )
        stream.write("\n")
    usage = read_exact_rollout(path).latest_usage
    assert usage is not None and usage.total_tokens == 12
