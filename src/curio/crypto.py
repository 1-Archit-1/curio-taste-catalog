"""Process-split sealed-box encryption (libsodium via PyNaCl) and token helpers.

Per ADR 0002:
- API processes hold only `KEY_PUBLIC_KEY` and instantiate `KeyEncryptor`.
- Worker processes hold `KEY_PRIVATE_KEY` and instantiate `KeyDecryptor`.
"""

import base64
import hashlib
import secrets
from dataclasses import dataclass

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox


class PublicKeyNotConfiguredError(RuntimeError):
    """Raised when encryption is attempted without KEY_PUBLIC_KEY configured."""


class PrivateKeyNotConfiguredError(RuntimeError):
    """Raised when decryption is attempted without KEY_PRIVATE_KEY configured."""


class KeyDecryptionError(RuntimeError):
    """Raised when sealed-box decryption fails (wrong key or corrupted ciphertext)."""


@dataclass(frozen=True)
class Keypair:
    public_key: str
    private_key: str


def generate_keypair() -> Keypair:
    """Generate a new Curve25519 keypair, encoded as standard base64 strings."""
    sk = PrivateKey.generate()
    return Keypair(
        public_key=base64.b64encode(bytes(sk.public_key)).decode("ascii"),
        private_key=base64.b64encode(bytes(sk)).decode("ascii"),
    )


def _decode_key_bytes(key_b64: str, expected_len: int = 32) -> bytes:
    try:
        raw = base64.b64decode(key_b64.strip(), validate=True)
    except Exception as exc:
        raise ValueError("invalid base64 key material") from exc
    if len(raw) != expected_len:
        raise ValueError(f"key must decode to {expected_len} bytes, got {len(raw)}")
    return raw


class KeyEncryptor:
    """Encrypts API keys using only the Curve25519 public key. Cannot decrypt."""

    def __init__(self, public_key_b64: str) -> None:
        if not public_key_b64 or not public_key_b64.strip():
            raise PublicKeyNotConfiguredError("KEY_PUBLIC_KEY is not configured")
        self._box = SealedBox(PublicKey(_decode_key_bytes(public_key_b64)))

    def encrypt(self, plaintext: str) -> bytes:
        return bytes(self._box.encrypt(plaintext.encode("utf-8")))


class KeyDecryptor:
    """Decrypts sealed-box API keys using the worker's Curve25519 private key."""

    def __init__(self, private_key_b64: str | None) -> None:
        if not private_key_b64 or not private_key_b64.strip():
            raise PrivateKeyNotConfiguredError("KEY_PRIVATE_KEY is not configured")
        self._sk = PrivateKey(_decode_key_bytes(private_key_b64))
        self._box = SealedBox(self._sk)

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(bytes(self._sk.public_key)).decode("ascii")

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._box.decrypt(bytes(ciphertext)).decode("utf-8")
        except (CryptoError, UnicodeDecodeError) as exc:
            raise KeyDecryptionError("failed to decrypt stored API key") from exc


def generate_secret_token(nbytes: int = 32) -> str:
    """Return a URL-safe random token with `nbytes` of entropy."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Return a SHA-256 hex digest of a secret token for safe database storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_last4(secret: str) -> str:
    """Return the last 4 characters of a secret for masked UI display."""
    cleaned = secret.strip()
    return cleaned[-4:] if len(cleaned) >= 4 else cleaned
