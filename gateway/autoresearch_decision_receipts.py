"""Immutable decision receipts for autoresearch iteration boundaries."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from gateway.autoresearch.constants import (
    AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN,
    AUTHORITATIVE_STATE_REFERENCE_VERSION,
)
from gateway.autoresearch.enums import Phase
from gateway.autoresearch.errors import AutoresearchValidationError
from gateway.autoresearch.state import AutoresearchState
from gateway.autoresearch.transitions import build_authoritative_state_reference

DECISION_RECEIPT_SCHEMA_VERSION = 1
DECISION_RECEIPT_TYPE = "g2-openclaw.autoresearch.decision-receipt"
DECISION_RECEIPT_DIGEST_DOMAIN = "g2-openclaw.autoresearch.decision-receipt.v1"
DECISION_RECEIPT_DIR_NAME = "decision-receipts"
_DECISION_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "receipt_type",
        "digest_domain",
        "iteration",
        "mode",
        "state_reference",
        "state_reference_sha256",
        "state_reference_digest_domain",
        "instruction_manifest_sha256",
        "final_decision",
        "final_decision_sha256",
        "latest_verification",
        "latest_verification_sha256",
        "memory_verification_receipt",
        "memory_verification_receipt_sha256",
    }
)
_STATE_REFERENCE_KEYS = frozenset(
    {
        "version",
        "digest_domain",
        "path",
        "state_sha256",
        "phase",
        "iteration",
    }
)


@dataclass(frozen=True, slots=True)
class PersistedDecisionReceipt:
    path: Path
    sha256: str
    content: bytes


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _domain_digest(domain: str, value: object) -> str:
    payload = b"\n".join((domain.encode("utf-8"), _canonical_json_bytes(value)))
    return hashlib.sha256(payload).hexdigest()


def _validate_sha256(value: str, *, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise AutoresearchValidationError(f"{label} must be a lowercase SHA-256 digest")


def build_decision_receipt_payload(
    state: AutoresearchState,
    *,
    state_path: Path,
    instruction_manifest_sha256: str,
) -> dict[str, object]:
    """Return the deterministic platform decision receipt payload for a completed state."""
    _validate_sha256(instruction_manifest_sha256, label="instruction_manifest_sha256")
    if state.phase is not Phase.REPEAT or state.final_decision is None:
        raise AutoresearchValidationError(
            "decision receipt requires a completed repeat state with final_decision"
        )
    if state.final_decision.memory_write_required:
        if not state.memory_written or state.memory_verification_receipt is None:
            raise AutoresearchValidationError(
                "decision receipt requires verified memory receipt before next iteration"
            )
    elif state.memory_verification_receipt is not None:
        raise AutoresearchValidationError(
            "decision receipt cannot include memory receipt for a no-memory decision"
        )
    state_reference = build_authoritative_state_reference(state, state_path=state_path)
    state_reference_payload = state_reference.to_dict()
    state_reference_sha256 = state_reference.sha256()
    final_decision = state.final_decision.to_dict()
    latest_verification = (
        state.latest_verification.to_dict() if state.latest_verification is not None else None
    )
    memory_receipt = (
        state.memory_verification_receipt.to_dict()
        if state.memory_verification_receipt is not None
        else None
    )
    return {
        "schema_version": DECISION_RECEIPT_SCHEMA_VERSION,
        "receipt_type": DECISION_RECEIPT_TYPE,
        "digest_domain": DECISION_RECEIPT_DIGEST_DOMAIN,
        "iteration": state.iteration,
        "mode": state.mode.value if state.mode is not None else None,
        "state_reference": state_reference_payload,
        "state_reference_sha256": state_reference_sha256,
        "state_reference_digest_domain": AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN,
        "instruction_manifest_sha256": instruction_manifest_sha256,
        "final_decision": final_decision,
        "final_decision_sha256": _domain_digest(
            f"{DECISION_RECEIPT_DIGEST_DOMAIN}.final-decision", final_decision
        ),
        "latest_verification": latest_verification,
        "latest_verification_sha256": (
            _domain_digest(f"{DECISION_RECEIPT_DIGEST_DOMAIN}.verification", latest_verification)
            if latest_verification is not None
            else None
        ),
        "memory_verification_receipt": memory_receipt,
        "memory_verification_receipt_sha256": (
            _domain_digest(f"{DECISION_RECEIPT_DIGEST_DOMAIN}.memory-receipt", memory_receipt)
            if memory_receipt is not None
            else None
        ),
    }


def decision_receipt_content(
    state: AutoresearchState,
    *,
    state_path: Path,
    instruction_manifest_sha256: str,
) -> bytes:
    payload = build_decision_receipt_payload(
        state,
        state_path=state_path,
        instruction_manifest_sha256=instruction_manifest_sha256,
    )
    return _canonical_json_bytes(payload)


def decision_receipt_path(state_path: Path, iteration: int) -> Path:
    if iteration < 1:
        raise AutoresearchValidationError("decision receipt iteration must be >= 1")
    state_path_abs = _canonical_no_symlink_state_path(state_path)
    with _open_decision_receipt_directory(state_path_abs) as receipt_dir:
        receipt_path = receipt_dir.path / _receipt_file_name(iteration)
    return receipt_path


def persist_decision_receipt(
    state: AutoresearchState,
    *,
    state_path: Path,
    instruction_manifest_sha256: str,
) -> PersistedDecisionReceipt:
    content = decision_receipt_content(
        state,
        state_path=state_path,
        instruction_manifest_sha256=instruction_manifest_sha256,
    )
    state_path_abs = _canonical_no_symlink_state_path(state_path)
    receipt_name = _receipt_file_name(state.iteration)
    with _open_decision_receipt_directory(state_path_abs) as receipt_dir:
        path = receipt_dir.path / receipt_name
        _write_idempotent_receipt(receipt_dir.fd, path, receipt_name, content)
        _verify_receipt_file(receipt_dir.fd, path, receipt_name, content)
    return PersistedDecisionReceipt(
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


@dataclass(frozen=True, slots=True)
class _OpenReceiptDirectory:
    path: Path
    fd: int


def _receipt_file_name(iteration: int) -> str:
    if iteration < 1:
        raise AutoresearchValidationError("decision receipt iteration must be >= 1")
    return f"iteration-{iteration:06d}.json"


def _canonical_no_symlink_state_path(state_path: Path) -> Path:
    expanded = state_path.expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    canonical = Path(os.path.normpath(os.fspath(absolute)))
    if os.fspath(canonical) != os.fspath(absolute):
        raise AutoresearchValidationError(
            f"autoresearch state path must be canonical without '.' or '..': {absolute}"
        )
    if canonical.name in ("", ".", ".."):
        raise AutoresearchValidationError(f"invalid autoresearch state path: {state_path}")
    state_dir = canonical.parent
    fd = _open_directory_chain(state_dir)
    try:
        directory_stat = os.fstat(fd)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise AutoresearchValidationError(
                f"autoresearch state directory is not a directory: {state_dir}"
            )
        if _fd_path(fd) != state_dir:
            raise AutoresearchValidationError(
                f"autoresearch state directory must be a canonical no-symlink path: {state_dir}"
            )
    finally:
        os.close(fd)
    return canonical


def _open_directory_chain(path: Path) -> int:
    if not path.is_absolute():
        raise AutoresearchValidationError(f"directory path must be absolute: {path}")
    current_fd = os.open("/", os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in ("", ".", ".."):
                raise AutoresearchValidationError(
                    f"directory path must be canonical without '.' or '..': {path}"
                )
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise AutoresearchValidationError(
                        f"directory path must not contain symlinks: {path}"
                    ) from exc
                raise AutoresearchValidationError(
                    f"unable to open directory without following symlinks: {path}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _fd_path(fd: int) -> Path:
    try:
        raw = os.readlink(f"/proc/self/fd/{fd}")
    except OSError as exc:
        raise AutoresearchValidationError("unable to verify canonical directory fd") from exc
    return Path(raw)


@contextmanager
def _open_decision_receipt_directory(state_path: Path) -> Iterator[_OpenReceiptDirectory]:
    state_dir = state_path.parent
    state_dir_fd = _open_directory_chain(state_dir)
    try:
        receipt_dir = state_dir / DECISION_RECEIPT_DIR_NAME
        _ensure_receipt_directory(state_dir_fd, state_dir, receipt_dir)
        receipt_dir_fd = _open_receipt_directory_fd(state_dir_fd, receipt_dir)
    finally:
        os.close(state_dir_fd)
    try:
        yield _OpenReceiptDirectory(path=receipt_dir, fd=receipt_dir_fd)
    finally:
        os.close(receipt_dir_fd)


def _ensure_receipt_directory(state_dir_fd: int, state_dir: Path, receipt_dir: Path) -> None:
    try:
        os.mkdir(DECISION_RECEIPT_DIR_NAME, mode=0o700, dir_fd=state_dir_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise AutoresearchValidationError(
            f"unable to create decision receipt directory: {receipt_dir}"
        ) from exc


def _open_receipt_directory_fd(state_dir_fd: int, receipt_dir: Path) -> int:
    try:
        receipt_dir_fd = os.open(
            DECISION_RECEIPT_DIR_NAME,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=state_dir_fd,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AutoresearchValidationError(
                f"decision receipt directory must not be a symlink: {receipt_dir}"
            ) from exc
        raise AutoresearchValidationError(
            f"unable to open decision receipt directory: {receipt_dir}"
        ) from exc
    try:
        receipt_dir_stat = os.fstat(receipt_dir_fd)
        if not stat.S_ISDIR(receipt_dir_stat.st_mode):
            raise AutoresearchValidationError(
                f"decision receipt path is not a directory: {receipt_dir}"
            )
        if receipt_dir_stat.st_uid != os.getuid():
            raise AutoresearchValidationError(
                f"decision receipt directory has wrong owner: {receipt_dir}"
            )
        if stat.S_IMODE(receipt_dir_stat.st_mode) != 0o700:
            raise AutoresearchValidationError(
                f"decision receipt directory permissions must be 0700: {receipt_dir}"
            )
        if _fd_path(receipt_dir_fd) != receipt_dir:
            raise AutoresearchValidationError(
                f"decision receipt directory must be a canonical no-symlink path: {receipt_dir}"
            )
    except Exception:
        os.close(receipt_dir_fd)
        raise
    return receipt_dir_fd


def _write_idempotent_receipt(
    receipt_dir_fd: int,
    path: Path,
    receipt_name: str,
    content: bytes,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(receipt_name, flags, 0o600, dir_fd=receipt_dir_fd)
    except FileExistsError:
        _verify_receipt_file(receipt_dir_fd, path, receipt_name, content)
        return
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AutoresearchValidationError(
                f"decision receipt path must not be a symlink: {path}"
            ) from exc
        raise AutoresearchValidationError(f"unable to create decision receipt: {path}") from exc
    try:
        _write_all(fd, content)
        os.fsync(fd)
    except OSError as exc:
        raise AutoresearchValidationError(f"unable to write decision receipt: {path}") from exc
    finally:
        os.close(fd)
    _fsync_directory_fd(receipt_dir_fd, path.parent)


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written == 0:
            raise OSError(errno.EIO, "zero-byte write while writing decision receipt")
        offset += written


def _verify_receipt_file(
    receipt_dir_fd: int,
    path: Path,
    receipt_name: str,
    expected_content: bytes,
) -> None:
    try:
        fd = os.open(
            receipt_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=receipt_dir_fd,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AutoresearchValidationError(
                f"decision receipt path must not be a symlink: {path}"
            ) from exc
        raise AutoresearchValidationError(f"missing decision receipt: {path}") from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise AutoresearchValidationError(f"decision receipt is not a regular file: {path}")
        if file_stat.st_uid != os.getuid():
            raise AutoresearchValidationError(f"decision receipt has wrong owner: {path}")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise AutoresearchValidationError(f"decision receipt permissions must be 0600: {path}")
        content = _read_all(fd, max(len(expected_content) + 1, 1))
    finally:
        os.close(fd)
    try:
        _validate_expected_receipt_bytes(content, expected_content)
    except AutoresearchValidationError as exc:
        raise AutoresearchValidationError(f"decision receipt conflict: {path}") from exc


def _read_all(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = os.read(fd, min(64 * 1024, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _validate_expected_receipt_bytes(content: bytes, expected_content: bytes) -> None:
    try:
        payload = json.loads(content.decode("utf-8"))
        expected_payload = json.loads(expected_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AutoresearchValidationError("decision receipt is not canonical JSON") from exc
    _validate_decision_receipt_payload(payload)
    _validate_decision_receipt_payload(expected_payload)
    if payload != expected_payload:
        raise AutoresearchValidationError("decision receipt payload differs")
    if _canonical_json_bytes(payload) != content:
        raise AutoresearchValidationError("decision receipt JSON is not canonical")


def _validate_decision_receipt_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        raise AutoresearchValidationError("decision receipt must be a JSON object")
    if set(payload) != _DECISION_RECEIPT_KEYS:
        raise AutoresearchValidationError("decision receipt keys are not exact")
    if payload["schema_version"] != DECISION_RECEIPT_SCHEMA_VERSION:
        raise AutoresearchValidationError("decision receipt schema_version is invalid")
    if payload["receipt_type"] != DECISION_RECEIPT_TYPE:
        raise AutoresearchValidationError("decision receipt type is invalid")
    if payload["digest_domain"] != DECISION_RECEIPT_DIGEST_DOMAIN:
        raise AutoresearchValidationError("decision receipt digest domain is invalid")
    if payload["state_reference_digest_domain"] != AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN:
        raise AutoresearchValidationError("decision receipt state reference domain is invalid")
    _validate_sha256(
        _require_string(payload["instruction_manifest_sha256"], "instruction_manifest_sha256"),
        label="instruction_manifest_sha256",
    )
    state_reference = _require_mapping(payload["state_reference"], "state_reference")
    if set(state_reference) != _STATE_REFERENCE_KEYS:
        raise AutoresearchValidationError("decision receipt state_reference keys are not exact")
    if state_reference["version"] != AUTHORITATIVE_STATE_REFERENCE_VERSION:
        raise AutoresearchValidationError("decision receipt state_reference version is invalid")
    _validate_sha256(
        _require_string(state_reference["state_sha256"], "state_reference.state_sha256"),
        label="state_reference.state_sha256",
    )
    if payload["state_reference_sha256"] != _state_reference_payload_sha256(state_reference):
        raise AutoresearchValidationError("decision receipt state_reference_sha256 is invalid")
    final_decision = _require_mapping(payload["final_decision"], "final_decision")
    if payload["final_decision_sha256"] != _domain_digest(
        f"{DECISION_RECEIPT_DIGEST_DOMAIN}.final-decision", final_decision
    ):
        raise AutoresearchValidationError("decision receipt final_decision_sha256 is invalid")
    latest_verification = payload["latest_verification"]
    expected_latest_digest = (
        _domain_digest(f"{DECISION_RECEIPT_DIGEST_DOMAIN}.verification", latest_verification)
        if latest_verification is not None
        else None
    )
    if payload["latest_verification_sha256"] != expected_latest_digest:
        raise AutoresearchValidationError("decision receipt latest_verification_sha256 is invalid")
    memory_receipt = payload["memory_verification_receipt"]
    expected_memory_digest = (
        _domain_digest(f"{DECISION_RECEIPT_DIGEST_DOMAIN}.memory-receipt", memory_receipt)
        if memory_receipt is not None
        else None
    )
    if payload["memory_verification_receipt_sha256"] != expected_memory_digest:
        raise AutoresearchValidationError(
            "decision receipt memory_verification_receipt_sha256 is invalid"
        )


def _state_reference_payload_sha256(payload: dict[str, object]) -> str:
    version = _require_string(payload["version"], "state_reference.version")
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        "\n".join((AUTHORITATIVE_STATE_REFERENCE_DIGEST_DOMAIN, version, canonical_json)).encode(
            "utf-8"
        )
    ).hexdigest()


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AutoresearchValidationError(f"{label} must be an object")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AutoresearchValidationError(f"{label} must be a string")
    return value


def _fsync_directory_fd(directory_fd: int, path: Path) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        raise AutoresearchValidationError(
            f"unable to fsync decision receipt directory: {path}"
        ) from exc
