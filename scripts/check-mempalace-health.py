#!/usr/bin/env python3
"""Strict MemPalace healthcheck for G2/OpenClaw startup.

This check intentionally fails instead of repairing or falling back. A failed
palace should be fixed explicitly before OpenClaw starts using research memory.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

EXPECTED_MODEL = os.environ.get("MEMPALACE_EXPECTED_EMBEDDING_MODEL", "bge-base")
EXPECTED_DIMENSION = int(os.environ.get("MEMPALACE_EXPECTED_EMBEDDING_DIMENSION", "768"))
COLLECTION_NAME = os.environ.get("MEMPALACE_COLLECTION_NAME", "mempalace_drawers")
PALACE_PATH = Path(
    os.environ.get("MEMPALACE_PALACE", str(Path.home() / ".mempalace" / "palace"))
).expanduser()
CONFIG_PATH = Path(
    os.environ.get("MEMPALACE_CONFIG", str(Path.home() / ".mempalace" / "config.json"))
).expanduser()


def _error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        _error(f"{path} is not valid JSON: {exc}")
        raise SystemExit(1) from exc
    if not isinstance(data, dict):
        _error(f"{path} must contain a JSON object")
        raise SystemExit(1)
    return data


def _check_static_files() -> int:
    failures = 0

    cache_path = os.environ.get("FASTEMBED_CACHE_PATH")
    if not cache_path:
        _error("FASTEMBED_CACHE_PATH must be set to a durable cache directory")
        failures += 1
    elif not Path(cache_path).expanduser().is_dir():
        _error(f"FASTEMBED_CACHE_PATH does not exist: {cache_path}")
        failures += 1

    if os.environ.get("MEMPALACE_EMBEDDING_MODEL", EXPECTED_MODEL) != EXPECTED_MODEL:
        _error(f"MEMPALACE_EMBEDDING_MODEL must be {EXPECTED_MODEL!r}")
        failures += 1

    config = _load_json(CONFIG_PATH)
    if config is None:
        _error(f"MemPalace config is missing: {CONFIG_PATH}")
        failures += 1
    elif config.get("embedding_model") != EXPECTED_MODEL:
        _error(
            f"{CONFIG_PATH} embedding_model must be {EXPECTED_MODEL!r}; "
            f"got {config.get('embedding_model')!r}"
        )
        failures += 1

    if not PALACE_PATH.is_dir():
        _error(f"MemPalace palace directory is missing: {PALACE_PATH}")
        failures += 1

    return failures


def main() -> int:
    failures = _check_static_files()
    if failures:
        return 1

    try:
        import chromadb  # type: ignore[import-not-found]
        from mempalace.embedding import get_embedding_function  # type: ignore[import-not-found]
    except ImportError as exc:
        _error(f"MemPalace health imports failed: {exc}")
        return 1

    try:
        embedding_function = get_embedding_function(model=EXPECTED_MODEL)
        probe = embedding_function(["mempalace health check"])
    except Exception as exc:
        _error(f"Embedding model {EXPECTED_MODEL!r} failed to load: {exc}")
        return 1

    if not probe or len(probe[0]) != EXPECTED_DIMENSION:
        got = len(probe[0]) if probe else 0
        _error(f"Embedding dimension must be {EXPECTED_DIMENSION}; got {got}")
        return 1

    identity_path = PALACE_PATH / "mempalace_embedder.json"
    identity = _load_json(identity_path)
    if identity is not None:
        collection_identity = identity.get(COLLECTION_NAME)
        if not isinstance(collection_identity, dict):
            _error(f"{identity_path} is missing identity for {COLLECTION_NAME!r}")
            return 1
        if collection_identity.get("model_name") != EXPECTED_MODEL:
            _error(
                f"{identity_path} model_name must be {EXPECTED_MODEL!r}; "
                f"got {collection_identity.get('model_name')!r}"
            )
            return 1
        if collection_identity.get("dimension") != EXPECTED_DIMENSION:
            _error(
                f"{identity_path} dimension must be {EXPECTED_DIMENSION}; "
                f"got {collection_identity.get('dimension')!r}"
            )
            return 1

    try:
        client = chromadb.PersistentClient(path=str(PALACE_PATH))
        collection = client.get_collection(
            COLLECTION_NAME,
            embedding_function=embedding_function,
        )
    except Exception as exc:
        message = str(exc).lower()
        if "does not exist" not in message and "not found" not in message:
            _error(f"Chroma collection {COLLECTION_NAME!r} failed to load: {exc}")
            return 1
        print(f"OK: MemPalace collection {COLLECTION_NAME!r} is not initialized yet ({exc})")
        return 0

    metadata = collection.metadata or {}
    if metadata.get("hnsw:space") != "cosine":
        _error(
            f"Chroma collection {COLLECTION_NAME!r} must use cosine distance; "
            f"metadata is {metadata!r}. Run an explicit repair before startup."
        )
        return 1

    count = collection.count()
    if count > 0 and identity is None:
        _error(f"{identity_path} is required when {COLLECTION_NAME!r} contains drawers")
        return 1

    if count > 0:
        try:
            collection.query(query_texts=["mempalace health check"], n_results=1)
        except Exception as exc:
            _error(f"MemPalace query probe failed: {exc}")
            return 1

    t87 = collection.get(where={"room": "room_t87_ellt"}, limit=1)
    if t87.get("ids"):
        result = collection.query(
            query_texts=["T87 ELLT"],
            where={"wing": "wing_quantipy"},
            n_results=1,
        )
        ids = result.get("ids") or []
        first_id = ids[0][0] if ids and ids[0] else None
        if first_id != t87["ids"][0]:
            _error("Quantipy search probe did not return T87-ELLT as the top result")
            return 1

    print(
        "OK: MemPalace healthcheck passed "
        f"(model={EXPECTED_MODEL}, dim={EXPECTED_DIMENSION}, drawers={count})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
