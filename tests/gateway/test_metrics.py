"""Tests for gateway.metrics — custom OTel application metrics."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from gateway.metrics import GatewayMetrics
from opentelemetry import metrics
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.util._once import Once


@pytest.fixture()
def metric_reader() -> Iterator[InMemoryMetricReader]:
    """Set up an in-memory metric reader and reset OTel globals after."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)

    yield reader

    provider.shutdown()
    metrics_internal._METER_PROVIDER = None
    metrics_internal._METER_PROVIDER_SET_ONCE = Once()


def _fresh_metrics() -> GatewayMetrics:
    """Import and construct a fresh GatewayMetrics to pick up current provider."""
    from gateway.metrics import GatewayMetrics

    return GatewayMetrics()


class TestGatewayMetricsInitializes:
    def test_creates_without_error_when_otel_available(
        self, metric_reader: InMemoryMetricReader
    ) -> None:
        gm = _fresh_metrics()

        assert gm._connections is not None
        assert gm._transcription_duration is not None
        assert gm._openclaw_request_duration is not None
        assert gm._openclaw_errors is not None
        assert gm._orphans_reaped is not None

    def test_all_noop_when_otel_unavailable(self) -> None:
        with patch("gateway.metrics._HAS_OTEL", False):
            from gateway.metrics import GatewayMetrics

            gm = GatewayMetrics()

        assert gm._connections is None
        assert gm._transcription_duration is None
        assert gm._openclaw_request_duration is None
        assert gm._openclaw_errors is None
        assert gm._orphans_reaped is None

    def test_noop_methods_do_not_raise(self) -> None:
        with patch("gateway.metrics._HAS_OTEL", False):
            from gateway.metrics import GatewayMetrics

            gm = GatewayMetrics()

        gm.connection_opened()
        gm.connection_closed()
        gm.record_transcription_duration(1.5)
        gm.record_openclaw_request_duration(2.0)
        gm.record_openclaw_error()
        gm.record_orphans_reaped(3)


class TestMetricMethods:
    def test_each_method_does_not_raise(self, metric_reader: InMemoryMetricReader) -> None:
        gm = _fresh_metrics()

        gm.connection_opened()
        gm.connection_closed()
        gm.record_transcription_duration(0.5)
        gm.record_openclaw_request_duration(1.2)
        gm.record_openclaw_error()
        gm.record_orphans_reaped(2)

    def test_connection_counting(self, metric_reader: InMemoryMetricReader) -> None:
        gm = _fresh_metrics()

        gm.connection_opened()
        gm.connection_opened()
        gm.connection_closed()

        data = metric_reader.get_metrics_data()
        assert data is not None
        connection_metrics = [
            m
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == "gateway.ws.connections_active"
        ]
        assert len(connection_metrics) == 1
        points = list(connection_metrics[0].data.data_points)
        assert len(points) == 1
        # Two opens + one close = net +1
        assert points[0].value == 1

    def test_transcription_duration_recorded(self, metric_reader: InMemoryMetricReader) -> None:
        gm = _fresh_metrics()

        gm.record_transcription_duration(0.42)

        data = metric_reader.get_metrics_data()
        assert data is not None
        hist_metrics = [
            m
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == "gateway.transcription.duration_seconds"
        ]
        assert len(hist_metrics) == 1
        points = list(hist_metrics[0].data.data_points)
        assert len(points) == 1
        assert points[0].sum == pytest.approx(0.42)

    def test_openclaw_error_counter(self, metric_reader: InMemoryMetricReader) -> None:
        gm = _fresh_metrics()

        gm.record_openclaw_error()
        gm.record_openclaw_error()

        data = metric_reader.get_metrics_data()
        assert data is not None
        err_metrics = [
            m
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == "gateway.openclaw.errors_total"
        ]
        assert len(err_metrics) == 1
        points = list(err_metrics[0].data.data_points)
        assert len(points) == 1
        assert points[0].value == 2

    def test_orphans_reaped_counter(self, metric_reader: InMemoryMetricReader) -> None:
        gm = _fresh_metrics()

        gm.record_orphans_reaped(5)

        data = metric_reader.get_metrics_data()
        assert data is not None
        reap_metrics = [
            m
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == "gateway.process_monitor.orphans_reaped_total"
        ]
        assert len(reap_metrics) == 1
        points = list(reap_metrics[0].data.data_points)
        assert len(points) == 1
        assert points[0].value == 5
