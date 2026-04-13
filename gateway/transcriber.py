"""Async Whisper wrapper for speech-to-text transcription."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

try:
    from opentelemetry import trace
    from opentelemetry.trace import StatusCode

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


def _span(name: str, **attributes: str | int | float | bool):  # type: ignore[no-untyped-def]
    """Create an OTel span context manager, or a no-op if OTel is unavailable."""
    if not _HAS_OTEL:
        return contextlib.nullcontext()
    return trace.get_tracer(__name__).start_as_current_span(name, attributes=attributes)


def _record_span_error(exc: BaseException) -> None:
    """Record an exception on the current active span, if any."""
    if not _HAS_OTEL:
        return
    span = trace.get_current_span()
    span.set_status(StatusCode.ERROR, str(exc))
    span.record_exception(exc)


class TranscriptionError(Exception):
    """Raised when transcription fails or produces empty result."""


class Transcriber:
    """Async wrapper around faster-whisper for speech-to-text."""

    def __init__(
        self,
        model_name: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        """Load the Whisper model. This blocks during model download/load."""
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model_name, device=device, compute_type=compute_type)

    async def transcribe(
        self,
        audio: np.ndarray,
        language: str = "en",
        timeout: float = 30.0,
    ) -> str:
        """Transcribe audio array to text.

        Runs inference in a thread pool executor to avoid blocking the event loop.
        Uses greedy decoding with VAD filter per doc 02 §4.6.

        Raises:
            TranscriptionError: If transcription is empty.
            asyncio.TimeoutError: If inference exceeds timeout.
        """
        with _span("whisper.transcribe", language=language, audio_samples=len(audio)):
            try:
                loop = asyncio.get_running_loop()

                def _run_inference() -> str:
                    segments, _info = self._model.transcribe(
                        audio,
                        language=language,
                        beam_size=1,
                        best_of=1,
                        temperature=0.0,
                        condition_on_previous_text=False,
                        vad_filter=True,
                        vad_parameters={"threshold": 0.3},
                    )
                    return " ".join(seg.text.strip() for seg in segments).strip()

                result = await asyncio.wait_for(
                    loop.run_in_executor(None, _run_inference),
                    timeout=timeout,
                )

                if not result:
                    raise TranscriptionError("Transcription produced empty result")

                return result
            except (TranscriptionError, TimeoutError) as exc:
                _record_span_error(exc)
                raise
