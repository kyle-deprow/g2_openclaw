"""Ed25519 device identity for OpenClaw Gateway authentication.

OpenClaw requires a device identity (Ed25519 keypair) bound to the connect
request in order to grant operator scopes.  Without it, the server's
``clearUnboundScopes`` silently strips all requested scopes — even when
token auth succeeds — resulting in ``"missing scope: operator.write"`` on
any subsequent method call.

This module mirrors the behaviour of the official JS ``GatewayClient``:

1. Generate or load a persistent Ed25519 identity stored as JSON.
2. Derive a stable ``deviceId`` as the hex SHA-256 of the raw public key.
3. Sign a canonical payload (``buildDeviceAuthPayload`` format v2).
4. Build the ``device`` block expected by the connect handshake.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

logger = logging.getLogger(__name__)

# SPKI DER prefix for Ed25519 keys (12 bytes), raw key follows immediately.
_ED25519_SPKI_PREFIX = bytes(
    [0x30, 0x2A, 0x30, 0x05, 0x06, 0x03, 0x2B, 0x65, 0x70, 0x03, 0x21, 0x00]
)

# Default storage path — mirrors OpenClaw's ``~/.openclaw/state/device-identity.json``
_DEFAULT_IDENTITY_DIR = Path.home() / ".openclaw" / "state"
_DEFAULT_IDENTITY_PATH = _DEFAULT_IDENTITY_DIR / "device-identity.json"


def _b64url_encode_no_pad(data: bytes) -> str:
    """Base64-URL encode without padding (matches OpenClaw JS ``base64UrlEncode``)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_key_raw(public_key_pem: str) -> bytes:
    """Extract the 32-byte raw Ed25519 public key from a PEM string."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pub = load_pem_public_key(public_key_pem.encode())
    spki = pub.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    prefix_len = len(_ED25519_SPKI_PREFIX)
    if len(spki) == prefix_len + 32 and spki[:prefix_len] == _ED25519_SPKI_PREFIX:
        return spki[prefix_len:]
    return spki


def _fingerprint_public_key(public_key_pem: str) -> str:
    """SHA-256 hex digest of the raw public key — used as ``deviceId``."""
    raw = _public_key_raw(public_key_pem)
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class DeviceIdentity:
    """Loaded or generated Ed25519 device identity."""

    device_id: str
    public_key_pem: str
    private_key_pem: str


def _generate_identity() -> DeviceIdentity:
    """Generate a fresh Ed25519 identity."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    public_pem = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    private_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()

    device_id = _fingerprint_public_key(public_pem)
    return DeviceIdentity(
        device_id=device_id,
        public_key_pem=public_pem,
        private_key_pem=private_pem,
    )


def load_or_create_device_identity(
    path: Path | None = None,
) -> DeviceIdentity:
    """Load an existing device identity from *path*, or generate and persist a new one.

    File permissions are set to ``0o600`` (owner-only read/write) to protect
    the private key, matching the JS SDK behaviour.
    """
    file_path = path or _DEFAULT_IDENTITY_PATH

    # Try loading existing
    if file_path.exists():
        try:
            raw = json.loads(file_path.read_text())
            if (
                raw.get("version") == 1
                and isinstance(raw.get("deviceId"), str)
                and isinstance(raw.get("publicKeyPem"), str)
                and isinstance(raw.get("privateKeyPem"), str)
            ):
                # Re-derive device ID in case the stored value is stale
                derived_id = _fingerprint_public_key(raw["publicKeyPem"])
                return DeviceIdentity(
                    device_id=derived_id,
                    public_key_pem=raw["publicKeyPem"],
                    private_key_pem=raw["privateKeyPem"],
                )
        except Exception:
            logger.warning("Corrupt device identity at %s — regenerating", file_path)

    # Generate new identity
    identity = _generate_identity()

    file_path.parent.mkdir(parents=True, exist_ok=True)
    stored = {
        "version": 1,
        "deviceId": identity.device_id,
        "publicKeyPem": identity.public_key_pem,
        "privateKeyPem": identity.private_key_pem,
        "createdAtMs": int(__import__("time").time() * 1000),
    }
    file_path.write_text(json.dumps(stored, indent=2) + "\n")
    with contextlib.suppress(OSError):
        os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

    logger.info("Created new device identity %s at %s", identity.device_id[:12], file_path)
    return identity


def build_device_auth_payload(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    signed_at_ms: int,
    token: str | None,
    nonce: str,
) -> str:
    """Build the canonical v2 payload string that must be signed.

    Format: ``v2|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce``

    Must match the JS ``buildDeviceAuthPayload`` exactly.
    """
    return "|".join(
        [
            "v2",
            device_id,
            client_id,
            client_mode,
            role,
            ",".join(scopes),
            str(signed_at_ms),
            token or "",
            nonce,
        ]
    )


def sign_payload(private_key_pem: str, payload: str) -> str:
    """Sign *payload* with the Ed25519 private key and return a base64-URL string."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    private_key = load_pem_private_key(private_key_pem.encode(), password=None)
    signature = private_key.sign(payload.encode("utf-8"))  # type: ignore[union-attr, call-arg]
    return _b64url_encode_no_pad(signature)


def build_device_connect_block(
    identity: DeviceIdentity,
    *,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    token: str | None,
    nonce: str,
) -> tuple[dict[str, object], int]:
    """Build the ``device`` dict and ``signedAt`` for the connect request.

    Returns ``(device_block, signed_at_ms)``.
    """
    import time

    signed_at_ms = int(time.time() * 1000)

    payload = build_device_auth_payload(
        device_id=identity.device_id,
        client_id=client_id,
        client_mode=client_mode,
        role=role,
        scopes=scopes,
        signed_at_ms=signed_at_ms,
        token=token,
        nonce=nonce,
    )

    signature = sign_payload(identity.private_key_pem, payload)
    raw_pub = _public_key_raw(identity.public_key_pem)

    return (
        {
            "id": identity.device_id,
            "publicKey": _b64url_encode_no_pad(raw_pub),
            "signature": signature,
            "signedAt": signed_at_ms,
            "nonce": nonce,
        },
        signed_at_ms,
    )
