"""Tests for gateway.config."""

import pytest

from gateway.config import GatewayConfig, load_config


class TestGatewayConfigDefaults:
    """GatewayConfig defaults."""

    def test_defaults(self) -> None:
        cfg = GatewayConfig()
        assert cfg.gateway_host == "0.0.0.0"
        assert cfg.gateway_port == 8765
        assert cfg.gateway_token is None

    def test_frozen(self) -> None:
        cfg = GatewayConfig()
        with pytest.raises(AttributeError):
            cfg.gateway_port = 9999  # type: ignore[misc]


class TestLoadConfig:
    """load_config reads from environment."""

    def test_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
        monkeypatch.setenv("GATEWAY_PORT", "9000")
        monkeypatch.setenv("GATEWAY_TOKEN", "s3cret")

        cfg = load_config()

        assert cfg.gateway_host == "127.0.0.1"
        assert cfg.gateway_port == 9000
        assert cfg.gateway_token == "s3cret"

    def test_defaults_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GATEWAY_HOST", raising=False)
        monkeypatch.delenv("GATEWAY_PORT", raising=False)
        monkeypatch.delenv("GATEWAY_TOKEN", raising=False)

        cfg = load_config()

        assert cfg.gateway_host == "0.0.0.0"
        assert cfg.gateway_port == 8765
        assert cfg.gateway_token is None

    def test_empty_token_treated_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GATEWAY_TOKEN", "")

        cfg = load_config()

        assert cfg.gateway_token is None
