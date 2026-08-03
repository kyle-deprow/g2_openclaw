"""State persistence, locking, and artifact publication boundaries."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from gateway.autoresearch import configuration as configuration_module
from gateway.autoresearch import constants
from gateway.autoresearch import manifest_runtime as manifest_runtime_module
from gateway.autoresearch import persistence as persistence_module
from gateway.autoresearch import transitions as transitions_module
from gateway.autoresearch.artifacts import (
    ConsensusResultArtifact as ConsensusResultArtifact,
)
from gateway.autoresearch.artifacts import (
    ContextPacketArtifact as ContextPacketArtifact,
)
from gateway.autoresearch.artifacts import (
    DebateResultArtifact as DebateResultArtifact,
)
from gateway.autoresearch.artifacts import (
    FinalDecisionArtifact as FinalDecisionArtifact,
)
from gateway.autoresearch.artifacts import (
    FixResultArtifact as FixResultArtifact,
)
from gateway.autoresearch.artifacts import (
    ImplementationResultArtifact as ImplementationResultArtifact,
)
from gateway.autoresearch.artifacts import (
    ReviewResultArtifact as ReviewResultArtifact,
)
from gateway.autoresearch.artifacts import (
    SetupContextArtifact as SetupContextArtifact,
)
from gateway.autoresearch.artifacts import (
    VerificationResultArtifact as VerificationResultArtifact,
)
from gateway.autoresearch.enums import (
    ArtifactType as ArtifactType,
)
from gateway.autoresearch.errors import (
    AutoresearchValidationError as AutoresearchValidationError,
)
from gateway.autoresearch.fields import (
    _ensure_mapping as _ensure_mapping,
)
from gateway.autoresearch.fields import (
    _require_exact_keys as _require_exact_keys,
)
from gateway.autoresearch.fields import (
    _require_int as _require_int,
)
from gateway.autoresearch.fields import (
    _require_sha256 as _require_sha256,
)
from gateway.autoresearch.fields import (
    _validate_sha256 as _validate_sha256,
)
from gateway.autoresearch.manifest import (
    AuthoritativeStateReference as AuthoritativeStateReference,
)
from gateway.autoresearch.policy import (
    AutoresearchPolicy as AutoresearchPolicy,
)
from gateway.autoresearch.policy import (
    ReceiptCatalog as ReceiptCatalog,
)
from gateway.autoresearch.prompts import (
    _select_phase_target as _select_phase_target,
)
from gateway.autoresearch.state import (
    AutoresearchState as AutoresearchState,
)
from gateway.autoresearch.state import (
    AutoresearchValidationContext as AutoresearchValidationContext,
)
from gateway.autoresearch_readiness import (
    PlatformReadinessManifest as PlatformReadinessManifest,
)


def load_artifact_file(
    path: Path,
    state: AutoresearchState,
    policy: AutoresearchPolicy,
    *,
    instruction_manifest_sha256: str,
    validation_context: AutoresearchValidationContext | None = None,
    state_reference_sha256: str | None = None,
    state_path: Path = constants.DEFAULT_AUTORESEARCH_STATE_PATH,
) -> (
    SetupContextArtifact
    | ContextPacketArtifact
    | DebateResultArtifact
    | ConsensusResultArtifact
    | ImplementationResultArtifact
    | VerificationResultArtifact
    | ReviewResultArtifact
    | FixResultArtifact
    | FinalDecisionArtifact
):
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as exc:
        raise AutoresearchValidationError(f"missing artifact file: {path}") from exc
    if len(raw_bytes) > constants.MAX_ARTIFACT_FILE_BYTES:
        raise AutoresearchValidationError(
            "artifact file exceeds hard byte budget: "
            f"{len(raw_bytes)} > {constants.MAX_ARTIFACT_FILE_BYTES} bytes; compact the "
            "phase artifact and write the strict manifest/state-reference envelope"
        )
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise AutoresearchValidationError(f"artifact JSON is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchValidationError(f"invalid artifact JSON: {path}") from exc

    _validate_sha256(instruction_manifest_sha256, label="instruction_manifest_sha256")
    if state_reference_sha256 is not None:
        _validate_sha256(state_reference_sha256, label="state_reference_sha256")
    data = _ensure_mapping(raw, label="artifact_file")
    _require_exact_keys(
        data,
        label="artifact_file",
        expected=("instruction_manifest_sha256", "state_reference_sha256", "artifact"),
    )
    envelope_digest = _require_sha256(data, "instruction_manifest_sha256")
    if envelope_digest != instruction_manifest_sha256:
        raise AutoresearchValidationError(
            "artifact instruction_manifest_sha256 does not match dispatched manifest"
        )
    state = transitions_module._validate_persisted_state_matches(state, state_path=state_path)
    expected_state_reference_sha256 = transitions_module.build_authoritative_state_reference(
        state,
        state_path=state_path,
    ).sha256()
    envelope_state_reference_sha256 = _require_sha256(data, "state_reference_sha256")
    if (
        state_reference_sha256 is not None
        and envelope_state_reference_sha256 != state_reference_sha256
    ):
        raise AutoresearchValidationError(
            "artifact state_reference_sha256 does not match dispatched state reference"
        )
    if envelope_state_reference_sha256 != expected_state_reference_sha256:
        raise AutoresearchValidationError(
            "artifact state_reference_sha256 does not match the current authoritative state"
        )
    artifact_raw = data["artifact"]

    transitions_module._validate_state(state, policy, validation_context)
    target = _select_phase_target(state, policy)
    if target.artifact_type is ArtifactType.SETUP:
        return SetupContextArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.CONTEXT_PACKET:
        return ContextPacketArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.DEBATE_RESULT:
        return DebateResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.CONSENSUS_RESULT:
        return ConsensusResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.IMPLEMENTATION_RESULT:
        return ImplementationResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.VERIFICATION_RESULT:
        return VerificationResultArtifact.from_dict(artifact_raw, mode=state.mode)
    if target.artifact_type is ArtifactType.REVIEW_RESULT:
        return ReviewResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.FIX_RESULT:
        return FixResultArtifact.from_dict(artifact_raw)
    if target.artifact_type is ArtifactType.FINAL_DECISION:
        return FinalDecisionArtifact.from_dict(artifact_raw)
    raise AutoresearchValidationError(
        f"{state.phase.value} does not accept artifact files; use state mutation commands instead"
    )


def load_state_file(path: Path) -> AutoresearchState:
    return AutoresearchState.from_dict(_load_state_raw(path))


def _load_state_raw(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutoresearchValidationError(f"missing state file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutoresearchValidationError(f"invalid state JSON: {path}") from exc
    return _ensure_mapping(raw, label="autoresearch_state")


def initialize_state(readiness: PlatformReadinessManifest) -> AutoresearchState:
    """Create a pristine v4 campaign state pinned to authoritative readiness."""
    try:
        identity = readiness.require_ready()
    except ValueError as exc:
        raise AutoresearchValidationError(str(exc)) from exc
    return AutoresearchState(platform_readiness=identity)


def save_state_file(path: Path, state: AutoresearchState) -> None:
    resolved_path = path.expanduser().resolve(strict=False)
    with persistence_module._exclusive_state_locks((resolved_path,)):
        persistence_module._atomic_save_state_file(resolved_path, state)


def _canonical_state_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    canonical_paths = {path.expanduser().resolve(strict=False) for path in paths}
    lock_namespace = _prepare_lock_namespace()
    for path in canonical_paths:
        if path == lock_namespace or lock_namespace in path.parents:
            raise AutoresearchValidationError(
                f"autoresearch state path cannot be inside lock namespace: {path}"
            )
    return tuple(sorted(canonical_paths, key=os.fspath))


def _prepare_lock_namespace() -> Path:
    namespace_path = constants.AUTORESEARCH_LOCK_NAMESPACE.expanduser().absolute()
    try:
        namespace_path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            f"unable to create autoresearch lock namespace: {namespace_path}"
        ) from exc
    try:
        namespace_stat = namespace_path.lstat()
    except OSError as exc:
        raise AutoresearchValidationError(
            f"unable to inspect autoresearch lock namespace: {namespace_path}"
        ) from exc
    if not stat.S_ISDIR(namespace_stat.st_mode):
        raise AutoresearchValidationError(
            f"autoresearch lock namespace is not a directory: {namespace_path}"
        )
    if namespace_stat.st_uid != os.getuid():
        raise AutoresearchValidationError(
            f"autoresearch lock namespace has wrong owner: {namespace_path}"
        )
    if stat.S_IMODE(namespace_stat.st_mode) != 0o700:
        raise AutoresearchValidationError(
            f"autoresearch lock namespace permissions must be 0700: {namespace_path}"
        )
    return namespace_path.resolve(strict=True)


def _state_lock_path(state_path: Path) -> Path:
    canonical_state_path = state_path.expanduser().resolve(strict=False)
    lock_namespace = constants.AUTORESEARCH_LOCK_NAMESPACE.expanduser().absolute()
    state_path_digest = hashlib.sha256(
        "\n".join(
            (constants.AUTORESEARCH_STATE_LOCK_DIGEST_DOMAIN, os.fspath(canonical_state_path))
        ).encode("utf-8")
    ).hexdigest()
    return lock_namespace / f"{state_path_digest}.lock"


def _open_state_lock_file(lock_path: Path) -> int:
    create_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        lock_fd = os.open(lock_path, create_flags, 0o600)
    except FileExistsError:
        try:
            return os.open(
                lock_path,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise AutoresearchValidationError(
                f"unable to open autoresearch state lock: {lock_path}"
            ) from exc
    except OSError as exc:
        raise AutoresearchValidationError(
            f"unable to open autoresearch state lock: {lock_path}"
        ) from exc
    try:
        os.fchmod(lock_fd, 0o600)
    except OSError as exc:
        os.close(lock_fd)
        raise AutoresearchValidationError(
            f"unable to secure autoresearch state lock: {lock_path}"
        ) from exc
    return lock_fd


@contextmanager
def _exclusive_state_locks(paths: Sequence[Path]) -> Iterator[None]:
    """Lock canonical state paths once in deterministic order."""
    with ExitStack() as stack:
        for path in _canonical_state_paths(paths):
            stack.enter_context(persistence_module._exclusive_state_lock(path))
        yield


@contextmanager
def _exclusive_state_lock(state_path: Path) -> Iterator[None]:
    """Serialize access to one canonical state path across CLI processes."""
    resolved_state_path = state_path.expanduser().resolve(strict=False)
    _prepare_lock_namespace()
    lock_path = _state_lock_path(resolved_state_path)
    lock_fd = _open_state_lock_file(lock_path)
    try:
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise AutoresearchValidationError(
                f"autoresearch state lock is not a regular file: {lock_path}"
            )
        if lock_stat.st_uid != os.getuid():
            raise AutoresearchValidationError(
                f"autoresearch state lock has wrong owner: {lock_path}"
            )
        if stat.S_IMODE(lock_stat.st_mode) != 0o600:
            raise AutoresearchValidationError(
                f"autoresearch state lock permissions must be 0600: {lock_path}"
            )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except AutoresearchValidationError:
        os.close(lock_fd)
        raise
    except OSError as exc:
        os.close(lock_fd)
        raise AutoresearchValidationError(
            f"unable to lock autoresearch state: {resolved_state_path}"
        ) from exc
    try:
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def persist_derived_state(
    source_path: Path,
    output_path: Path,
    source_state: AutoresearchState,
    derived_state: AutoresearchState,
    *,
    policy: AutoresearchPolicy | None = None,
    validation_context: AutoresearchValidationContext | None = None,
) -> None:
    """Atomically publish a derived state only while its source remains authorized.

    The source path, rather than a prior derived output, is always compared while
    both canonical paths are locked. When the paths are equal, the verified source
    is atomically replaced. When they differ, the source is left untouched and only
    the derived output is atomically replaced.
    """
    resolved_source_path = source_path.expanduser().resolve(strict=False)
    resolved_output_path = output_path.expanduser().resolve(strict=False)
    expected_reference = transitions_module.build_authoritative_state_reference(
        source_state,
        state_path=resolved_source_path,
    )
    candidate_policy = policy or configuration_module.load_autoresearch_policy(
        constants.DEFAULT_OPENCLAW_CONFIG_PATH
    )
    transitions_module._validate_state(derived_state, candidate_policy, validation_context)
    with persistence_module._exclusive_state_locks((resolved_source_path, resolved_output_path)):
        persisted_state = persistence_module.load_state_file(resolved_source_path)
        persisted_reference = transitions_module.build_authoritative_state_reference(
            persisted_state,
            state_path=resolved_source_path,
        )
        if persisted_reference != expected_reference:
            raise AutoresearchValidationError(
                "persisted state does not match the supplied authoritative state"
            )
        transitions_module._validate_state(derived_state, candidate_policy, validation_context)
        persistence_module._atomic_save_state_file(resolved_output_path, derived_state)


@dataclass(frozen=True, slots=True)
class _ArtifactAdvancePublicationGuard:
    source_path: Path
    artifact_path: Path
    instruction_manifest_sha256: str
    policy: AutoresearchPolicy
    validation_context: AutoresearchValidationContext | None
    state_reference_sha256: str | None
    expected_source_reference: AuthoritativeStateReference
    expected_candidate: AutoresearchState


def _revalidate_artifact_advance_for_atomic_publication(
    guard: _ArtifactAdvancePublicationGuard,
) -> None:
    """Re-derive every mutable input after temp-file fsync and before replace."""
    locked_state = persistence_module.load_state_file(guard.source_path)
    from gateway.autoresearch import attestation as attestation_module

    attestation_module._require_canonical_verification_runtime_attestation(
        locked_state,
        validation_context=guard.validation_context,
    )
    locked_reference = transitions_module.build_authoritative_state_reference(
        locked_state,
        state_path=guard.source_path,
    )
    if locked_reference != guard.expected_source_reference:
        raise AutoresearchValidationError(
            "persisted state does not match the supplied authoritative state"
        )
    locked_artifact = persistence_module.load_artifact_file(
        guard.artifact_path,
        locked_state,
        guard.policy,
        instruction_manifest_sha256=guard.instruction_manifest_sha256,
        validation_context=guard.validation_context,
        state_reference_sha256=guard.state_reference_sha256,
        state_path=guard.source_path,
    )
    if isinstance(locked_artifact, ImplementationResultArtifact | FixResultArtifact):
        transitions_module.validate_artifact_workspace(locked_state, locked_artifact)
    locked_candidate = transitions_module.advance_state(
        locked_state,
        locked_artifact,
        guard.policy,
        validation_context=guard.validation_context,
        state_path=guard.source_path,
    )
    if locked_candidate != guard.expected_candidate:
        raise AutoresearchValidationError("artifact changed before state publication")


def advance_artifact_state_file(
    *,
    state_path: Path,
    output_path: Path,
    artifact_path: Path,
    instruction_manifest_sha256: str,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
    state_reference_sha256: str | None = None,
) -> AutoresearchState:
    """Advance a dispatched artifact twice, publishing only the locked re-derivation.

    Artifact bytes and the detached runtime evidence are mutable external inputs.
    The first derivation gives a useful fail-fast result; the second occurs while
    the state-file publication lock is held and must produce the identical state.
    """
    resolved_state_path = state_path.expanduser().resolve(strict=False)
    resolved_output_path = output_path.expanduser().resolve(strict=False)
    resolved_artifact_path = artifact_path.expanduser().resolve(strict=False)
    source_state = persistence_module.load_state_file(resolved_state_path)
    from gateway.autoresearch import attestation as attestation_module

    attestation_module._require_canonical_verification_runtime_attestation(
        source_state,
        validation_context=validation_context,
    )
    expected_reference = transitions_module.build_authoritative_state_reference(
        source_state,
        state_path=resolved_state_path,
    )
    artifact = persistence_module.load_artifact_file(
        resolved_artifact_path,
        source_state,
        policy,
        instruction_manifest_sha256=instruction_manifest_sha256,
        validation_context=validation_context,
        state_reference_sha256=state_reference_sha256,
        state_path=resolved_state_path,
    )
    if isinstance(artifact, ImplementationResultArtifact | FixResultArtifact):
        transitions_module.validate_artifact_workspace(source_state, artifact)
    candidate = transitions_module.advance_state(
        source_state,
        artifact,
        policy,
        validation_context=validation_context,
        state_path=resolved_state_path,
    )
    with persistence_module._exclusive_state_locks((resolved_state_path, resolved_output_path)):
        publication_guard = _ArtifactAdvancePublicationGuard(
            source_path=resolved_state_path,
            artifact_path=resolved_artifact_path,
            instruction_manifest_sha256=instruction_manifest_sha256,
            policy=policy,
            validation_context=validation_context,
            state_reference_sha256=state_reference_sha256,
            expected_source_reference=expected_reference,
            expected_candidate=candidate,
        )
        persistence_module._atomic_save_state_file(
            resolved_output_path,
            candidate,
            publication_guard=publication_guard,
        )
        return candidate


def submit_stage_artifact_file(
    *,
    state_path: Path,
    artifact_path: Path,
    inbox_path: Path = constants.DEFAULT_AUTORESEARCH_STAGE_INBOX,
    instruction_manifest_sha256: str,
    policy: AutoresearchPolicy,
    validation_context: AutoresearchValidationContext | None,
    state_reference_sha256: str | None = None,
) -> Path:
    """Validate a model-produced artifact and publish only its envelope to the inbox.

    This is the model-writable boundary. It never writes authoritative state; the
    supervisor/controller later re-derives the same transition under state locks.
    """
    resolved_state_path = state_path.expanduser().resolve(strict=False)
    resolved_artifact_path = artifact_path.expanduser().resolve(strict=False)
    configured_inbox_path = inbox_path.expanduser()
    source_state = persistence_module.load_state_file(resolved_state_path)
    from gateway.autoresearch import attestation as attestation_module

    attestation_module._require_canonical_verification_runtime_attestation(
        source_state,
        validation_context=validation_context,
    )
    expected_reference = transitions_module.build_authoritative_state_reference(
        source_state,
        state_path=resolved_state_path,
    )
    artifact = persistence_module.load_artifact_file(
        resolved_artifact_path,
        source_state,
        policy,
        instruction_manifest_sha256=instruction_manifest_sha256,
        validation_context=validation_context,
        state_reference_sha256=state_reference_sha256,
        state_path=resolved_state_path,
    )
    if isinstance(artifact, ImplementationResultArtifact | FixResultArtifact):
        transitions_module.validate_artifact_workspace(source_state, artifact)
    transitions_module.advance_state(
        source_state,
        artifact,
        policy,
        validation_context=validation_context,
        state_path=resolved_state_path,
    )
    envelope = resolved_artifact_path.read_bytes()
    if len(envelope) > constants.MAX_STAGE_SUBMISSION_BYTES:
        raise AutoresearchValidationError(
            "stage submission exceeds hard byte budget: "
            f"{len(envelope)} > {constants.MAX_STAGE_SUBMISSION_BYTES} bytes"
        )
    artifact_sha256 = hashlib.sha256(envelope).hexdigest()
    filename = (
        f"{source_state.iteration:04d}-{source_state.phase.value}-"
        f"{expected_reference.sha256()[:16]}-{artifact_sha256[:16]}.json"
    )
    output_path = configured_inbox_path / filename
    try:
        configured_inbox_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        inbox_fd = os.open(
            configured_inbox_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except FileExistsError:
        raise
    except OSError as exc:
        raise AutoresearchValidationError(f"failed to open stage submission inbox: {exc}") from exc
    try:
        _validate_stage_inbox_directory_fd(inbox_fd, label="stage submission inbox")
        try:
            output_fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=inbox_fd,
            )
        except FileExistsError:
            try:
                existing_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=inbox_fd)
            except OSError as exc:
                raise AutoresearchValidationError(
                    f"failed to inspect existing stage submission artifact: {exc}"
                ) from exc
            try:
                existing = os.read(existing_fd, constants.MAX_STAGE_SUBMISSION_BYTES + 1)
            finally:
                os.close(existing_fd)
            if hashlib.sha256(existing).hexdigest() != artifact_sha256:
                raise AutoresearchValidationError(
                    "stage submission filename collision with different artifact"
                ) from None
        except OSError as exc:
            raise AutoresearchValidationError(
                f"failed to write stage submission inbox artifact: {exc}"
            ) from exc
        else:
            try:
                written = 0
                while written < len(envelope):
                    written += os.write(output_fd, envelope[written:])
                os.fsync(output_fd)
            finally:
                os.close(output_fd)
    finally:
        os.close(inbox_fd)
    return output_path


def _validate_stage_inbox_directory_fd(fd: int, *, label: str) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise AutoresearchValidationError(f"{label} must be a plain directory")
    if metadata.st_uid != os.getuid():
        raise AutoresearchValidationError(f"{label} must be owned by the current user")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AutoresearchValidationError(f"{label} must not be group/world writable")
    return metadata


def _open_stage_inbox_child_directory(inbox_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=inbox_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            f"cannot create stage submission {name} directory: {exc}"
        ) from exc
    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=inbox_fd,
        )
    except OSError as exc:
        raise AutoresearchValidationError(
            f"stage submission {name} path must be a plain directory: {exc}"
        ) from exc
    try:
        _validate_stage_inbox_directory_fd(child_fd, label=f"stage submission {name} directory")
    except Exception:
        os.close(child_fd)
        raise
    return child_fd


def _move_stage_inbox_entry_no_replace(
    name: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    for attempt in range(128):
        target = name
        if attempt:
            digest = hashlib.sha256(f"{name}\n{time.time_ns()}\n{attempt}".encode()).hexdigest()
            target = f"{name}.{digest[:16]}"
        try:
            os.link(
                name,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise AutoresearchValidationError(
                f"cannot quarantine stage submission artifact: {exc}"
            ) from exc
        os.unlink(name, dir_fd=src_dir_fd)
        return
    raise AutoresearchValidationError(
        "cannot quarantine stage submission artifact without overwrite"
    )


def consume_stage_submission_inbox(
    *,
    state_path: Path,
    output_path: Path,
    inbox_path: Path,
    openclaw_config: Path = constants.DEFAULT_OPENCLAW_CONFIG_PATH,
    quantipy_root: Path = constants.DEFAULT_QUANTIPY_ROOT,
    validation_context: AutoresearchValidationContext | None,
) -> AutoresearchState | None:
    """Consume at most one validated stage submission from a model-writable inbox."""
    try:
        inbox_fd = os.open(
            inbox_path.expanduser(),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AutoresearchValidationError(f"cannot open stage submission inbox: {exc}") from exc
    snapshot_path: Path | None = None
    accepted_fd: int | None = None
    rejected_fd: int | None = None
    try:
        _validate_stage_inbox_directory_fd(inbox_fd, label="stage submission inbox")
        accepted_fd = _open_stage_inbox_child_directory(inbox_fd, "accepted")
        rejected_fd = _open_stage_inbox_child_directory(inbox_fd, "rejected")
        assert accepted_fd is not None
        assert rejected_fd is not None

        candidates: list[str] = []
        for name in sorted(os.listdir(inbox_fd)):
            if not name.endswith(".json"):
                continue
            entry = os.stat(name, dir_fd=inbox_fd, follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode):
                raise AutoresearchValidationError(
                    "stage submission candidate must be a non-symlink regular file"
                )
            candidates.append(name)

        if not candidates:
            return None
        policy = configuration_module.load_autoresearch_policy(openclaw_config)

        def quarantine(name: str, destination_fd: int) -> None:
            _move_stage_inbox_entry_no_replace(
                name,
                src_dir_fd=inbox_fd,
                dst_dir_fd=destination_fd,
            )

        for artifact_name in candidates:
            try:
                artifact_stat = os.stat(artifact_name, dir_fd=inbox_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AutoresearchValidationError(
                    f"cannot inspect stage submission artifact: {exc}"
                ) from exc
            if not stat.S_ISREG(artifact_stat.st_mode):
                raise AutoresearchValidationError(
                    "stage submission artifact must be a non-symlink regular file"
                )
            try:
                artifact_fd = os.open(artifact_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=inbox_fd)
            except OSError as exc:
                raise AutoresearchValidationError(
                    f"cannot open stage submission artifact: {exc}"
                ) from exc
            try:
                opened_artifact = os.fstat(artifact_fd)
                if (
                    opened_artifact.st_dev,
                    opened_artifact.st_ino,
                    opened_artifact.st_size,
                ) != (
                    artifact_stat.st_dev,
                    artifact_stat.st_ino,
                    artifact_stat.st_size,
                ):
                    raise AutoresearchValidationError(
                        "stage submission artifact changed during inspection"
                    )
                if opened_artifact.st_nlink != 1:
                    quarantine(artifact_name, rejected_fd)
                    continue
                if opened_artifact.st_size > constants.MAX_STAGE_SUBMISSION_BYTES:
                    quarantine(artifact_name, rejected_fd)
                    continue
                envelope = os.read(artifact_fd, constants.MAX_STAGE_SUBMISSION_BYTES + 1)
                if len(envelope) != opened_artifact.st_size:
                    raise AutoresearchValidationError(
                        "stage submission artifact changed while reading"
                    )
            finally:
                os.close(artifact_fd)
            snapshot_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=Path(state_path).expanduser().resolve(strict=False).parent,
                    prefix=".stage-submission.",
                    suffix=".json",
                    delete=False,
                ) as snapshot:
                    snapshot_path = Path(snapshot.name)
                    snapshot.write(envelope)
                    snapshot.flush()
                    os.fsync(snapshot.fileno())
                snapshot_path.chmod(0o600)
                artifact_path = snapshot_path
                state = persistence_module.load_state_file(state_path)
                receipts = manifest_runtime_module.build_receipt_catalog(quantipy_root)
                instruction_manifest_sha256 = (
                    manifest_runtime_module.expected_instruction_manifest_sha256(
                        state,
                        policy,
                        receipts,
                        state_path=state_path,
                    )
                )
                try:
                    advanced = persistence_module.advance_artifact_state_file(
                        state_path=state_path,
                        output_path=output_path,
                        artifact_path=artifact_path,
                        instruction_manifest_sha256=instruction_manifest_sha256,
                        policy=policy,
                        validation_context=validation_context,
                    )
                except AutoresearchValidationError:
                    quarantine(artifact_name, rejected_fd)
                    continue
                quarantine(artifact_name, accepted_fd)
                return advanced
            finally:
                if snapshot_path is not None:
                    with suppress(FileNotFoundError):
                        snapshot_path.unlink()
    finally:
        if accepted_fd is not None:
            os.close(accepted_fd)
        if rejected_fd is not None:
            os.close(rejected_fd)
        os.close(inbox_fd)
    return None


def persist_next_iteration_state(
    source_path: Path,
    output_path: Path,
    source_state: AutoresearchState,
    derived_state: AutoresearchState,
    *,
    instruction_manifest_sha256: str,
    policy: AutoresearchPolicy,
    receipt_catalog_factory: Callable[[], ReceiptCatalog],
) -> None:
    """Publish a next-iteration state only after persisting its decision receipt."""
    resolved_source_path = source_path.expanduser().resolve(strict=False)
    resolved_output_path = output_path.expanduser().resolve(strict=False)
    expected_reference = transitions_module.build_authoritative_state_reference(
        source_state,
        state_path=resolved_source_path,
    )
    with persistence_module._exclusive_state_locks((resolved_source_path, resolved_output_path)):
        persisted_state = persistence_module.load_state_file(resolved_source_path)
        persisted_reference = transitions_module.build_authoritative_state_reference(
            persisted_state,
            state_path=resolved_source_path,
        )
        if persisted_reference != expected_reference:
            raise AutoresearchValidationError(
                "persisted state does not match the supplied authoritative state"
            )
        current_instruction_manifest_sha256 = (
            manifest_runtime_module.expected_instruction_manifest_sha256(
                persisted_state,
                policy,
                receipt_catalog_factory(),
                state_path=resolved_source_path,
            )
        )
        if current_instruction_manifest_sha256 != instruction_manifest_sha256:
            raise AutoresearchValidationError("decision receipt instruction manifest is stale")
        from gateway.autoresearch_decision_receipts import persist_decision_receipt

        persist_decision_receipt(
            persisted_state,
            state_path=source_path,
            instruction_manifest_sha256=instruction_manifest_sha256,
        )
        persistence_module._atomic_save_state_file(resolved_output_path, derived_state)


def _atomic_save_state_file(
    path: Path,
    state: AutoresearchState,
    *,
    publication_guard: _ArtifactAdvancePublicationGuard | None = None,
) -> None:
    if publication_guard is not None and state != publication_guard.expected_candidate:
        raise AutoresearchValidationError(
            "atomic artifact publication candidate does not match its guard"
        )
    serialized_state = json.dumps(state.to_dict(), indent=2, sort_keys=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(serialized_state)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if publication_guard is not None:
            persistence_module._revalidate_artifact_advance_for_atomic_publication(
                publication_guard
            )
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
