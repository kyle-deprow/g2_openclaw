"""PCM accumulation buffer for G2 audio streaming."""

from __future__ import annotations

import numpy as np


class BufferOverflow(Exception):
    """Raised when audio buffer exceeds maximum duration."""


class AudioBuffer:
    """Accumulates raw PCM bytes and converts to float32 numpy array for Whisper."""

    MAX_DURATION_SECONDS = 60  # doc 02 §4.3

    def __init__(
        self,
        sample_rate: int = 16_000,
        channels: int = 1,
        sample_width: int = 2,
    ) -> None:
        """Accept format params from start_audio frame."""
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width  # bytes per sample (2 = 16-bit)
        self._byte_rate = sample_rate * channels * sample_width
        self._max_bytes = self.MAX_DURATION_SECONDS * self._byte_rate
        self._chunks: list[bytes] = []
        self._total_bytes = 0

    def append(self, chunk: bytes) -> None:
        """Append PCM bytes. Raises BufferOverflow if limit exceeded."""
        if self._total_bytes + len(chunk) > self._max_bytes:
            raise BufferOverflow(
                f"Audio buffer overflow: {self._total_bytes + len(chunk)} bytes "
                f"exceeds {self.MAX_DURATION_SECONDS}s limit ({self._max_bytes} bytes)"
            )
        self._chunks.append(chunk)
        self._total_bytes += len(chunk)

    def to_numpy(self) -> np.ndarray:
        """Convert accumulated PCM to float32 array normalized to [-1.0, 1.0].

        Assumes 16-bit signed integer PCM (sample_width=2).
        """
        raw = b"".join(self._chunks)
        samples = np.frombuffer(raw, dtype=np.int16)
        return samples.astype(np.float32) / 32768.0

    def reset(self) -> None:
        """Clear buffer for next recording."""
        self._chunks.clear()
        self._total_bytes = 0

    @property
    def duration_seconds(self) -> float:
        """Estimated duration from byte count and format."""
        if self._byte_rate == 0:
            return 0.0
        return self._total_bytes / self._byte_rate

    @property
    def is_empty(self) -> bool:
        return self._total_bytes == 0
