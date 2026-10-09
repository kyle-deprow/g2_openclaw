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


# Sandbox code cannot import the gateway; tests assert these mirror the gateway constants.
MAX_SESSIONS = 60
BORROW_COST_MISSING_REASON = "long_only is false but costs.borrow_bps_annual is not set above zero"
NO_SHORTABLE_REASON = "long_only is false but no instrument is shortable"
COMMON_STOCK_REFUSAL = (
    "trusted universe cannot contain common stock until an earnings calendar source exists"
)
DAILY_RECEIPT_CONTRACT = "research-price-panel-daily-v1"


def _spec_refusal(spec_path: str) -> str | None:
    """Mirror the v3 evaluator's refusals for the hedge fields of a spec, when present."""
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    max_sessions = spec.get("holding", {}).get("max_sessions", 5)
    if type(max_sessions) is not int or not 1 <= max_sessions <= MAX_SESSIONS:
        return f"holding.max_sessions must be an integer in 1..{MAX_SESSIONS}"
    if spec.get("long_only", True) is False and not any(
        item.get("shortable") is True for item in spec.get("instruments", [])
    ):
        return NO_SHORTABLE_REASON
    borrow = spec.get("costs", {}).get("borrow_bps_annual", 0.0)
    if isinstance(borrow, bool) or not isinstance(borrow, int | float) or borrow < 0:
        return "costs.borrow_bps_annual must be non-negative"
    if spec.get("long_only", True) is False and not borrow > 0:
        return BORROW_COST_MISSING_REASON
    return None


def _short_refusal(spec: dict[str, Any], positions: list[dict[str, Any]]) -> str | None:
    """SHORT_NOT_PERMITTED unless the spec is hedged and the shorted ticker is shortable."""
    shortable = {
        item.get("ticker") for item in spec.get("instruments", []) if item.get("shortable") is True
    }
    for position in positions:
        if position["weight"] < 0 and (
            spec.get("long_only", True) is not False or position["symbol"] not in shortable
        ):
            return f"SHORT_NOT_PERMITTED: {position['symbol']} target {position['weight']}"
    return None


def _has_common_stock(spec_path: str) -> bool:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    return any(
        item.get("instrument_class") == "common_stock" for item in spec.get("instruments", [])
    )


def _cost_bps(spec: dict[str, Any]) -> int:
    """The golden specs carry ``cost_bps``; stock specs use the real ``costs`` block."""
    if "cost_bps" in spec:
        return int(spec["cost_bps"])
    return int(spec["costs"]["half_spread_bps"])


def _optional_digests(args: argparse.Namespace) -> dict[str, str]:
    """Mirror the real validate-inputs / evaluator: report the digest of each bound input."""
    digests: dict[str, str] = {}
    if args.earnings_snapshot:
        digests["earnings_sha256"] = _sha256(args.earnings_snapshot)
    if args.universe_membership:
        digests["membership_sha256"] = _sha256(args.universe_membership)
    return digests


def _validate_inputs(args: argparse.Namespace) -> int:
    refusal = _spec_refusal(args.spec)
    if refusal is None and _has_common_stock(args.spec) and not args.earnings_snapshot:
        refusal = COMMON_STOCK_REFUSAL
    if refusal is not None:
        print(json.dumps({"verdict": "FAIL", "reasons": [refusal]}, sort_keys=True))
        return 1
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
                **_optional_digests(args),
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
    refusal = _spec_refusal(args.spec)
    if refusal is not None:
        print(f"golden evaluator: {refusal}", file=sys.stderr)
        return 1
    if spec.get("golden_fail"):
        print("golden evaluator: deliberate genuine failure", file=sys.stderr)
        return 1
    targets = json.loads(Path(args.targets).read_text(encoding="utf-8"))
    out = Path(args.out)
    if out.exists():
        print(f"output directory already exists: {out}", file=sys.stderr)
        return 1
    out.mkdir(parents=True)
    stock = _has_common_stock(args.spec)
    if stock and not args.earnings_snapshot:
        print(f"golden evaluator: {COMMON_STOCK_REFUSAL}", file=sys.stderr)
        return 1
    daily = args.receipt is not None and (
        json.loads(Path(args.receipt).read_text(encoding="utf-8")).get("contract")
        == DAILY_RECEIPT_CONTRACT
    )
    cost_bps = _cost_bps(spec)
    # The strategy fixture writes "positions"; the compute probe's empty targets file uses
    # the real evaluator's {"targets": []} shape.
    sessions = len(targets.get("positions", targets.get("targets", [])))
    positions = [item for item in targets.get("positions", []) if "weight" in item]
    short_refusal = _short_refusal(spec, positions)
    if short_refusal is not None:
        print(f"golden evaluator: {short_refusal}", file=sys.stderr)
        return 1
    borrow_bps_annual = float(spec.get("costs", {}).get("borrow_bps_annual", 0.0))
    # Borrow accrues per session on the short notional, so it comes from the targets' short
    # weights (not from the spec alone): a long-only target set accrues none.
    short_notional = sum(-item["weight"] for item in positions if item["weight"] < 0)
    net_exposure = round(sum(item["weight"] for item in positions) / max(len(positions), 1), 12)
    result: dict[str, Any] = {
        "evaluator_version": "research-evaluator-v3",
        "spec_sha256": _semantic_sha256(args.spec),
        "dividends_sha256": _sha256(args.dividends),
        "compliant": True,
        "zero_trade": False,
        "metrics_available": True,
        # Stock results are exploratory snapshot replay, never provider-backed acceptance.
        "acceptance_class": "exploratory_snapshot" if stock else "accepted",
        "earnings_provenance": "snapshot" if args.earnings_snapshot else "unavailable",
        "dividend_payable_total": 0.0,
        "sessions": sessions,
        "mean_net_return": round(0.001 - cost_bps / 10000, 6),
        **_optional_digests(args),
    }
    if daily:
        result["delisting_policy"] = {"long_haircut": 0.3, "max_stale_sessions": 5}
    (out / "result.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    # The sandbox interpreter has no parquet library, so these are marker-framed
    # placeholders; the worker and host only bind their names, sizes and digests.
    # The v3 daily table carries ``net_exposure`` and ``borrow_costs``.
    borrow_costs = round(short_notional * borrow_bps_annual / 1e4 / 252, 12)
    columns = {
        "trades.parquet": "",
        "daily.parquet": f":net_exposure={net_exposure}:borrow_costs={borrow_costs}",
    }
    for name, extra in columns.items():
        body = f"{name}:{cost_bps}:{sessions}{extra}".encode()
        (out / name).write_bytes(b"PAR1" + body + b"PAR1")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("group")
    parser.add_argument("command")
    for option in (
        "panel",
        "receipt",
        "spec",
        "dividends",
        "universe",
        "targets",
        "out",
        "earnings-snapshot",
        "universe-membership",
    ):
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
