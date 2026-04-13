"""Credential resolver for runtime vault-based credential lookup.

Searches session-bound vaults in order for matching credentials,
decrypts secrets, and returns plaintext values for the orchestrator
and connector to use.

Validates: Requirements 10.1, 10.2, 10.3, 10.4, 11.1, 11.2, 11.3, 11.5
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid

from cryptography.fernet import InvalidToken

from app.db import fetch_one
from app.encryption import get_encryption_service

logger = logging.getLogger("linchpin-api.credentials")


class CredentialResolver:
    """Resolves credentials from vaults at runtime."""

    async def resolve_api_key(
        self, vault_ids: list[str], provider: str
    ) -> str | None:
        """Search vaults in order for an api_key credential matching *provider*.

        Returns the decrypted API key string, or None if no match is found
        or decryption fails.
        """
        for vault_id in vault_ids:
            try:
                uid = _uuid.UUID(vault_id)
            except ValueError:
                continue
            row = await fetch_one(
                """
                SELECT encrypted_secrets FROM credentials
                WHERE vault_id = $1
                  AND credential_type = 'api_key'
                  AND provider = $2
                  AND archived_at IS NULL
                """,
                uid,
                provider,
            )
            if row is not None and row["encrypted_secrets"] is not None:
                try:
                    enc_svc = get_encryption_service()
                    plaintext = enc_svc.decrypt(row["encrypted_secrets"])
                    secrets = json.loads(plaintext)
                    return secrets.get("api_key")
                except (InvalidToken, Exception) as exc:
                    logger.error(
                        "Failed to decrypt api_key credential in vault %s for provider %s: %s",
                        vault_id,
                        provider,
                        exc,
                    )
                    continue
        return None

    async def resolve_mcp_credential(
        self, vault_ids: list[str], mcp_server_url: str
    ) -> dict | None:
        """Search vaults in order for a bearer_token or oauth credential
        matching *mcp_server_url*.

        Returns a dict with auth_type + token fields, or None if no match
        is found or decryption fails.
        """
        for vault_id in vault_ids:
            try:
                uid = _uuid.UUID(vault_id)
            except ValueError:
                continue
            row = await fetch_one(
                """
                SELECT credential_type, encrypted_secrets FROM credentials
                WHERE vault_id = $1
                  AND mcp_server_url = $2
                  AND archived_at IS NULL
                """,
                uid,
                mcp_server_url,
            )
            if row is not None and row["encrypted_secrets"] is not None:
                try:
                    enc_svc = get_encryption_service()
                    plaintext = enc_svc.decrypt(row["encrypted_secrets"])
                    secrets = json.loads(plaintext)
                    cred_type = row["credential_type"]

                    if cred_type == "bearer_token":
                        return {
                            "auth_type": "bearer_token",
                            "token": secrets.get("token"),
                        }
                    elif cred_type == "oauth":
                        return {
                            "auth_type": "oauth",
                            "access_token": secrets.get("access_token"),
                        }
                except (InvalidToken, Exception) as exc:
                    logger.error(
                        "Failed to decrypt MCP credential in vault %s for %s: %s",
                        vault_id,
                        mcp_server_url,
                        exc,
                    )
                    continue
        return None
