"""Tests for gateway.config."""

import pytest
from gateway.config import GatewayConfig, load_config


@pytest.fixture(autouse=True)
def _required_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most load_config tests need production-required auth tokens."""
    monkeypatch.setenv("GATEWAY_TOKEN", "gateway-token-1234567890")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "openclaw-token-1234567890")


class TestGatewayConfigDefaults:
    """GatewayConfig defaults."""

    def test_defaults(self) -> None:
        cfg = GatewayConfig()
        assert cfg.gateway_host == "127.0.0.1"
        assert cfg.gateway_port == 8765
        assert cfg.gateway_token is None

    def test_frozen(self) -> None:
        cfg = GatewayConfig()
        with pytest.raises(AttributeError):
            cfg.gateway_port = 9999  # type: ignore[misc]

    def test_config_defaults_include_whisper(self) -> None:
        cfg = GatewayConfig()
        assert cfg.whisper_model == "base.en"
        assert cfg.whisper_device == "cpu"
        assert cfg.whisper_compute_type == "int8"


class TestLoadConfig:
    """load_config reads from environment."""

    def test_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
        monkeypatch.setenv("GATEWAY_PORT", "9000")
        monkeypatch.setenv("GATEWAY_TOKEN", "gateway-token-abcdef")

        cfg = load_config()

        assert cfg.gateway_host == "127.0.0.1"
        assert cfg.gateway_port == 9000
        assert cfg.gateway_token == "gateway-token-abcdef"

    def test_optional_defaults_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.delenv("GATEWAY_HOST", raising=False)
        monkeypatch.delenv("GATEWAY_PORT", raising=False)

        cfg = load_config()

        assert cfg.gateway_host == "127.0.0.1"
        assert cfg.gateway_port == 8765
        assert cfg.gateway_token == "gateway-token-1234567890"

    def test_empty_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
        monkeypatch.setenv("GATEWAY_TOKEN", "")

        with pytest.raises(ValueError, match="GATEWAY_TOKEN is required"):
            load_config()

    def test_config_whisper_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHISPER_MODEL", "large-v3")
        monkeypatch.setenv("WHISPER_DEVICE", "cuda")
        monkeypatch.setenv("WHISPER_COMPUTE_TYPE", "float16")

        cfg = load_config()

        assert cfg.whisper_model == "large-v3"
        assert cfg.whisper_device == "cuda"
        assert cfg.whisper_compute_type == "float16"

    def test_config_whisper_defaults_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.delenv("WHISPER_MODEL", raising=False)
        monkeypatch.delenv("WHISPER_DEVICE", raising=False)
        monkeypatch.delenv("WHISPER_COMPUTE_TYPE", raising=False)

        cfg = load_config()

        assert cfg.whisper_model == "base.en"
        assert cfg.whisper_device == "cpu"
        assert cfg.whisper_compute_type == "int8"


class TestOpenClawConfigDefaults:
    """OpenClaw-related config defaults."""

    def test_openclaw_defaults(self) -> None:
        cfg = GatewayConfig()
        assert cfg.openclaw_host == "127.0.0.1"
        assert cfg.openclaw_port == 18789
        assert cfg.openclaw_gateway_token is None
        assert cfg.agent_timeout == 120
        assert cfg.openclaw_agent_id == "main"


class TestOpenClawLoadConfig:
    """load_config reads OpenClaw env vars."""

    def test_openclaw_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENCLAW_HOST", "10.0.0.5")
        monkeypatch.setenv("OPENCLAW_PORT", "9999")
        monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "oc-secret")
        monkeypatch.setenv("AGENT_TIMEOUT", "60")

        cfg = load_config()

        assert cfg.openclaw_host == "10.0.0.5"
        assert cfg.openclaw_port == 9999
        assert cfg.openclaw_gateway_token == "oc-secret"
        assert cfg.agent_timeout == 60
        assert cfg.openclaw_agent_id == "main"

    def test_openclaw_optional_defaults_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.delenv("OPENCLAW_HOST", raising=False)
        monkeypatch.delenv("OPENCLAW_PORT", raising=False)
        monkeypatch.delenv("AGENT_TIMEOUT", raising=False)

        cfg = load_config()

        assert cfg.openclaw_host == "127.0.0.1"
        assert cfg.openclaw_port == 18789
        assert cfg.openclaw_gateway_token == "openclaw-token-1234567890"
        assert cfg.agent_timeout == 120

    def test_empty_openclaw_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "")

        with pytest.raises(ValueError, match="OPENCLAW_GATEWAY_TOKEN is required"):
            load_config()


class TestSecurityConfig:
    """Tests for security-related config behaviors."""

    def test_non_loopback_without_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Binding to a non-loopback host without GATEWAY_TOKEN raises ValueError."""
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("GATEWAY_HOST", "10.0.0.1")
        monkeypatch.delenv("GATEWAY_TOKEN", raising=False)

        with pytest.raises(ValueError, match="GATEWAY_TOKEN is required"):
            load_config()

    def test_all_interfaces_without_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Binding to 0.0.0.0 without GATEWAY_TOKEN raises ValueError."""
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")
        monkeypatch.delenv("GATEWAY_TOKEN", raising=False)

        with pytest.raises(ValueError, match="GATEWAY_TOKEN is required"):
            load_config()

    def test_ipv6_all_interfaces_without_token_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binding to :: without GATEWAY_TOKEN raises ValueError."""
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("GATEWAY_HOST", "::")
        monkeypatch.delenv("GATEWAY_TOKEN", raising=False)

        with pytest.raises(ValueError, match="GATEWAY_TOKEN is required"):
            load_config()

    def test_loopback_without_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loopback host without token raises ValueError."""
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
        monkeypatch.delenv("GATEWAY_TOKEN", raising=False)

        with pytest.raises(ValueError, match="GATEWAY_TOKEN is required"):
            load_config()

    def test_weak_token_raises_without_leaking_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Weak token raises without leaking the actual token value."""
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
        monkeypatch.setenv("GATEWAY_TOKEN", "changeme")

        with pytest.raises(ValueError, match="weak/common value") as exc_info:
            load_config()
        assert "changeme" not in str(exc_info.value)

    def test_weak_token_non_loopback_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Weak token on non-loopback host raises ValueError."""
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("GATEWAY_HOST", "0.0.0.0")
        monkeypatch.setenv("GATEWAY_TOKEN", "changeme")

        with pytest.raises(ValueError, match="weak/common value"):
            load_config()

    def test_short_token_non_loopback_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Short token on non-loopback host raises ValueError."""
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("GATEWAY_HOST", "10.0.0.1")
        monkeypatch.setenv("GATEWAY_TOKEN", "abc123")

        with pytest.raises(ValueError, match="too short"):
            load_config()

    def test_short_token_loopback_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Short token on loopback host raises ValueError."""
        monkeypatch.setattr("gateway.config.load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
        monkeypatch.setenv("GATEWAY_TOKEN", "short")

        with pytest.raises(ValueError, match="too short"):
            load_config()

    def test_parse_int_env_invalid_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-integer GATEWAY_PORT raises clear ValueError."""
        monkeypatch.setenv("GATEWAY_PORT", "not-a-number")

        with pytest.raises(ValueError, match="must be an integer"):
            load_config()

    def test_parse_int_env_invalid_openclaw_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-integer OPENCLAW_PORT raises clear ValueError."""
        monkeypatch.setenv("OPENCLAW_PORT", "abc")

        with pytest.raises(ValueError, match="must be an integer"):
            load_config()

    def test_auth_timeout_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AUTH_TIMEOUT is parsed from env."""
        monkeypatch.setenv("AUTH_TIMEOUT", "2.5")

        cfg = load_config()
        assert cfg.auth_timeout == 2.5

    def test_auth_timeout_invalid_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-numeric AUTH_TIMEOUT raises ValueError."""
        monkeypatch.setenv("AUTH_TIMEOUT", "nope")

        with pytest.raises(ValueError, match="must be a number"):
            load_config()

    def test_autoresearch_feed_interval_default(self) -> None:
        """AUTORESEARCH_FEED_INTERVAL defaults to 5.0."""
        cfg = load_config()
        assert cfg.autoresearch_feed_interval == 5.0

    def test_autoresearch_feed_interval_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AUTORESEARCH_FEED_INTERVAL is parsed from env."""
        monkeypatch.setenv("AUTORESEARCH_FEED_INTERVAL", "0")

        cfg = load_config()
        assert cfg.autoresearch_feed_interval == 0.0

    def test_autoresearch_feed_interval_invalid_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-numeric AUTORESEARCH_FEED_INTERVAL raises ValueError."""
        monkeypatch.setenv("AUTORESEARCH_FEED_INTERVAL", "nope")

        with pytest.raises(ValueError, match="must be a number"):
            load_config()

    def test_allowed_origins_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ALLOWED_ORIGINS is parsed as comma-separated list."""
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.com, https://other.com")

        cfg = load_config()
        assert cfg.allowed_origins == ["https://example.com", "https://other.com"]

    def test_allowed_origins_empty_string_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty ALLOWED_ORIGINS yields None."""
        monkeypatch.setenv("ALLOWED_ORIGINS", "")

        cfg = load_config()
        assert cfg.allowed_origins is None

    def test_allowed_origins_whitespace_only_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitespace-only ALLOWED_ORIGINS yields None."""
        monkeypatch.setenv("ALLOWED_ORIGINS", " , , ")

        cfg = load_config()
        assert cfg.allowed_origins is None

    def test_allowed_origins_default_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No ALLOWED_ORIGINS env var leaves it as None."""
        monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)

        cfg = load_config()
        assert cfg.allowed_origins is None
