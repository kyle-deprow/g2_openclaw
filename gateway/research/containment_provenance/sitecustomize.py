"""Stdlib-only in-sandbox import provenance recorder."""

import atexit
import contextlib
import hashlib
import json
import os
import sys
from datetime import UTC, datetime


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record() -> None:
    directory = os.environ.get("RESEARCH_PROVENANCE_DIR")
    stage = os.environ.get("RESEARCH_PROVENANCE_STAGE")
    if not directory or not stage:
        return
    path = os.path.join(directory, f"{stage}-{os.getpid()}.json")
    created = False
    try:
        modules = []
        for name, module in sorted(sys.modules.items()):
            module_path = getattr(module, "__file__", None)
            if not isinstance(module_path, str):
                continue
            module_path = os.path.realpath(module_path)
            if module_path.startswith("/snapshot/src/"):
                origin = "snapshot"
            elif module_path.startswith("/work/"):
                origin = "work"
            elif module_path.startswith("/provenance/"):
                origin = "recorder"
            else:
                origin = "runtime"
            entry = {"name": name, "path": module_path, "origin": origin}
            if origin in {"snapshot", "work", "recorder"}:
                entry["sha256"] = _sha256(module_path)
            modules.append(entry)
        payload = {
            "contract": "research-containment-provenance-v1",
            "stage": stage,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "argv": sys.argv,
            "executable": sys.executable,
            "cwd": os.getcwd(),
            "python_version": sys.version.split()[0],
            "flags": {
                "safe_path": bool(getattr(sys.flags, "safe_path", False)),
                "no_user_site": bool(sys.flags.no_user_site),
            },
            "pythonpath": os.environ.get("PYTHONPATH"),
            "sys_path": list(sys.path),
            "recorder": {"path": os.path.realpath(__file__), "sha256": _sha256(__file__)},
            "modules": modules,
            "written_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if created:
            with contextlib.suppress(Exception):
                os.unlink(path)


directory = os.environ.get("RESEARCH_PROVENANCE_DIR")
stage = os.environ.get("RESEARCH_PROVENANCE_STAGE")
if directory and stage:
    atexit.register(_record)
