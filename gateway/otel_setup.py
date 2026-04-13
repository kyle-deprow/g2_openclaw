"""OpenTelemetry initialization for G2 Gateway."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

logger = logging.getLogger(__name__)


def _noop_shutdown() -> None:
    pass


def init_otel() -> Callable[[], None]:
    """Initialize OpenTelemetry tracing, metrics, and logging.

    Returns a shutdown callable that flushes and shuts down all providers.
    If OTel packages are not installed or the endpoint is disabled, returns
    a no-op callable and the gateway continues without telemetry.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    if endpoint.lower() in ("", "none"):
        logger.info("OTel disabled (OTEL_EXPORTER_OTLP_ENDPOINT=%r) — skipping init", endpoint)
        return _noop_shutdown

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from opentelemetry.instrumentation.logging.handler import LoggingHandler
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("OpenTelemetry packages not installed — telemetry disabled")
        return _noop_shutdown

    resource = Resource.create(
        {
            "service.name": "g2-gateway",
            "service.version": _get_version(),
        }
    )

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(tracer_provider)

    # Metrics
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # Logs
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint))
    )

    # Attach OTel log handler to root logger so existing logging.getLogger() calls emit to OTel
    otel_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    logging.getLogger().addHandler(otel_handler)

    # Bridge stdlib logging → OTel (adds trace context to log records)
    LoggingInstrumentor().instrument(set_logging_format=True)

    logger.info("OpenTelemetry initialized (endpoint=%s)", endpoint)

    def shutdown() -> None:
        logging.getLogger().removeHandler(otel_handler)
        tracer_provider.shutdown()
        meter_provider.shutdown()
        logger_provider.shutdown()

    return shutdown


def _get_version() -> str:
    """Read package version, falling back to 'dev'."""
    try:
        from importlib.metadata import version

        # Project is named azure-infra-cli in pyproject.toml (historical — infra CLI was first)
        return version("azure-infra-cli")
    except Exception:
        return "dev"
