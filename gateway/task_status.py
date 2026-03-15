"""Extract task status from OpenClaw session transcripts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from gateway.session_history import extract_text, resolve_session_file

logger = logging.getLogger(__name__)

# Match [TASK:status] markers in assistant messages
_TASK_PATTERN = re.compile(
    r"\[TASK:(running|complete|failed)\]\s*(.+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TaskInfo:
    """A parsed task status from the transcript."""

    status: str  # "running", "complete", "failed"
    description: str  # Everything after the [TASK:status] marker


def read_task_status(
    session_key: str,
    agent_id: str = "claw",
) -> TaskInfo | None:
    """Read the most recent [TASK:*] marker from the session transcript.

    Returns the latest TaskInfo or None if no task markers found.
    Reads from the tail of the JSONL file for performance.
    """
    transcript_path = resolve_session_file(session_key, agent_id=agent_id)
    if transcript_path is None or not transcript_path.exists():
        return None

    # Read tail of file for performance (last 256KB)
    tail_bytes = 256_000
    try:
        file_size = transcript_path.stat().st_size
        with transcript_path.open("rb") as f:
            if file_size > tail_bytes:
                f.seek(file_size - tail_bytes)
                f.readline()  # Skip partial first line
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        logger.debug("Cannot read transcript %s", transcript_path)
        return None

    # Parse JSONL lines, find last assistant message with a [TASK:*] marker
    latest: TaskInfo | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("type") != "message":
            continue
        msg = entry.get("message", {})
        if msg.get("role") != "assistant":
            continue

        content = extract_text(msg.get("content", ""))
        if not content:
            continue

        match = _TASK_PATTERN.search(content)
        if match:
            latest = TaskInfo(
                status=match.group(1).lower(),
                description=match.group(2).strip(),
            )

    return latest
