"""Canonical Quantipy runtime attestation and verification dispatch guards."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from gateway.autoresearch import attestation as attestation_module
from gateway.autoresearch import persistence as persistence_module
from gateway.autoresearch import transitions as transitions_module
from gateway.autoresearch.artifacts import (
    FixResultArtifact as FixResultArtifact,
)
from gateway.autoresearch.artifacts import (
    ImplementationResultArtifact as ImplementationResultArtifact,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES as CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_PYPROJECT_MAX_BYTES as CANONICAL_QUANTIPY_PYPROJECT_MAX_BYTES,
)
from gateway.autoresearch.constants import (
    CANONICAL_QUANTIPY_UV_LOCK_MAX_BYTES as CANONICAL_QUANTIPY_UV_LOCK_MAX_BYTES,
)
from gateway.autoresearch.enums import (
    Phase as Phase,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.evidence import (
    _git_show_committed_bytes as _git_show_committed_bytes,
)
from gateway.autoresearch.evidence import (
    _target_repo_root_for_state as _target_repo_root_for_state,
)
from gateway.autoresearch.fields import (
    _require_str as _require_str,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)
from gateway.autoresearch.gitops import (
    _require_git_descends_from as _require_git_descends_from,
)
from gateway.autoresearch.gitops import (
    _require_git_output as _require_git_output,
)
from gateway.autoresearch.gitops import (
    _require_git_worktree_root as _require_git_worktree_root,
)
from gateway.autoresearch.gitops import (
    _resolve_git_commit as _resolve_git_commit,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy as AutoresearchPolicy,
)
from gateway.autoresearch.recovery_receipts import (
    CanonicalQuantipyRuntimeAttestation as CanonicalQuantipyRuntimeAttestation,
)
from gateway.autoresearch.secure_io import (
    _require_canonical_absolute_path as _require_canonical_absolute_path,
)
from gateway.autoresearch.secure_io import (
    _require_runtime_venv_prefix as _require_runtime_venv_prefix,
)
from gateway.autoresearch.secure_io import (
    _secure_open_external_uv_base_interpreter as _secure_open_external_uv_base_interpreter,
)
from gateway.autoresearch.secure_io import (
    _secure_open_snapshot as _secure_open_snapshot,
)
from gateway.autoresearch.secure_io import (
    _SecureFileSnapshot as _SecureFileSnapshot,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.state import (
    AutoresearchValidationContext as AutoresearchValidationContext,
)
from gateway.autoresearch.transitions import (
    build_authoritative_state_reference as build_authoritative_state_reference,
)


def _probe_quantipy_runtime_resolution(runtime_root: Path) -> tuple[Path, Path, str]:
    """Resolve the frozen uv executable and Quantipy import without changing the runtime."""
    command = (
        "uv",
        "--directory",
        str(runtime_root),
        "run",
        "--frozen",
        "--no-sync",
        "python",
        "-c",
        (
            "import json, pathlib, quantipy, sys; "
            "print(json.dumps({'base_interpreter': str(pathlib.Path(sys.executable).resolve()), "
            "'base_interpreter_version': '.'.join(map(str, sys.version_info[:3])), "
            "'import_path': str(pathlib.Path(quantipy.__file__).resolve())}, sort_keys=True))"
        ),
    )
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise AutoresearchValidationError(
            "canonical Quantipy runtime uv resolution failed"
        ) from exc
    if result.returncode != 0:
        raise AutoresearchValidationError("canonical Quantipy runtime uv resolution failed")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AutoresearchValidationError(
            "canonical Quantipy runtime resolution did not produce JSON"
        ) from exc
    if not isinstance(raw, Mapping) or set(raw) != {
        "base_interpreter",
        "base_interpreter_version",
        "import_path",
    }:
        raise AutoresearchValidationError("canonical Quantipy runtime resolution is invalid")
    base_interpreter = _require_canonical_absolute_path(
        _require_str(raw, "base_interpreter"), label="canonical Quantipy runtime base interpreter"
    )
    import_path = _require_canonical_absolute_path(
        _require_str(raw, "import_path"), label="canonical Quantipy runtime import"
    )
    version = _require_str(raw, "base_interpreter_version")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise AutoresearchValidationError(
            "canonical Quantipy runtime base interpreter version is invalid"
        )
    return base_interpreter, import_path, version


def _attest_canonical_quantipy_runtime(
    state: AutoresearchState,
    implementation: ImplementationResultArtifact,
    *,
    readiness_quantipy_commit: str | None = None,
) -> CanonicalQuantipyRuntimeAttestation:
    """Fail closed unless uv's project runtime and source bind one pinned commit."""
    if state.setup is None:
        raise AutoresearchValidationError("canonical Quantipy runtime requires setup target_repo")
    runtime_root = _require_git_worktree_root(
        Path(state.setup.target_repo).expanduser(), label="canonical Quantipy runtime root"
    )
    if runtime_root != _target_repo_root_for_state(state):
        raise AutoresearchValidationError(
            "canonical Quantipy runtime root must equal state target_repo"
        )
    status = _require_git_output(
        runtime_root,
        ("status", "--porcelain=v1", "--untracked-files=no"),
        operation="canonical Quantipy runtime tracked-file status check",
    )
    if status:
        raise AutoresearchValidationError("canonical Quantipy runtime has dirty tracked files")
    runtime_commit = _resolve_git_commit(
        runtime_root, "HEAD", label="canonical Quantipy runtime HEAD"
    )
    implementation_workspace = _require_git_worktree_root(
        Path(implementation.workspace_path), label="implementation_result workspace_path"
    )
    implementation_commit = _resolve_git_commit(
        implementation_workspace,
        implementation.commit_sha,
        label="implementation_result commit_sha",
    )
    readiness_commit = (
        readiness_quantipy_commit
        if readiness_quantipy_commit is not None
        else (state.platform_readiness.quantipy_commit if state.platform_readiness else None)
    )
    if readiness_commit is None:
        raise AutoresearchValidationError(
            "canonical Quantipy runtime requires the readiness-pinned Quantipy commit"
        )
    _require_git_descends_from(
        runtime_root, readiness_commit, runtime_commit, label="canonical Quantipy runtime"
    )
    _require_git_descends_from(
        implementation_workspace,
        readiness_commit,
        implementation_commit,
        label="implementation_result commit_sha",
    )
    snapshots: dict[str, _SecureFileSnapshot] = {}
    for filename, max_bytes in (
        ("pyproject.toml", CANONICAL_QUANTIPY_PYPROJECT_MAX_BYTES),
        ("uv.lock", CANONICAL_QUANTIPY_UV_LOCK_MAX_BYTES),
    ):
        snapshot = _secure_open_snapshot(
            runtime_root / filename,
            label=f"canonical Quantipy runtime {filename}",
            allow_group_write=True,
            max_bytes=max_bytes,
        )
        if snapshot.content != _git_show_committed_bytes(
            runtime_root, runtime_commit, Path(filename)
        ):
            raise AutoresearchValidationError(
                f"canonical Quantipy runtime {filename} must match runtime commit exactly"
            )
        snapshots[filename] = snapshot
    venv_prefix = runtime_root / ".venv"
    _require_runtime_venv_prefix(venv_prefix)
    entrypoint = _secure_open_snapshot(
        venv_prefix / "bin" / "quantipy",
        label="canonical Quantipy runtime .venv quantipy entrypoint",
        allow_group_write=True,
        max_bytes=CANONICAL_QUANTIPY_ENTRYPOINT_MAX_BYTES,
    )
    base_interpreter, import_path, base_interpreter_version = (
        attestation_module._probe_quantipy_runtime_resolution(runtime_root)
    )
    import_package = import_path.parent
    if import_package != runtime_root / "src" / "quantipy":
        raise AutoresearchValidationError(
            "canonical Quantipy runtime import must resolve from root/src/quantipy"
        )
    base_snapshot = _secure_open_external_uv_base_interpreter(base_interpreter)
    return CanonicalQuantipyRuntimeAttestation(
        root=str(runtime_root),
        commit_sha=runtime_commit,
        readiness_quantipy_commit=readiness_commit,
        pyproject_sha256=snapshots["pyproject.toml"].sha256,
        uv_lock_sha256=snapshots["uv.lock"].sha256,
        venv_prefix=str(venv_prefix),
        executable_path=str(entrypoint.path),
        executable_sha256=entrypoint.sha256,
        executable_size_bytes=len(entrypoint.content),
        executable_mode=entrypoint.mode,
        executable_owner_uid=entrypoint.owner_uid,
        import_path=str(import_package),
        base_interpreter_path=str(base_snapshot.path),
        base_interpreter_version=base_interpreter_version,
        base_interpreter_sha256=base_snapshot.sha256,
        base_interpreter_size_bytes=len(base_snapshot.content),
        base_interpreter_mode=base_snapshot.mode,
        base_interpreter_owner_uid=base_snapshot.owner_uid,
    )


def _require_canonical_verification_runtime_attestation(
    state: AutoresearchState,
    *,
    validation_context: AutoresearchValidationContext | None,
) -> None:
    """Reattest the runtime sealed for the currently dispatched verification."""
    if state.phase is not Phase.VERIFICATION:
        return
    implementation = state.implementation_result
    attestation = state.canonical_quantipy_runtime_attestation
    if implementation is None or attestation is None:
        raise AutoresearchValidationError(
            "canonical verification dispatch requires a sealed runtime attestation"
        )
    if validation_context is None or validation_context.quantipy_commit is None:
        raise AutoresearchValidationError(
            "canonical verification runtime attestation requires the readiness-pinned "
            "Quantipy commit"
        )
    current = attestation_module._attest_canonical_quantipy_runtime(
        state,
        implementation,
        readiness_quantipy_commit=validation_context.quantipy_commit,
    )
    if (
        current != attestation
        or current.readiness_quantipy_commit != validation_context.quantipy_commit
    ):
        raise AutoresearchValidationError(
            "canonical verification runtime attestation changed after dispatch"
        )


def seal_canonical_verification_dispatch_state_file(
    state_path: Path,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState:
    """Atomically seal the runtime identity before dispatching a verification agent."""
    resolved_path = state_path.expanduser().resolve(strict=False)
    with persistence_module._exclusive_state_locks((resolved_path,)):
        state = persistence_module.load_state_file(resolved_path)
        transitions_module._validate_state(state, policy, validation_context)
        if state.phase is not Phase.VERIFICATION or state.implementation_result is None:
            raise AutoresearchValidationError(
                "canonical verification runtime sealing requires a verification implementation"
            )
        if validation_context is None or validation_context.quantipy_commit is None:
            raise AutoresearchValidationError(
                "canonical verification runtime sealing requires the readiness-pinned "
                "Quantipy commit"
            )
        transitions_module.validate_artifact_workspace(state, state.implementation_result)
        attestation = attestation_module._attest_canonical_quantipy_runtime(
            state,
            state.implementation_result,
            readiness_quantipy_commit=validation_context.quantipy_commit,
        )
        sealed = replace(state, canonical_quantipy_runtime_attestation=attestation)
        attestation_module._require_canonical_verification_runtime_attestation(
            sealed,
            validation_context=validation_context,
        )
        if sealed != state:
            # An unchanged attestation must not rewrite the file: the state
            # mtime is the supervisor's staleness clock, and an every-cycle
            # touch would suppress idle-recovery nudging indefinitely.
            persistence_module._atomic_save_state_file(resolved_path, sealed)
        return sealed


def require_canonical_verification_dispatch_attestation(
    state_path: Path,
    *,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
    expected_state_reference_sha256: str | None = None,
) -> AutoresearchState:
    """Read-only production guard for a verification dispatch or result publication."""
    resolved_path = state_path.expanduser().resolve(strict=False)
    with persistence_module._exclusive_state_locks((resolved_path,)):
        state = persistence_module.load_state_file(resolved_path)
        transitions_module._validate_state(state, policy, validation_context)
        attestation_module._require_canonical_verification_runtime_attestation(
            state,
            validation_context=validation_context,
        )
        if state.implementation_result is not None:
            transitions_module.validate_artifact_workspace(state, state.implementation_result)
        if expected_state_reference_sha256 is not None:
            _validate_sha256(
                expected_state_reference_sha256,
                label="expected_state_reference_sha256",
            )
            current_reference = build_authoritative_state_reference(
                state,
                state_path=resolved_path,
            ).sha256()
            if current_reference != expected_state_reference_sha256:
                raise AutoresearchValidationError(
                    "canonical verification state reference changed before dispatch"
                )
        return state
