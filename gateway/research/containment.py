"""Fixed namespace and host resource boundary for research stages.

The worker owns outcomes; this module only validates immutable runtime pins and
constructs the one allowed bubblewrap/systemd scope command.  It deliberately
has no fallback command for an unavailable boundary.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final


class ContainmentError(RuntimeError):
    """The trusted namespace or its immutable inputs are unavailable."""


_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_DOTTED_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SECRET_HINT = re.compile(r"(?i)(?:api[_-]?key|password|secret|token)")
_STAGES: Final[frozenset[str]] = frozenset({"validate", "targets", "evaluate"})
_MAX_TARGET_BYTES: Final[int] = 16 * 1024 * 1024
_BWRAP: Final[str] = "/usr/bin/bwrap"
_SYSTEMD_RUN: Final[str] = "/usr/bin/systemd-run"
_SYSTEMCTL: Final[str] = "/usr/bin/systemctl"


@dataclass(frozen=True, slots=True)
class RuntimePins:
    """All host paths and digests needed to reproduce the trusted runtime."""

    snapshot_dir: Path
    snapshot_sha256: str
    shared_python: Path
    resolved_python: Path
    shared_python_sha256: str
    venv_dir: Path
    pyvenv_cfg: Path
    pyvenv_sha256: str
    distribution_dir: Path
    evaluator: Path
    evaluator_sha256: str
    universe: Path
    universe_sha256: str


@dataclass(frozen=True, slots=True)
class StagePlan:
    """A fully fixed stage command and the exact resource scope it owns."""

    stage: str
    scope_unit: str
    argv: tuple[str, ...]


def runtime_pins_from_record(record: Mapping[str, object]) -> RuntimePins:
    """Reconstruct the immutable init-time pin set stored by the driver."""
    names = (
        "snapshot_dir",
        "snapshot_sha256",
        "shared_python",
        "shared_python_resolved",
        "shared_python_sha256",
        "pyvenv_cfg",
        "pyvenv_sha256",
        "distribution_dir",
        "evaluator",
        "evaluator_sha256",
        "universe",
        "universe_sha256",
    )
    values = {name: record[name] for name in names}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ContainmentError("complete operator runtime pins are required")
    return RuntimePins(
        Path(str(values["snapshot_dir"])),
        str(values["snapshot_sha256"]),
        Path(str(values["shared_python"])),
        Path(str(values["shared_python_resolved"])),
        str(values["shared_python_sha256"]),
        Path(str(values["shared_python"])).parent.parent,
        Path(str(values["pyvenv_cfg"])),
        str(values["pyvenv_sha256"]),
        Path(str(values["distribution_dir"])),
        Path(str(values["evaluator"])),
        str(values["evaluator_sha256"]),
        Path(str(values["universe"])),
        str(values["universe_sha256"]),
    )


def verify_configured_runtime_pins(pins: RuntimePins) -> None:
    """Compare current bytes with the exact identity captured at init."""
    current = runtime_pins(pins.snapshot_dir, pins.shared_python, pins.evaluator, pins.universe)
    if current != pins:
        raise ContainmentError("configured trusted runtime pin changed")


def file_sha256(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError as exc:
        raise ContainmentError(f"unable to hash trusted file: {path}") from exc


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise ContainmentError(f"missing {label}: {path}") from exc
    if os.path.islink(path):
        raise ContainmentError(f"symlinks are not allowed for {label}: {path}")
    return value


def require_regular(path: Path, label: str) -> Path:
    stat_value = _lstat(path, label)
    if not stat_value or not stat.S_ISREG(stat_value.st_mode):
        raise ContainmentError(f"{label} is not a regular file: {path}")
    return path.resolve()


def _snapshot_entries(root: Path) -> list[tuple[str, str, str]]:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ContainmentError(f"snapshot is unavailable: {root}") from exc
    if not root_stat or not os.path.isdir(root) or os.path.islink(root):
        raise ContainmentError(f"snapshot must be a directory: {root}")
    entries: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        stat = _lstat(path, "snapshot entry")
        if path.is_dir():
            entries.append((relative, "directory", ""))
        elif path.is_file():
            entries.append((relative, "regular", file_sha256(path)))
        elif stat:
            raise ContainmentError(f"snapshot entry is not regular or directory: {path}")
    return entries


def snapshot_sha256(root: Path) -> str:
    """Hash sorted relative names, file types, and regular-file contents."""
    required = (root / "src" / "quantipy", root / "pyproject.toml", root / "uv.lock")
    entries = _snapshot_entries(root)
    if (
        not required[0].is_dir()
        or os.path.islink(required[0])
        or not required[1].is_file()
        or os.path.islink(required[1])
        or not required[2].is_file()
        or os.path.islink(required[2])
    ):
        raise ContainmentError("snapshot must contain src/quantipy, pyproject.toml, and uv.lock")
    digest = hashlib.sha256()
    for relative, kind, content in entries:
        digest.update(f"{kind}\0{relative}\0{content}\n".encode())
    return digest.hexdigest()


def _pyvenv_home(cfg: Path) -> Path:
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("home") and "=" in line:
            value = line.split("=", 1)[1].strip()
            if value:
                candidate = Path(value)
                if not candidate.is_absolute():
                    raise ContainmentError(f"pyvenv.cfg home must be absolute: {cfg}")
                return candidate.resolve()
    raise ContainmentError(f"pyvenv.cfg has no absolute home: {cfg}")


def _distribution_dir(home: Path, resolved_python: Path) -> Path:
    candidate = home.parent if home.name == "bin" else home
    try:
        candidate_stat = candidate.lstat()
    except OSError as exc:
        raise ContainmentError("pyvenv distribution is unavailable") from exc
    if os.path.islink(candidate) or not stat.S_ISDIR(candidate_stat.st_mode):
        raise ContainmentError("pyvenv distribution is not a regular directory")
    try:
        resolved_python.relative_to(candidate)
    except ValueError as exc:
        raise ContainmentError("resolved Python is outside its pyvenv distribution") from exc
    return candidate


def runtime_pins(
    snapshot_dir: Path,
    shared_python: Path,
    evaluator: Path,
    universe: Path,
) -> RuntimePins:
    """Validate and snapshot immutable host runtime identity at ``init``."""
    snapshot = snapshot_dir.absolute()
    shared_python = shared_python.absolute()
    shared = shared_python.resolve()
    evaluator = require_regular(evaluator, "evaluator")
    universe = require_regular(universe, "operator universe")
    try:
        shared_stat = shared.stat()
    except OSError as exc:
        raise ContainmentError(f"shared interpreter is missing: {shared_python}") from exc
    if (
        not stat.S_ISREG(shared_stat.st_mode)
        or not shared.is_file()
        or not os.access(shared, os.X_OK)
    ):
        raise ContainmentError(f"shared interpreter is missing or not executable: {shared_python}")
    venv_dir = shared_python.parent.parent
    try:
        evaluator.relative_to(venv_dir.resolve())
    except ValueError as exc:
        raise ContainmentError(
            "evaluator must be the pinned console script in the shared venv"
        ) from exc
    pyvenv = shared_python.parent.parent / "pyvenv.cfg"
    require_regular(pyvenv, "pyvenv.cfg")
    home = _pyvenv_home(pyvenv)
    return RuntimePins(
        snapshot,
        snapshot_sha256(snapshot),
        shared_python,
        shared,
        file_sha256(shared),
        venv_dir,
        pyvenv,
        file_sha256(pyvenv),
        _distribution_dir(home, shared),
        evaluator,
        file_sha256(evaluator),
        universe,
        file_sha256(universe),
    )


def verify_runtime_pins(pins: RuntimePins) -> None:
    """Fail closed if any trusted source, interpreter, or universe changed."""
    current = runtime_pins(pins.snapshot_dir, pins.shared_python, pins.evaluator, pins.universe)
    if current != pins:
        raise ContainmentError("trusted runtime or input pin changed")


def _require_stage(stage: str) -> None:
    if stage not in _STAGES:
        raise ContainmentError(f"unknown containment stage: {stage}")


def _inside(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        return None


def rewrite_targets_argv(
    argv: Sequence[str],
    *,
    shared_python: Path,
    worktree: Path,
    run_dir: Path,
    panel: Path | None = None,
    receipt: Path | None = None,
) -> tuple[str, ...]:
    """Rewrite only known host paths into the target namespace."""
    if not argv or Path(argv[0]).resolve() != shared_python.resolve():
        raise ContainmentError("target argv does not start with the pinned interpreter")
    rewritten: list[str] = []
    for index, token in enumerate(argv):
        path = Path(token)
        if index == 0:
            rewritten.append(str(shared_python))
            continue
        if not path.is_absolute():
            rewritten.append(token)
            continue
        relative = _inside(path, worktree)
        if relative is not None:
            rewritten.append(str(Path("/work") / relative))
            continue
        relative = _inside(path, run_dir)
        if relative is not None:
            rewritten.append(str(Path("/stage") / relative))
            continue
        if panel is not None and path.absolute() == panel.absolute():
            rewritten.append("/inputs/panel.parquet")
            continue
        if receipt is not None and path.absolute() == receipt.absolute():
            rewritten.append("/inputs/receipt.json")
            continue
        raise ContainmentError(f"absolute target path is outside the fixed namespace: {token}")
    return tuple(rewritten)


def validate_targets_argv(
    argv: Sequence[str],
    *,
    shared_python: Path,
    worktree: Path,
    run_dir: Path,
    panel: Path | None = None,
    receipt: Path | None = None,
) -> None:
    """Validate the strategy shape before rewriting it into ``/work``/``/stage``."""
    if not argv or Path(argv[0]).resolve() != shared_python.resolve():
        raise ContainmentError("target argv does not start with the pinned interpreter")
    if len(argv) < 2:
        raise ContainmentError("target argv has no strategy")
    if argv[1] == "-m":
        if len(argv) < 3 or _DOTTED_MODULE.fullmatch(argv[2]) is None:
            raise ContainmentError("target module must be a dotted import name")
    elif not Path(argv[1]).is_absolute() or not Path(argv[1]).is_file():
        raise ContainmentError("target program must be an absolute file")
    for index, token in enumerate(argv):
        if index == 0:
            continue
        if _SECRET_HINT.search(token):
            raise ContainmentError("target arguments may not contain secret values or names")
        path = Path(token)
        if not path.is_absolute():
            continue
        if _inside(path, worktree) is not None or _inside(path, run_dir) is not None:
            continue
        if panel is not None and path.absolute() == panel.absolute():
            continue
        if receipt is not None and path.absolute() == receipt.absolute():
            continue
        raise ContainmentError(f"target path is outside the worktree/run directory: {token}")


def _add_parent_dirs(argv: list[str], path: Path, seen: set[str]) -> None:
    if not str(path).startswith("/home/"):
        return
    current = Path("/home")
    for part in path.relative_to("/home").parts[:-1]:
        current /= part
        if str(current) not in seen:
            argv.extend(("--dir", str(current)))
            seen.add(str(current))


def _base_argv(pins: RuntimePins) -> list[str]:
    if not Path(_BWRAP).is_file() or not Path(_SYSTEMD_RUN).is_file():
        raise ContainmentError("bubblewrap/systemd-run is unavailable")
    argv = [
        _BWRAP,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/home",
        "--ro-bind",
        str(pins.snapshot_dir),
        "/snapshot",
    ]
    seen: set[str] = set()
    _add_parent_dirs(argv, pins.venv_dir, seen)
    argv.extend(("--ro-bind", str(pins.venv_dir), str(pins.venv_dir)))
    if pins.distribution_dir not in {Path("/usr"), Path("/lib"), Path("/lib64")}:
        _add_parent_dirs(argv, pins.distribution_dir, seen)
        argv.extend(("--ro-bind", str(pins.distribution_dir), str(pins.distribution_dir)))
    argv.extend(
        (
            "--setenv",
            "PATH",
            f"{pins.shared_python.parent}:/usr/bin:/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "PYTHONPATH",
            "/snapshot/src",
            "--setenv",
            "OMP_NUM_THREADS",
            "1",
            "--setenv",
            "OPENBLAS_NUM_THREADS",
            "1",
            "--setenv",
            "MKL_NUM_THREADS",
            "1",
            "--setenv",
            "NUMEXPR_NUM_THREADS",
            "1",
        )
    )
    return argv


def bwrap_argv(
    pins: RuntimePins,
    stage: str,
    command: Sequence[str],
    *,
    worktree: Path | None = None,
    targets_stage: Path | None = None,
    evaluator_stage: Path | None = None,
    panel: Path | None = None,
    receipt: Path | None = None,
    spec: Path | None = None,
    dividends: Path | None = None,
    targets_file: Path | None = None,
) -> tuple[str, ...]:
    """Build the fixed bubblewrap command for one of the three stages."""
    _require_stage(stage)
    argv = _base_argv(pins)
    if stage in {"validate", "targets", "evaluate"}:
        if panel is None or receipt is None:
            raise ContainmentError("panel and receipt are required in every stage")
        argv.extend(("--dir", "/inputs", "--ro-bind", str(panel), "/inputs/panel.parquet"))
        argv.extend(("--ro-bind", str(receipt), "/inputs/receipt.json"))
    if stage in {"validate", "evaluate"}:
        if spec is None or dividends is None:
            raise ContainmentError("spec and dividends are required in validation/evaluation")
        argv.extend(("--ro-bind", str(spec), "/inputs/spec.json"))
        argv.extend(("--ro-bind", str(dividends), "/inputs/dividends.json"))
        argv.extend(("--ro-bind", str(pins.universe), "/universe.json"))
    if stage == "targets":
        if worktree is None or targets_stage is None:
            raise ContainmentError("target stage requires worktree and stage output")
        argv.extend(("--ro-bind", str(worktree), "/work", "--bind", str(targets_stage), "/stage"))
        argv.extend(("--chdir", "/work"))
    elif stage == "evaluate":
        if evaluator_stage is None or targets_file is None:
            raise ContainmentError("evaluation stage requires output and targets")
        argv.extend(("--ro-bind", str(targets_file), "/targets.json"))
        argv.extend(("--bind", str(evaluator_stage), "/stage", "--chdir", "/snapshot"))
    else:
        argv.extend(("--chdir", "/snapshot"))
    argv.extend(command)
    return tuple(argv)


def scope_unit(job_id: str, stage: str) -> str:
    _require_stage(stage)
    unit = f"research-{job_id}-{stage}"
    if _UNIT_RE.fullmatch(unit) is None:
        raise ContainmentError("job id cannot form a safe systemd unit name")
    return unit


def scope_argv(job_id: str, stage: str, max_rss_mb: int, bwrap: Sequence[str]) -> StagePlan:
    if max_rss_mb <= 0:
        raise ContainmentError("max RSS must be positive")
    unit = scope_unit(job_id, stage)
    command = (
        _SYSTEMD_RUN,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        f"--property=MemoryMax={max_rss_mb}M",
        "--property=MemorySwapMax=0",
        "--property=TasksMax=64",
        "--",
        *bwrap,
    )
    return StagePlan(stage, unit, command)


def stage_plan(
    pins: RuntimePins,
    job_id: str,
    stage: str,
    max_rss_mb: int,
    command: Sequence[str],
    **mounts: Path | None,
) -> StagePlan:
    return scope_argv(job_id, stage, max_rss_mb, bwrap_argv(pins, stage, command, **mounts))


def host_bus_environment() -> dict[str, str]:
    uid = os.getuid()
    return {
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }


def stop_scope(unit: str, signal_name: str = "SIGTERM") -> bool:
    """Stop the exact worker-owned scope and verify that it is inactive.

    The process-group kill remains a caller-owned fallback for the stage
    leader, but it cannot account for descendants that called ``setsid``.
    Therefore callers must treat ``False`` as an unverified containment
    failure and must not publish successful cancellation evidence.
    """
    base_unit = unit.removesuffix(".scope")
    if _UNIT_RE.fullmatch(base_unit) is None:
        return False
    scoped_unit = f"{base_unit}.scope"
    environment = host_bus_environment()
    try:
        subprocess.run(
            [
                _SYSTEMCTL,
                "--user",
                "kill",
                "--kill-who=all",
                f"--signal={signal_name}",
                scoped_unit,
            ],
            env=environment,
            check=False,
            timeout=5,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [_SYSTEMCTL, "--user", "stop", scoped_unit],
            env=environment,
            check=False,
            timeout=5,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    deadline = time.monotonic() + 5
    while True:
        try:
            loaded = subprocess.run(
                [
                    _SYSTEMCTL,
                    "--user",
                    "show",
                    "--property=LoadState",
                    "--value",
                    scoped_unit,
                ],
                env=environment,
                check=False,
                timeout=5,
                capture_output=True,
                text=True,
            )
            if loaded.returncode != 0:
                return False
            load_state = loaded.stdout.strip()
            if load_state == "not-found":
                return True
            active = subprocess.run(
                [
                    _SYSTEMCTL,
                    "--user",
                    "show",
                    "--property=ActiveState",
                    "--value",
                    scoped_unit,
                ],
                env=environment,
                check=False,
                timeout=5,
                capture_output=True,
                text=True,
            )
            if active.returncode != 0:
                return False
            if active.stdout.strip() in {"inactive", "failed", "dead"}:
                return True
        except (OSError, subprocess.TimeoutExpired):
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def target_file_ok(path: Path, max_bytes: int = _MAX_TARGET_BYTES) -> bool:
    try:
        stat_value = path.lstat()
        return (
            stat.S_ISREG(stat_value.st_mode)
            and not os.path.islink(path)
            and stat_value.st_size <= max_bytes
        )
    except OSError:
        return False


def which_tools() -> tuple[str, str]:
    """Expose fixed tool availability for diagnostics without accepting overrides."""
    return _BWRAP, _SYSTEMD_RUN
