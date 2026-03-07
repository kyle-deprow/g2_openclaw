"""Text-to-speech synthesis via espeak-ng for HIL dev mode."""

from __future__ import annotations

import asyncio
import io
import logging
import wave

import numpy as np

logger = logging.getLogger(__name__)

# espeak-ng native output rate (may vary by platform, 22050 is typical)
_ESPEAK_SAMPLE_RATE = 22050


async def synthesize_pcm(
    text: str,
    target_sample_rate: int = 16_000,
) -> tuple[bytes, int]:
    """Synthesize *text* to 16-bit mono PCM bytes at *target_sample_rate*.

    Returns ``(pcm_bytes, sample_rate)`` where *sample_rate* ==
    *target_sample_rate*.

    Raises ``RuntimeError`` on espeak-ng failure.
    """
    wav_bytes = await _espeak_to_wav_bytes(text)
    pcm = _wav_bytes_to_pcm(wav_bytes, target_sample_rate)
    return pcm, target_sample_rate


async def _espeak_to_wav_bytes(text: str) -> bytes:
    """Run espeak-ng with --stdout and capture the WAV output."""
    proc = await asyncio.create_subprocess_exec(
        "espeak-ng",
        "--stdout",
        "-v",
        "en",
        "-s",
        "160",  # words-per-minute (slightly fast for clarity)
        text,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"espeak-ng failed (rc={proc.returncode}): {detail}")
    if not stdout:
        raise RuntimeError("espeak-ng produced empty output")
    return stdout


def _wav_bytes_to_pcm(wav_bytes: bytes, target_rate: int) -> bytes:
    """Convert WAV bytes to raw 16-bit mono PCM at *target_rate*."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        src_rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    # Decode to int16 array
    if sample_width == 2:
        samples = np.frombuffer(raw, dtype=np.int16)
    elif sample_width == 1:
        # 8-bit unsigned → 16-bit signed
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) * 256
    else:
        raise RuntimeError(f"Unsupported sample width from espeak-ng: {sample_width}")

    # Convert to mono if stereo
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1).astype(np.int16)

    # Resample if needed
    if src_rate != target_rate:
        # Linear interpolation resample
        duration = len(samples) / src_rate
        n_target = int(duration * target_rate)
        indices = np.linspace(0, len(samples) - 1, n_target)
        resampled = np.interp(indices, np.arange(len(samples)), samples.astype(np.float64))
        samples = np.clip(resampled, -32768, 32767).astype(np.int16)

    return samples.tobytes()
