"""Custom OTel metrics for G2 Gateway observability."""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    from opentelemetry import metrics

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter


def _get_meter() -> Meter | None:
    if not _HAS_OTEL:
        return None
    return metrics.get_meter("g2-gateway")


class GatewayMetrics:
    """Lazy-initialized gateway metrics. All methods are no-ops if OTel is unavailable."""

    def __init__(self) -> None:
        meter = _get_meter()
        if meter is None:
            self._connections = None
            self._transcription_duration = None
            self._openclaw_request_duration = None
            self._openclaw_errors = None
            return

        self._connections = meter.create_up_down_counter(
            "gateway.ws.connections_active",
            description="Active WebSocket connections",
        )
        self._transcription_duration = meter.create_histogram(
            "gateway.transcription.duration_seconds",
            description="Whisper transcription duration",
            unit="s",
        )
        self._openclaw_request_duration = meter.create_histogram(
            "gateway.openclaw.request_duration_seconds",
            description="Time from sending to OpenClaw to lifecycle end",
            unit="s",
        )
        self._openclaw_errors = meter.create_counter(
            "gateway.openclaw.errors_total",
            description="OpenClaw communication errors",
        )

    def connection_opened(self) -> None:
        if self._connections:
            self._connections.add(1)

    def connection_closed(self) -> None:
        if self._connections:
            self._connections.add(-1)

    def record_transcription_duration(self, duration_s: float) -> None:
        if self._transcription_duration:
            self._transcription_duration.record(duration_s)

    def record_openclaw_request_duration(self, duration_s: float) -> None:
        if self._openclaw_request_duration:
            self._openclaw_request_duration.record(duration_s)

    def record_openclaw_error(self) -> None:
        if self._openclaw_errors:
            self._openclaw_errors.add(1)


# Module-level singleton
gateway_metrics = GatewayMetrics()
