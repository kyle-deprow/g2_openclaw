"""Tests for gateway.transcriber — async Whisper wrapper (fully mocked)."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Provide a fake ``faster_whisper`` module so that importing
# ``gateway.transcriber`` never requires the real package.
# ---------------------------------------------------------------------------
_mock_fw_module = ModuleType("faster_whisper")
_mock_fw_module.WhisperModel = MagicMock()  # type: ignore[attr-defined]
sys.modules.setdefault("faster_whisper", _mock_fw_module)

from gateway.transcriber import Transcriber, TranscriptionError  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_segment(text: str) -> MagicMock:
    """Return a mock segment whose ``.text`` attribute is *text*."""
    seg = MagicMock()
    seg.text = text
    return seg


def _make_info() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def mock_model() -> Iterator[MagicMock]:
    """Yield a fresh mock WhisperModel **instance** wired into the module.

    Each test gets its own mock so side-effects don't leak.
    """
    model_cls = MagicMock()
    model_instance = model_cls.return_value
    # Patch the class in the fake module
    _mock_fw_module.WhisperModel = model_cls  # type: ignore[attr-defined]
    yield model_instance
    # Reset for next test
    _mock_fw_module.WhisperModel = MagicMock()  # type: ignore[attr-defined]


def _build_transcriber() -> Transcriber:
    """Construct a Transcriber (will use the mocked WhisperModel)."""
    return Transcriber(model_name="base.en", device="cpu", compute_type="int8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

DUMMY_AUDIO = np.zeros(16_000, dtype=np.float32)


class TestTranscribeHappyPath:
    async def test_transcribe_returns_text(self, mock_model: MagicMock) -> None:
        """Mock WhisperModel, return fake segments, verify joined text."""
        mock_model.transcribe.return_value = (
            iter([_make_segment("hello world")]),
            _make_info(),
        )
        t = _build_transcriber()
        result = await t.transcribe(DUMMY_AUDIO)
        assert result == "hello world"

    async def test_multiple_segments_joined(self, mock_model: MagicMock) -> None:
        """Mock model returning 3 segments, verify space-joined."""
        mock_model.transcribe.return_value = (
            iter([_make_segment("one"), _make_segment("two"), _make_segment("three")]),
            _make_info(),
        )
        t = _build_transcriber()
        result = await t.transcribe(DUMMY_AUDIO)
        assert result == "one two three"

    async def test_whitespace_handling(self, mock_model: MagicMock) -> None:
        """Mock segments with leading/trailing whitespace, verify stripped output."""
        mock_model.transcribe.return_value = (
            iter([_make_segment("  hello  "), _make_segment("  world  ")]),
            _make_info(),
        )
        t = _build_transcriber()
        result = await t.transcribe(DUMMY_AUDIO)
        assert result == "hello world"


class TestTranscribeParams:
    async def test_transcribe_passes_correct_params(self, mock_model: MagicMock) -> None:
        """Verify beam_size=1, temperature=0.0, vad_filter=True, etc."""
        mock_model.transcribe.return_value = (
            iter([_make_segment("ok")]),
            _make_info(),
        )
        t = _build_transcriber()
        await t.transcribe(DUMMY_AUDIO, language="en")

        mock_model.transcribe.assert_called_once()
        _args, kwargs = mock_model.transcribe.call_args
        assert kwargs["beam_size"] == 1
        assert kwargs["best_of"] == 1
        assert kwargs["temperature"] == 0.0
        assert kwargs["condition_on_previous_text"] is False
        assert kwargs["vad_filter"] is True
        assert kwargs["language"] == "en"


class TestTranscribeErrors:
    async def test_empty_transcription_raises(self, mock_model: MagicMock) -> None:
        """Mock model returning empty segments, verify TranscriptionError."""
        mock_model.transcribe.return_value = (iter([]), _make_info())
        t = _build_transcriber()
        with pytest.raises(TranscriptionError, match="empty result"):
            await t.transcribe(DUMMY_AUDIO)

    async def test_timeout_raises(self, mock_model: MagicMock) -> None:
        """Mock model that blocks beyond timeout, verify asyncio.TimeoutError."""

        def slow_transcribe(*args: Any, **kwargs: Any) -> tuple[Any, MagicMock]:
            time.sleep(5)
            return iter([_make_segment("too late")]), _make_info()

        mock_model.transcribe.side_effect = slow_transcribe
        t = _build_transcriber()
        with pytest.raises(asyncio.TimeoutError):
            await t.transcribe(DUMMY_AUDIO, timeout=0.1)
