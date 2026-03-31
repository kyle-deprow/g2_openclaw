"""Preload script that patches the OpenAI SDK for GitHub Copilot API compatibility.

Patches applied:
1. Injects required Editor-Version / Editor-Plugin-Version / Copilot-Integration-Id
   headers into AsyncOpenAI when base_url points to a Copilot endpoint.
2. Forces encoding_format="float" for embeddings (Copilot proxy rejects "base64").

Usage:
    Import via sitecustomize.py (PYTHONPATH) before the Graphiti MCP server starts.
"""

# mypy: ignore-errors
# This file runs inside the Graphiti MCP venv, not our project venv.

import openai

_original_init = openai.AsyncOpenAI.__init__

COPILOT_HEADERS = {
    "Editor-Version": "vscode/1.100.0",
    "Editor-Plugin-Version": "copilot/1.300.0",
    "Copilot-Integration-Id": "vscode-chat",
}

COPILOT_DOMAINS = ("githubcopilot.com",)


def _patched_init(self: object, *args: object, **kwargs: object) -> None:
    base_url = kwargs.get("base_url") or (args[0] if args else None)
    base_url_str = str(base_url) if base_url else ""
    if any(d in base_url_str for d in COPILOT_DOMAINS):
        existing = kwargs.get("default_headers") or {}
        merged = {**COPILOT_HEADERS, **existing}
        kwargs["default_headers"] = merged
    _original_init(self, *args, **kwargs)


openai.AsyncOpenAI.__init__ = _patched_init

# Patch embeddings.create to force encoding_format="float" for Copilot proxy
_orig_embeddings_create = openai.resources.AsyncEmbeddings.create


async def _patched_embeddings_create(self: object, *args: object, **kwargs: object) -> object:
    base_url_str = str(self._client.base_url) if hasattr(self, "_client") else ""
    if any(d in base_url_str for d in COPILOT_DOMAINS):
        kwargs["encoding_format"] = "float"
    return await _orig_embeddings_create(self, *args, **kwargs)


openai.resources.AsyncEmbeddings.create = _patched_embeddings_create
