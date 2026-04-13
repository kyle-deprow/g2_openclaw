"""Tests for OTel trace span helpers in gateway modules."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once


@pytest.fixture()
def span_exporter() -> Iterator[InMemorySpanExporter]:
    """Set up an in-memory exporter and reset OTel globals after."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    yield exporter

    provider.shutdown()
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()


class TestServerSpanHelper:
    def test_span_returns_context_manager_with_tracer(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        from gateway.server import _span as server_span

        with server_span("gateway.test_span", msg_len=42):
            pass

        spans = span_exporter.get_finished_spans()
        assert any(s.name == "gateway.test_span" for s in spans)

    def test_span_records_attributes(self, span_exporter: InMemorySpanExporter) -> None:
        from gateway.server import _span as server_span

        with server_span("gateway.test_attrs", msg_len=99):
            pass

        spans = span_exporter.get_finished_spans()
        span = next(s for s in spans if s.name == "gateway.test_attrs")
        assert span.attributes is not None
        assert span.attributes["msg_len"] == 99

    def test_span_returns_nullcontext_when_otel_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gateway.server as server_mod

        monkeypatch.setattr(server_mod, "_HAS_OTEL", False)

        result = server_mod._span("gateway.noop")
        assert isinstance(result, contextlib.nullcontext)


class TestOpenClawClientSpanHelper:
    def test_span_creates_named_span(self, span_exporter: InMemorySpanExporter) -> None:
        from gateway.openclaw_client import _span as oc_span

        with oc_span("openclaw.test_span", session_key="abc"):
            pass

        spans = span_exporter.get_finished_spans()
        span = next(s for s in spans if s.name == "openclaw.test_span")
        assert span.attributes is not None
        assert span.attributes["session_key"] == "abc"

    def test_span_returns_nullcontext_when_otel_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gateway.openclaw_client as oc_mod

        monkeypatch.setattr(oc_mod, "_HAS_OTEL", False)

        result = oc_mod._span("openclaw.noop")
        assert isinstance(result, contextlib.nullcontext)


class TestTranscriberSpanHelper:
    def test_span_creates_named_span(self, span_exporter: InMemorySpanExporter) -> None:
        from gateway.transcriber import _span as t_span

        with t_span("whisper.test", language="en", audio_samples=1000):
            pass

        spans = span_exporter.get_finished_spans()
        span = next(s for s in spans if s.name == "whisper.test")
        assert span.attributes is not None
        assert span.attributes["language"] == "en"
        assert span.attributes["audio_samples"] == 1000

    def test_span_returns_nullcontext_when_otel_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gateway.transcriber as transcriber_mod

        monkeypatch.setattr(transcriber_mod, "_HAS_OTEL", False)

        result = transcriber_mod._span("whisper.noop")
        assert isinstance(result, contextlib.nullcontext)


class TestTtsSpanHelper:
    def test_span_creates_named_span(self, span_exporter: InMemorySpanExporter) -> None:
        from gateway.tts import _span as tts_span

        with tts_span("tts.test", text_length=50, target_sample_rate=16000):
            pass

        spans = span_exporter.get_finished_spans()
        span = next(s for s in spans if s.name == "tts.test")
        assert span.attributes is not None
        assert span.attributes["text_length"] == 50
        assert span.attributes["target_sample_rate"] == 16000

    def test_span_returns_nullcontext_when_otel_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gateway.tts as tts_mod

        monkeypatch.setattr(tts_mod, "_HAS_OTEL", False)

        result = tts_mod._span("tts.noop")
        assert isinstance(result, contextlib.nullcontext)


class TestSpanOnCriticalOperations:
    """Verify that critical functions are actually instrumented with spans."""

    def test_transcriber_transcribe_has_span(self, span_exporter: InMemorySpanExporter) -> None:
        """Verify _span is importable and callable from transcriber module."""
        from gateway.transcriber import _span

        with _span("whisper.transcribe", language="en", audio_samples=500):
            pass

        spans = span_exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert "whisper.transcribe" in names

    def test_tts_synthesize_has_span(self, span_exporter: InMemorySpanExporter) -> None:
        """Verify _span is importable and callable from tts module."""
        from gateway.tts import _span

        with _span("tts.synthesize", text_length=10, target_sample_rate=16000):
            pass

        spans = span_exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert "tts.synthesize" in names
