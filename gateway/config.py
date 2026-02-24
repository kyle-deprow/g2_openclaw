"""Gateway configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

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
    allowed_origins: list[str] | None = None


_WEAK_TOKENS = {"changeme", "test", "password", "secret", "token", "admin", ""}

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

logger = logging.getLogger(__name__)


def _parse_int_env(name: str, default: str) -> int:
    """Parse an integer environment variable with a clear error on bad values."""
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


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
    - ``AUTH_TIMEOUT`` (default ``5.0``)
    - ``ALLOWED_ORIGINS`` (default ``None`` — comma-separated list of allowed origins)
    """
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = _parse_int_env("GATEWAY_PORT", "8765")
    token = os.environ.get("GATEWAY_TOKEN")
    whisper_model = os.environ.get("WHISPER_MODEL", "base.en")
    whisper_device = os.environ.get("WHISPER_DEVICE", "cpu")
    whisper_compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
    openclaw_host = os.environ.get("OPENCLAW_HOST", "127.0.0.1")
    openclaw_port = _parse_int_env("OPENCLAW_PORT", "18789")
    openclaw_gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    agent_timeout = _parse_int_env("AGENT_TIMEOUT", "120")
    auth_timeout_raw = os.environ.get("AUTH_TIMEOUT", "5.0")
    try:
        auth_timeout = float(auth_timeout_raw)
    except ValueError as exc:
        raise ValueError(
            f"Environment variable AUTH_TIMEOUT must be a number, got {auth_timeout_raw!r}"
        ) from exc

    allowed_origins_raw = os.environ.get("ALLOWED_ORIGINS")
    allowed_origins: list[str] | None = None
    if allowed_origins_raw:
        allowed_origins = [o.strip() for o in allowed_origins_raw.split(",") if o.strip()]
        if not allowed_origins:
            allowed_origins = None

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
        auth_timeout=auth_timeout,
        allowed_origins=allowed_origins,
    )

    if cfg.gateway_token is None:
        if cfg.gateway_host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"GATEWAY_TOKEN is required when listening on non-loopback interface "
                f"({cfg.gateway_host}). Set GATEWAY_TOKEN to a strong random value."
            )
        logger.warning(
            "GATEWAY_TOKEN is not set — the gateway is running WITHOUT authentication. "
            "Set GATEWAY_TOKEN to a strong random value for production use."
        )
    elif cfg.gateway_token in _WEAK_TOKENS:
        logger.warning(
            "GATEWAY_TOKEN is set to a weak value. "
            'Generate a strong token, e.g.: python -c "import secrets;'
            ' print(secrets.token_urlsafe(32))"'
        )

    return cfg
