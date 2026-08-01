"""Compute capability and fit artifacts for autoresearch."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from ctypes.util import find_library
from dataclasses import dataclass
from pathlib import Path

from gateway.autoresearch.constants import (
    DEFAULT_QUANTIPY_ROOT as DEFAULT_QUANTIPY_ROOT,
)
from gateway.autoresearch.enums import ComputeTarget as ComputeTarget
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import _ensure_mapping as _ensure_mapping
from gateway.autoresearch.fields import _require_exact_keys as _require_exact_keys
from gateway.autoresearch.fields import _require_str as _require_str
from gateway.autoresearch.fields import _require_string_list as _require_string_list


@dataclass(frozen=True, slots=True)
class StageAgentPolicy:
    agent_id: str
    model: str
    reasoning: str
    skills: tuple[str, ...]

    def to_summary(self) -> str:
        skill_text = ", ".join(self.skills)
        return f"{self.agent_id}: {self.model} / {self.reasoning} / skills=[{skill_text}]"


@dataclass(frozen=True, slots=True)
class ComputeCapabilitySnapshot:
    """Read-only host capability probe supplied to research stages."""

    cpu_model: str
    logical_cpus: int
    memory_gib: float | None
    target_python_available: bool
    gpu_available: bool
    gpu_name: str | None
    gpu_vram_gib: float | None
    cuda_runtime_available: bool
    installed_gpu_packages: tuple[str, ...]
    probe_errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "cpu_model": self.cpu_model,
            "logical_cpus": self.logical_cpus,
            "memory_gib": self.memory_gib,
            "target_python_available": self.target_python_available,
            "gpu_available": self.gpu_available,
            "gpu_name": self.gpu_name,
            "gpu_vram_gib": self.gpu_vram_gib,
            "cuda_runtime_available": self.cuda_runtime_available,
            "installed_gpu_packages": list(self.installed_gpu_packages),
            "probe_errors": list(self.probe_errors),
        }


@dataclass(frozen=True, slots=True)
class ComputeFitArtifact:
    """Machine-readable experiment compute choice and its justification."""

    target: ComputeTarget
    rationale: str
    required_dependencies: tuple[str, ...]
    benchmark_plan: str

    @classmethod
    def from_dict(cls, raw: object) -> ComputeFitArtifact:
        data = _ensure_mapping(raw, label="compute_fit")
        _require_exact_keys(
            data,
            label="compute_fit",
            expected=("target", "rationale", "required_dependencies", "benchmark_plan"),
        )
        artifact = cls(
            target=ComputeTarget(_require_str(data, "target")),
            rationale=_require_str(data, "rationale"),
            required_dependencies=_require_string_list(data, "required_dependencies"),
            benchmark_plan=_require_str(data, "benchmark_plan"),
        )
        artifact.validate()
        return artifact

    def validate(self) -> None:
        if not self.rationale.strip():
            raise AutoresearchValidationError("compute_fit rationale must be non-empty")
        if not self.benchmark_plan.strip():
            raise AutoresearchValidationError("compute_fit benchmark_plan must be non-empty")
        if self.target is ComputeTarget.NONE and self.required_dependencies:
            raise AutoresearchValidationError(
                "compute_fit target=none cannot require compute dependencies"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "rationale": self.rationale,
            "required_dependencies": list(self.required_dependencies),
            "benchmark_plan": self.benchmark_plan,
        }


_GPU_PROBE_MODULES = (
    "torch",
    "tensorflow",
    "jax",
    "cupy",
    "cudf",
    "xgboost",
    "lightgbm",
    "catboost",
)


def _probe_installed_gpu_packages(target_repo: Path, errors: list[str]) -> tuple[str, ...]:
    python = _target_python_path(target_repo)
    if not python.is_file():
        errors.append(f"Quantipy virtualenv not found: {python}")
        return ()
    code = (
        "from importlib.util import find_spec; "
        "mods=('torch','tensorflow','jax','cupy','cudf','xgboost','lightgbm','catboost'); "
        "print(' '.join(m for m in mods if find_spec(m) is not None))"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", code],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"GPU package probe failed: {type(exc).__name__}")
        return ()
    if result.returncode != 0:
        errors.append("GPU package probe returned a nonzero exit code")
        return ()
    return tuple(sorted(set(result.stdout.split()) & set(_GPU_PROBE_MODULES)))


def _target_python_path(target_repo: Path) -> Path:
    return target_repo.expanduser().resolve() / ".venv" / "bin" / "python"


def _read_memory_gib(errors: list[str]) -> float | None:
    try:
        memory_info = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"memory probe failed: {type(exc).__name__}")
        return None
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB$", memory_info, re.MULTILINE)
    if match is None:
        errors.append("memory probe returned no MemTotal")
        return None
    return round(int(match.group(1)) / 1024 / 1024, 2)


def _probe_nvidia(errors: list[str]) -> tuple[bool, str | None, float | None]:
    if shutil.which("nvidia-smi") is None:
        errors.append("nvidia-smi is not installed")
        return False, None, None
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"nvidia-smi probe failed: {type(exc).__name__}")
        return False, None, None
    if result.returncode != 0 or not result.stdout.strip():
        errors.append("nvidia-smi returned no usable GPU")
        return False, None, None
    name, _, memory = result.stdout.strip().splitlines()[0].partition(",")
    try:
        vram_gib = round(float(memory.strip()) / 1024, 2)
    except ValueError:
        errors.append("nvidia-smi returned an invalid memory value")
        return False, name.strip() or None, None
    return True, name.strip() or None, vram_gib


def _probe_cuda_runtime(gpu_available: bool, errors: list[str]) -> bool:
    if not gpu_available:
        return False
    if find_library("cuda") is None:
        errors.append("CUDA driver library is not available")
        return False
    return True


def collect_compute_capability_snapshot(
    target_repo: Path = DEFAULT_QUANTIPY_ROOT,
) -> ComputeCapabilitySnapshot:
    """Collect non-mutating host and target-venv compute capabilities."""
    errors: list[str] = []
    gpu_available, gpu_name, gpu_vram_gib = _probe_nvidia(errors)
    target_python_available = _target_python_path(target_repo).is_file()
    cuda_runtime_available = _probe_cuda_runtime(gpu_available, errors)
    return ComputeCapabilitySnapshot(
        cpu_model=platform.processor() or platform.machine() or "unknown",
        logical_cpus=os.cpu_count() or 1,
        memory_gib=_read_memory_gib(errors),
        target_python_available=target_python_available,
        gpu_available=gpu_available,
        gpu_name=gpu_name,
        gpu_vram_gib=gpu_vram_gib,
        cuda_runtime_available=cuda_runtime_available,
        installed_gpu_packages=_probe_installed_gpu_packages(target_repo, errors),
        probe_errors=tuple(errors),
    )
