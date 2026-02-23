"""Tests for gateway.audio_buffer — PCM accumulation buffer."""

from __future__ import annotations

import struct

import numpy as np
import pytest

from gateway.audio_buffer import AudioBuffer, BufferOverflow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _silence_bytes(n: int) -> bytes:
    """Return *n* zero bytes (silence in any PCM encoding)."""
    return b"\x00" * n


def _int16_bytes(*values: int) -> bytes:
    """Pack int16 values into little-endian PCM bytes."""
    return struct.pack(f"<{len(values)}h", *values)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAppend:
    """Tests for AudioBuffer.append accumulation."""

    def test_append_small_chunks(self) -> None:
        """Append multiple 40-byte chunks (real hardware size), verify total bytes."""
        buf = AudioBuffer()
        for _ in range(10):
            buf.append(_silence_bytes(40))
        assert buf._total_bytes == 400

    def test_append_large_chunks(self) -> None:
        """Append 3200-byte chunks (simulator size), verify total bytes."""
        buf = AudioBuffer()
        for _ in range(5):
            buf.append(_silence_bytes(3200))
        assert buf._total_bytes == 16_000

    def test_mixed_chunk_sizes(self) -> None:
        """Append both 40 and 3200 byte chunks, verify correct accumulation."""
        buf = AudioBuffer()
        buf.append(_silence_bytes(40))
        buf.append(_silence_bytes(3200))
        buf.append(_silence_bytes(40))
        assert buf._total_bytes == 3280


class TestToNumpy:
    """Tests for PCM → float32 conversion."""

    def test_to_numpy_shape(self) -> None:
        """Append known bytes, verify output shape matches expected sample count."""
        buf = AudioBuffer()
        # 100 samples × 2 bytes each = 200 bytes
        buf.append(_silence_bytes(200))
        arr = buf.to_numpy()
        assert arr.shape == (100,)
        assert arr.dtype == np.float32

    def test_to_numpy_range(self) -> None:
        """Verify output is in [-1.0, 1.0] range for arbitrary PCM data."""
        buf = AudioBuffer()
        # Use extreme int16 values
        buf.append(_int16_bytes(32767, -32768, 0, 16384, -16384))
        arr = buf.to_numpy()
        assert arr.min() >= -1.0
        assert arr.max() <= 1.0

    def test_to_numpy_values(self) -> None:
        """Append known int16 values, verify float32 conversion is correct."""
        buf = AudioBuffer()
        buf.append(_int16_bytes(32767, -32768, 0))
        arr = buf.to_numpy()

        assert arr[0] == pytest.approx(32767.0 / 32768.0)  # ~1.0
        assert arr[1] == pytest.approx(-32768.0 / 32768.0)  # -1.0
        assert arr[2] == pytest.approx(0.0)


class TestOverflow:
    """Tests for buffer overflow protection."""

    def test_overflow_raises(self) -> None:
        """Exceed 60 s limit, verify BufferOverflow raised."""
        buf = AudioBuffer()  # default 16 kHz mono 16-bit → 32 000 bytes/s
        max_bytes = buf.MAX_DURATION_SECONDS * buf._byte_rate
        # Fill right up to the limit
        buf.append(_silence_bytes(max_bytes))
        # Two more bytes (aligned to sample_width=2) should overflow
        with pytest.raises(BufferOverflow):
            buf.append(b"\x00\x00")

    def test_overflow_message_contains_duration(self) -> None:
        """Verify error message mentions duration/bytes."""
        buf = AudioBuffer()
        max_bytes = buf.MAX_DURATION_SECONDS * buf._byte_rate
        buf.append(_silence_bytes(max_bytes))
        with pytest.raises(BufferOverflow, match=r"60s limit"):
            buf.append(b"\x00\x00")


class TestReset:
    """Tests for buffer reset."""

    def test_reset_clears_buffer(self) -> None:
        """Append, reset, verify is_empty and duration is 0."""
        buf = AudioBuffer()
        buf.append(_silence_bytes(3200))
        assert not buf.is_empty
        buf.reset()
        assert buf.is_empty
        assert buf.duration_seconds == 0.0


class TestProperties:
    """Tests for duration_seconds and is_empty."""

    def test_duration_seconds(self) -> None:
        """Append known bytes, verify calculated duration."""
        buf = AudioBuffer(sample_rate=16_000, channels=1, sample_width=2)
        # 32 000 bytes = 1 second at 16 kHz mono 16-bit
        buf.append(_silence_bytes(32_000))
        assert buf.duration_seconds == pytest.approx(1.0)

    def test_is_empty_initial(self) -> None:
        """New buffer is empty."""
        buf = AudioBuffer()
        assert buf.is_empty

    def test_custom_format(self) -> None:
        """Use non-default sample_rate/channels, verify byte rate calculation."""
        buf = AudioBuffer(sample_rate=44_100, channels=2, sample_width=2)
        expected_byte_rate = 44_100 * 2 * 2
        assert buf._byte_rate == expected_byte_rate
        # 1 second of audio at this rate
        buf.append(_silence_bytes(expected_byte_rate))
        assert buf.duration_seconds == pytest.approx(1.0)


class TestChunkValidation:
    """Tests for odd-byte and empty chunk handling (Issue 5)."""

    def test_odd_byte_chunk_raises_value_error(self) -> None:
        """Chunk whose size is not a multiple of sample_width raises ValueError."""
        buf = AudioBuffer()  # sample_width=2
        with pytest.raises(ValueError, match="must be a multiple"):
            buf.append(b"\x00\x01\x02")  # 3 bytes, not multiple of 2

    def test_single_byte_chunk_raises_value_error(self) -> None:
        """A single byte is not valid for 16-bit PCM."""
        buf = AudioBuffer()
        with pytest.raises(ValueError, match="must be a multiple"):
            buf.append(b"\x00")

    def test_empty_chunk_is_noop(self) -> None:
        """Appending an empty chunk does not modify the buffer."""
        buf = AudioBuffer()
        buf.append(b"")
        assert buf.is_empty
        assert buf._total_bytes == 0

    def test_even_chunk_accepted(self) -> None:
        """Valid even-byte chunk is accepted."""
        buf = AudioBuffer()
        buf.append(_silence_bytes(100))  # 100 bytes, multiple of 2
        assert buf._total_bytes == 100


class TestToNumpyGuard:
    """Tests for to_numpy sample_width guard."""

    def test_to_numpy_rejects_non_16bit(self) -> None:
        """to_numpy() raises ValueError when sample_width != 2."""
        buf = AudioBuffer(sample_width=3)
        buf._chunks = [b"\x00" * 6]
        buf._total_bytes = 6
        with pytest.raises(ValueError, match="requires sample_width=2"):
            buf.to_numpy()
