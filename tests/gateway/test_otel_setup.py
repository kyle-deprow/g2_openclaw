"""Tests for gateway.otel_setup."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from unittest.mock import patch

import pytest
from gateway.otel_setup import configure_logging, init_otel
from opentelemetry import metrics, trace
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once


@pytest.fixture(autouse=True)
def _reset_otel_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset OTel global providers and root logger handlers between tests."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    # Snapshot root logger handlers before the test
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    yield

    # Uninstrument the logging instrumentor if it was activated
    with contextlib.suppress(Exception):
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().uninstrument()

    # Reset the global tracer provider (write-once guard)
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()

    # Reset the global meter provider (write-once guard)
    metrics_internal._METER_PROVIDER = None
    metrics_internal._METER_PROVIDER_SET_ONCE = Once()

    # Restore root logger to pre-test state
    # (removes handlers added by configure_logging / init_otel)
    root.handlers = original_handlers
    root.level = original_level


class TestInitOtelReturnsShutdownCallable:
    def test_returns_callable(self) -> None:
        shutdown = init_otel()

        assert callable(shutdown)
        shutdown()  # must not raise


class TestInitOtelSetsTracerProvider:
    def test_tracer_provider_is_sdk_provider(self) -> None:
        init_otel()

        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)


class TestInitOtelSetsMeterProvider:
    def test_meter_provider_is_sdk_provider(self) -> None:
        init_otel()

        provider = metrics.get_meter_provider()
        assert isinstance(provider, MeterProvider)


class TestInitOtelNoopWhenDisabled:
    def test_empty_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

        shutdown = init_otel()

        assert callable(shutdown)
        shutdown()  # noop — no error
        assert not isinstance(trace.get_tracer_provider(), TracerProvider)

    def test_none_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "none")

        shutdown = init_otel()

        assert callable(shutdown)
        shutdown()
        assert not isinstance(trace.get_tracer_provider(), TracerProvider)


class TestInitOtelNoopWhenPackagesMissing:
    def test_import_error_returns_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real_import = builtins.__import__

        def _block_otel(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("opentelemetry"):
                raise ImportError(f"mocked: {name}")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        with patch("builtins.__import__", side_effect=_block_otel):
            shutdown = init_otel()

        assert callable(shutdown)
        shutdown()  # noop — no error
        assert not isinstance(trace.get_tracer_provider(), TracerProvider)


class TestLoggingInstrumentorActive:
    def test_log_record_emitted_to_otel(self) -> None:
        init_otel()

        # Attach an in-memory log exporter to verify OTel sees log records
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

        in_memory_exporter = InMemoryLogRecordExporter()
        logger_provider = LoggerProvider()
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(in_memory_exporter))

        from opentelemetry.instrumentation.logging.handler import LoggingHandler

        handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        test_logger = logging.getLogger("test.otel.verification")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)

        test_logger.info("hello from otel test")

        logger_provider.force_flush()
        records = in_memory_exporter.get_finished_logs()
        messages = [r.log_record.body for r in records]
        assert any("hello from otel test" in str(m) for m in messages)

        test_logger.removeHandler(handler)
        logger_provider.shutdown()


class TestConfigureLoggingOtelInactive:
    def test_adds_console_and_file_handler(self) -> None:
        configure_logging(otel_active=False)

        root = logging.getLogger()
        handler_types = [type(h) for h in root.handlers]
        assert logging.StreamHandler in handler_types
        assert RotatingFileHandler in handler_types

    def test_console_handler_level_is_info(self) -> None:
        configure_logging(otel_active=False)

        root = logging.getLogger()
        stream_handlers = [h for h in root.handlers if type(h) is logging.StreamHandler]
        assert stream_handlers[0].level == logging.INFO

    def test_file_handler_level_is_debug(self) -> None:
        configure_logging(otel_active=False)

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert file_handlers[0].level == logging.DEBUG


class TestConfigureLoggingOtelActive:
    def test_adds_only_console_handler(self) -> None:
        configure_logging(otel_active=True)

        root = logging.getLogger()
        handler_types = [type(h) for h in root.handlers]
        assert logging.StreamHandler in handler_types
        assert RotatingFileHandler not in handler_types

    def test_root_level_is_debug(self) -> None:
        configure_logging(otel_active=True)

        root = logging.getLogger()
        assert root.level == logging.DEBUG


class TestInitOtelConfiguresLogging:
    def test_otel_active_no_file_handler(self) -> None:
        init_otel()

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert file_handlers == []

    def test_otel_disabled_adds_file_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "none")

        init_otel()

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1
