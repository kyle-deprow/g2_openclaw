"""Fixture analysis module committed into the disposable worktree.

Runs as the real analysis stage: scenarios, inputs and the worktree are
read-only; only ``--out`` (``/stage/analysis``) is writable.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    importlib.import_module("quantipy")
    scenarios: dict[str, object] = {}
    for result_path in sorted(Path(args.scenarios).glob("s*/evaluator-stage/out/result.json")):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        scenarios[result_path.parents[2].name] = result["spec_sha256"]
    inputs = sorted(
        path.relative_to(args.inputs).as_posix()
        for path in Path(args.inputs).rglob("*")
        if path.is_file()
    )
    document = {"scenarios": scenarios, "inputs": inputs}
    (Path(args.out) / "result.json").write_text(json.dumps(document, sort_keys=True), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
