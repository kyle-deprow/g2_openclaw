"""Gateway configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class GatewayConfig:
    """Immutable gateway configuration."""

    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8765
    gateway_token: str | None = None
    whisper_model: str = "base.en"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    openclaw_host: str = "127.0.0.1"
    openclaw_port: int = 18789
    openclaw_gateway_token: str | None = None
    agent_timeout: int = 120
    auth_timeout: float = 5.0


_WEAK_TOKENS = {"changeme", "test", "password", "secret", "token", "admin", ""}

logger = logging.getLogger(__name__)


def load_config() -> GatewayConfig:
    """Load gateway config from environment variables.

    Reads a ``.env`` file if present, then builds a :class:`GatewayConfig` from:

    - ``GATEWAY_HOST`` (default ``"127.0.0.1"``)
    - ``GATEWAY_PORT`` (default ``8765``)
    - ``GATEWAY_TOKEN`` (default ``None`` — no auth)
    - ``WHISPER_MODEL`` (default ``"base.en"``)
    - ``WHISPER_DEVICE`` (default ``"cpu"``)
    - ``WHISPER_COMPUTE_TYPE`` (default ``"int8"``)
    - ``OPENCLAW_HOST`` (default ``"127.0.0.1"``)
    - ``OPENCLAW_PORT`` (default ``18789``)
    - ``OPENCLAW_GATEWAY_TOKEN`` (default ``None``)
    - ``AGENT_TIMEOUT`` (default ``120``)
    """
    load_dotenv()

    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8765"))
    token = os.environ.get("GATEWAY_TOKEN")
    whisper_model = os.environ.get("WHISPER_MODEL", "base.en")
    whisper_device = os.environ.get("WHISPER_DEVICE", "cpu")
    whisper_compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
    openclaw_host = os.environ.get("OPENCLAW_HOST", "127.0.0.1")
    openclaw_port = int(os.environ.get("OPENCLAW_PORT", "18789"))
    openclaw_gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    agent_timeout = int(os.environ.get("AGENT_TIMEOUT", "120"))

    cfg = GatewayConfig(
        gateway_host=host,
        gateway_port=port,
        gateway_token=token if token else None,
        whisper_model=whisper_model,
        whisper_device=whisper_device,
        whisper_compute_type=whisper_compute_type,
        openclaw_host=openclaw_host,
        openclaw_port=openclaw_port,
        openclaw_gateway_token=openclaw_gateway_token if openclaw_gateway_token else None,
        agent_timeout=agent_timeout,
    )

    if cfg.gateway_token is None:
        logger.warning(
            "GATEWAY_TOKEN is not set — the gateway is running WITHOUT authentication. "
            "Set GATEWAY_TOKEN to a strong random value for production use."
        )
    elif cfg.gateway_token in _WEAK_TOKENS:
        logger.warning(
            "GATEWAY_TOKEN is set to a weak value ('%s'). "
            'Generate a strong token, e.g.: python -c "import secrets;'
            ' print(secrets.token_urlsafe(32))"',
            cfg.gateway_token,
        )

    return cfg
