"""Tests for gateway.tts module."""

from __future__ import annotations

import shutil

import numpy as np
import pytest
from gateway.tts import _espeak_to_wav_bytes, _wav_bytes_to_pcm, synthesize_pcm

pytestmark = pytest.mark.asyncio

# Skip all tests if espeak-ng is not installed
espeak_available = shutil.which("espeak-ng") is not None
skip_no_espeak = pytest.mark.skipif(not espeak_available, reason="espeak-ng not installed")


@skip_no_espeak
class TestSynthesizePcm:
    async def test_returns_pcm_bytes_and_rate(self) -> None:
        pcm, rate = await synthesize_pcm("hello world")
        assert rate == 16_000
        assert isinstance(pcm, bytes)
        assert len(pcm) > 0
        # PCM is 16-bit so length must be even
        assert len(pcm) % 2 == 0

    async def test_pcm_is_valid_audio(self) -> None:
        pcm, rate = await synthesize_pcm("testing one two three")
        samples = np.frombuffer(pcm, dtype=np.int16)
        # Should have reasonable length (at least 0.1s of audio)
        assert len(samples) > rate * 0.1
        # Should not be silence (RMS > some threshold)
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        assert rms > 10, f"Audio appears to be silence (RMS={rms})"

    async def test_custom_sample_rate(self) -> None:
        pcm, rate = await synthesize_pcm("hello", target_sample_rate=22050)
        assert rate == 22050
        assert len(pcm) > 0

    async def test_empty_text_still_produces_output(self) -> None:
        """espeak-ng produces a short silence for empty text — should not crash."""
        pcm, rate = await synthesize_pcm("")
        assert rate == 16_000
        assert isinstance(pcm, bytes)


@skip_no_espeak
class TestEspeakToWavBytes:
    async def test_returns_wav_bytes(self) -> None:
        wav = await _espeak_to_wav_bytes("hello")
        # WAV files start with RIFF header
        assert wav[:4] == b"RIFF"

    async def test_wav_parseable(self) -> None:
        import io
        import wave

        wav = await _espeak_to_wav_bytes("test")
        with wave.open(io.BytesIO(wav), "rb") as wf:
            assert wf.getnchannels() >= 1
            assert wf.getsampwidth() in (1, 2)
            assert wf.getframerate() > 0


class TestWavBytesToPcm:
    def test_identity_conversion(self) -> None:
        """16-bit mono at target rate should pass through unchanged."""
        import io
        import wave

        # Create a valid WAV at 16000 Hz
        samples = np.array([0, 1000, -1000, 500], dtype=np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(samples.tobytes())
        pcm = _wav_bytes_to_pcm(buf.getvalue(), 16000)
        result = np.frombuffer(pcm, dtype=np.int16)
        np.testing.assert_array_equal(result, samples)

    def test_resampling(self) -> None:
        """Different source rate should resample to target."""
        import io
        import wave

        # Create 1 second of 440 Hz tone at 22050 Hz
        src_rate = 22050
        t = np.arange(src_rate) / src_rate
        tone = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(src_rate)
            wf.writeframes(tone.tobytes())
        pcm = _wav_bytes_to_pcm(buf.getvalue(), 16000)
        result = np.frombuffer(pcm, dtype=np.int16)
        # Should be approximately 16000 samples (1 second at 16k)
        assert abs(len(result) - 16000) < 2
