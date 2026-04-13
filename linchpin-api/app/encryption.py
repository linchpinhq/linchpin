"""Fernet-based encryption service for credential vault secrets."""

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("linchpin-api")

_ENV_VAR = "VAULT_ENCRYPTION_KEY"


class EncryptionService:
    """Encrypts and decrypts secret values using Fernet symmetric encryption."""

    def __init__(self, key: str):
        """Initialize with a Fernet-compatible key (base64-encoded 32 bytes).

        Raises ValueError if the key is not a valid Fernet key.
        """
        try:
            self._fernet = Fernet(key.encode())
        except Exception as exc:
            raise ValueError(f"Invalid Fernet key: {exc}") from exc

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt a plaintext string, return ciphertext bytes."""
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        """Decrypt ciphertext bytes, return plaintext string."""
        return self._fernet.decrypt(ciphertext).decode()


# ---------------------------------------------------------------------------
# Module-level singleton with lazy initialization
# ---------------------------------------------------------------------------

_instance: EncryptionService | None = None


def get_encryption_service() -> EncryptionService:
    """Return the module-level EncryptionService singleton.

    On first call, reads ``VAULT_ENCRYPTION_KEY`` from the environment and
    creates the singleton.  Raises ``RuntimeError`` if the env var is missing
    or contains an invalid Fernet key.
    """
    global _instance
    if _instance is not None:
        return _instance

    key = os.environ.get(_ENV_VAR)
    if not key:
        raise RuntimeError(
            f"{_ENV_VAR} environment variable is required"
        )

    try:
        _instance = EncryptionService(key)
    except ValueError as exc:
        raise RuntimeError(
            f"{_ENV_VAR} is not a valid Fernet key: {exc}"
        ) from exc

    return _instance


def reset_encryption_service() -> None:
    """Reset the singleton (useful for testing)."""
    global _instance
    _instance = None
