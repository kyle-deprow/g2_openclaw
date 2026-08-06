"""Tests for managed Codex deployment helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import gateway.deployment.codex_agents as codex_agents
import pytest


def test_ensure_writable_roots_creates_private_nested_directories(tmp_path: Path) -> None:
    roots = (tmp_path / "nested" / "model-workspaces", tmp_path / "stage-inbox")

    codex_agents.ensure_writable_roots(roots)

    for root in roots:
        assert root.is_dir()
        assert root.stat().st_mode & 0o777 == 0o700


def test_ensure_writable_roots_tightens_existing_directory_mode(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)

    codex_agents.ensure_writable_roots((existing,))

    assert existing.stat().st_mode & 0o777 == 0o700


def test_write_runtime_config_round_trips_through_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = (tmp_path / "model-workspaces", tmp_path / "stage-inbox")
    config_path = tmp_path / "runtime.toml"
    monkeypatch.setattr(codex_agents, "CODEX_WRITABLE_ROOTS", roots)
    runtime_environment = {
        "CODEX_RUNTIME_AGENT_ID": "reviewer",
        "CODEX_RUNTIME_CONFIG_PATH": str(config_path),
        "CODEX_RUNTIME_MEMPALACE_PYTHON": "/opt/mempalace-python",
        "CODEX_RUNTIME_MEMPALACE_WRAPPER": "/opt/mempalace-wrapper.py",
        "CODEX_RUNTIME_MEMPALACE_PALACE": "/opt/palace",
        "CODEX_RUNTIME_FASTEMBED_CACHE_PATH": "/opt/cache",
        "CODEX_RUNTIME_MEMPALACE_EMBEDDING_MODEL": "bge-base",
        "CODEX_RUNTIME_HF_HUB_OFFLINE": "1",
    }
    for key, value in runtime_environment.items():
        monkeypatch.setenv(key, value)

    codex_agents.write_runtime_config()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "codex_agents",
            str(config_path),
            "reviewer",
            "/opt/mempalace-python",
            "/opt/mempalace-wrapper.py",
            "/opt/palace",
            "/opt/g2-python",
            "gateway.g2_control_mcp_server",
            str(tmp_path),
        ],
    )

    codex_agents.validate_mcp_wiring()

    assert all(root.is_dir() and root.stat().st_mode & 0o777 == 0o700 for root in roots)
