"""Unit tests for CredentialResolver.

Validates: Requirements 10.1, 10.2, 10.3, 10.4, 11.1, 11.2, 11.3, 11.5
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.credentials import CredentialResolver


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Set a valid Fernet key for encryption service."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VAULT_ENCRYPTION_KEY", key)
    # Reset singleton so it picks up the new key
    from app.encryption import reset_encryption_service
    reset_encryption_service()
    yield
    reset_encryption_service()


def _encrypt(plaintext: str) -> bytes:
    """Encrypt a string using the current encryption service."""
    from app.encryption import get_encryption_service
    return get_encryption_service().encrypt(plaintext)


# ---- resolve_api_key ----


@pytest.mark.asyncio
@patch("app.credentials.fetch_one", new_callable=AsyncMock)
async def test_resolve_api_key_found(mock_fetch):
    """Returns decrypted api_key from first matching vault."""
    vault_id = str(uuid.uuid4())
    encrypted = _encrypt(json.dumps({"api_key": "sk-test-123"}))
    mock_fetch.return_value = {"encrypted_secrets": encrypted}

    resolver = CredentialResolver()
    result = await resolver.resolve_api_key([vault_id], "anthropic")

    assert result == "sk-test-123"
    mock_fetch.assert_called_once()


@pytest.mark.asyncio
@patch("app.credentials.fetch_one", new_callable=AsyncMock)
async def test_resolve_api_key_not_found(mock_fetch):
    """Returns None when no matching credential exists."""
    mock_fetch.return_value = None

    resolver = CredentialResolver()
    result = await resolver.resolve_api_key([str(uuid.uuid4())], "anthropic")

    assert result is None


@pytest.mark.asyncio
@patch("app.credentials.fetch_one", new_callable=AsyncMock)
async def test_resolve_api_key_vault_ordering(mock_fetch):
    """Returns api_key from the first vault that has a match."""
    vault_1 = str(uuid.uuid4())
    vault_2 = str(uuid.uuid4())
    encrypted = _encrypt(json.dumps({"api_key": "from-vault-2"}))

    # First vault has no match, second vault has a match
    mock_fetch.side_effect = [None, {"encrypted_secrets": encrypted}]

    resolver = CredentialResolver()
    result = await resolver.resolve_api_key([vault_1, vault_2], "openai")

    assert result == "from-vault-2"
    assert mock_fetch.call_count == 2


@pytest.mark.asyncio
@patch("app.credentials.fetch_one", new_callable=AsyncMock)
async def test_resolve_api_key_decryption_failure(mock_fetch):
    """Returns None and logs error on decryption failure."""
    mock_fetch.return_value = {"encrypted_secrets": b"corrupted-ciphertext"}

    resolver = CredentialResolver()
    result = await resolver.resolve_api_key([str(uuid.uuid4())], "anthropic")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_api_key_empty_vault_ids():
    """Returns None when vault_ids list is empty."""
    resolver = CredentialResolver()
    result = await resolver.resolve_api_key([], "anthropic")
    assert result is None


# ---- resolve_mcp_credential ----


@pytest.mark.asyncio
@patch("app.credentials.fetch_one", new_callable=AsyncMock)
async def test_resolve_mcp_credential_bearer_token(mock_fetch):
    """Returns bearer_token dict for matching MCP credential."""
    vault_id = str(uuid.uuid4())
    encrypted = _encrypt(json.dumps({"token": "mcp-bearer-abc"}))
    mock_fetch.return_value = {
        "credential_type": "bearer_token",
        "encrypted_secrets": encrypted,
    }

    resolver = CredentialResolver()
    result = await resolver.resolve_mcp_credential([vault_id], "https://mcp.example.com")

    assert result == {"auth_type": "bearer_token", "token": "mcp-bearer-abc"}


@pytest.mark.asyncio
@patch("app.credentials.fetch_one", new_callable=AsyncMock)
async def test_resolve_mcp_credential_oauth(mock_fetch):
    """Returns oauth dict for matching MCP credential."""
    vault_id = str(uuid.uuid4())
    encrypted = _encrypt(json.dumps({"access_token": "oauth-token-xyz"}))
    mock_fetch.return_value = {
        "credential_type": "oauth",
        "encrypted_secrets": encrypted,
    }

    resolver = CredentialResolver()
    result = await resolver.resolve_mcp_credential([vault_id], "https://mcp.example.com")

    assert result == {"auth_type": "oauth", "access_token": "oauth-token-xyz"}


@pytest.mark.asyncio
@patch("app.credentials.fetch_one", new_callable=AsyncMock)
async def test_resolve_mcp_credential_not_found(mock_fetch):
    """Returns None when no matching MCP credential exists."""
    mock_fetch.return_value = None

    resolver = CredentialResolver()
    result = await resolver.resolve_mcp_credential([str(uuid.uuid4())], "https://mcp.example.com")

    assert result is None


@pytest.mark.asyncio
@patch("app.credentials.fetch_one", new_callable=AsyncMock)
async def test_resolve_mcp_credential_decryption_failure(mock_fetch):
    """Returns None on decryption failure for MCP credential."""
    mock_fetch.return_value = {
        "credential_type": "bearer_token",
        "encrypted_secrets": b"bad-data",
    }

    resolver = CredentialResolver()
    result = await resolver.resolve_mcp_credential([str(uuid.uuid4())], "https://mcp.example.com")

    assert result is None


@pytest.mark.asyncio
@patch("app.credentials.fetch_one", new_callable=AsyncMock)
async def test_resolve_mcp_credential_vault_ordering(mock_fetch):
    """Returns MCP credential from the first vault that has a match."""
    vault_1 = str(uuid.uuid4())
    vault_2 = str(uuid.uuid4())
    encrypted = _encrypt(json.dumps({"token": "from-vault-2"}))

    mock_fetch.side_effect = [
        None,
        {"credential_type": "bearer_token", "encrypted_secrets": encrypted},
    ]

    resolver = CredentialResolver()
    result = await resolver.resolve_mcp_credential([vault_1, vault_2], "https://mcp.example.com")

    assert result == {"auth_type": "bearer_token", "token": "from-vault-2"}
    assert mock_fetch.call_count == 2
