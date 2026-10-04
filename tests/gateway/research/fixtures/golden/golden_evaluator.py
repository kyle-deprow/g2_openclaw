#!/usr/bin/env python3
"""Deterministic stand-in for the pinned quantipy research console script.

Only the external evaluator program is faked.  It is executed by the real worker
as ``python -P -s <pinned script> research <command> ...`` inside the real
bubblewrap/systemd-run boundary and writes the same output shapes the worker and
host verifier require: a ``validate-inputs`` PASS document on stdout, and for
``evaluate`` exactly ``result.json``, ``trades.parquet`` and ``daily.parquet``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _semantic_sha256(spec_path: str) -> str:
    """Digest the parsed spec so formatting changes do not alter the semantic hash."""
    parsed = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_inputs(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "verdict": "PASS",
                "reasons": [],
                "spec_sha256_semantic": _semantic_sha256(args.spec),
                "spec_sha256_raw": _sha256(args.spec),
                "panel_sha256": _sha256(args.panel),
                "receipt_sha256": _sha256(args.receipt),
                "universe_file_sha256": _sha256(args.universe),
                "dividends_sha256": _sha256(args.dividends),
            },
            sort_keys=True,
        )
    )
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    quantipy: Any = importlib.import_module("quantipy")
    source_root = args.require_source_root
    if source_root and not str(quantipy.__file__).startswith(source_root):
        print(f"quantipy not imported from {source_root}", file=sys.stderr)
        return 4
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    if spec.get("golden_fail"):
        print("golden evaluator: deliberate genuine failure", file=sys.stderr)
        return 1
    targets = json.loads(Path(args.targets).read_text(encoding="utf-8"))
    out = Path(args.out)
    if out.exists():
        print(f"output directory already exists: {out}", file=sys.stderr)
        return 1
    out.mkdir(parents=True)
    cost_bps = int(spec["cost_bps"])
    # The strategy fixture writes "positions"; the compute probe's empty targets file uses
    # the real evaluator's {"targets": []} shape.
    sessions = len(targets.get("positions", targets.get("targets", [])))
    result = {
        "evaluator_version": "research-evaluator-v2",
        "spec_sha256": _semantic_sha256(args.spec),
        "dividends_sha256": _sha256(args.dividends),
        "compliant": True,
        "zero_trade": False,
        "metrics_available": True,
        "acceptance_class": "accepted",
        "earnings_provenance": "unavailable",
        "sessions": sessions,
        "mean_net_return": round(0.001 - cost_bps / 10000, 6),
    }
    (out / "result.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    # The sandbox interpreter has no parquet library, so these are marker-framed
    # placeholders; the worker and host only bind their names, sizes and digests.
    for name in ("trades.parquet", "daily.parquet"):
        body = f"{name}:{cost_bps}:{sessions}".encode()
        (out / name).write_bytes(b"PAR1" + body + b"PAR1")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("group")
    parser.add_argument("command")
    for option in ("panel", "receipt", "spec", "dividends", "universe", "targets", "out"):
        parser.add_argument(f"--{option}")
    parser.add_argument("--require-source-root")
    args = parser.parse_args(argv)
    if args.group != "research":
        return 2
    if args.command == "validate-inputs":
        return _validate_inputs(args)
    if args.command == "evaluate":
        return _evaluate(args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
