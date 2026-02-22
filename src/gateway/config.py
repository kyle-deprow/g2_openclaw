"""Gateway configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class GatewayConfig:
    """Immutable gateway configuration."""

    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8765
    gateway_token: str | None = None


def load_config() -> GatewayConfig:
    """Load gateway config from environment variables.

    Reads a ``.env`` file if present, then builds a :class:`GatewayConfig` from:

    - ``GATEWAY_HOST`` (default ``"0.0.0.0"``)
    - ``GATEWAY_PORT`` (default ``8765``)
    - ``GATEWAY_TOKEN`` (default ``None`` — no auth)
    """
    load_dotenv()

    host = os.environ.get("GATEWAY_HOST", "0.0.0.0")
    port = int(os.environ.get("GATEWAY_PORT", "8765"))
    token = os.environ.get("GATEWAY_TOKEN")

    return GatewayConfig(
        gateway_host=host,
        gateway_port=port,
        gateway_token=token if token else None,
    )
