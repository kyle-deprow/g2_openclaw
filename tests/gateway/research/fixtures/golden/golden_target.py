"""Fixture strategy target committed into the disposable worktree.

It runs as a real target stage (read-only ``/work``, read-only panel and receipt,
writable ``/stage``) and writes the targets document the fake evaluator consumes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _work_is_writable() -> bool:
    try:
        Path("/work/golden-probe.txt").write_text("x", encoding="utf-8")
    except OSError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--variant", default="baseline")
    args = parser.parse_args()
    panel = Path(args.panel).read_bytes()
    receipt = Path(args.receipt).read_bytes()
    document = {
        "contract": "golden-targets-v1",
        "variant": args.variant,
        "panel_sha256": hashlib.sha256(panel).hexdigest(),
        "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
        "work_writable": _work_is_writable(),
        "positions": [
            {"date": "2024-01-02", "symbol": "SPY", "weight": 1.0},
            {"date": "2024-01-03", "symbol": "SPY", "weight": 0.5},
        ],
    }
    Path(args.out).write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
