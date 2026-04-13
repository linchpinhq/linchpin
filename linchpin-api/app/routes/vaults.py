"""Vault and Credential CRUD endpoints.

POST   /v1/vaults                                          — Create vault
GET    /v1/vaults                                          — List vaults (paginated)
GET    /v1/vaults/{vault_id}                               — Get vault
DELETE /v1/vaults/{vault_id}                               — Delete vault + cascade
POST   /v1/vaults/{vault_id}/archive                       — Archive vault

POST   /v1/vaults/{vault_id}/credentials                   — Create credential
GET    /v1/vaults/{vault_id}/credentials                   — List credentials (paginated)
GET    /v1/vaults/{vault_id}/credentials/{credential_id}   — Get credential
PATCH  /v1/vaults/{vault_id}/credentials/{credential_id}   — Update credential secrets
DELETE /v1/vaults/{vault_id}/credentials/{credential_id}   — Delete credential
POST   /v1/vaults/{vault_id}/credentials/{credential_id}/archive — Archive credential

Validates: Requirements 1.1–1.6, 2.1–2.4, 3.1–3.9, 4.1–4.4, 5.1–5.4,
           6.1–6.4, 7.1–7.3, 13.1–13.12
"""

from __future__ import annotations

import json
import logging
import uuid

import asyncpg
from fastapi import APIRouter, HTTPException, Query

from app.db import fetch_all, fetch_one, execute, get_pool
from app.encryption import get_encryption_service
from app.models import (
    CreateCredentialRequest,
    CreateVaultRequest,
    CredentialResponse,
    PaginatedListResponse,
    UpdateCredentialRequest,
    VaultResponse,
)

logger = logging.getLogger("linchpin-api")

router = APIRouter(prefix="/vaults", tags=["vaults"])

MAX_CREDENTIALS_PER_VAULT = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_vault(row) -> VaultResponse:
    """Convert an asyncpg Record to a VaultResponse."""
    return VaultResponse(
        id=str(row["id"]),
        display_name=row["display_name"],
        metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _row_to_credential(row) -> CredentialResponse:
    """Convert an asyncpg Record to a CredentialResponse (secrets omitted)."""
    return CredentialResponse(
        id=str(row["id"]),
        vault_id=str(row["vault_id"]),
        credential_type=row["credential_type"],
        mcp_server_url=row["mcp_server_url"],
        provider=row["provider"],
        client_id=row["client_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


async def _get_vault_or_404(vault_id: str):
    """Fetch a vault row by id or raise 404."""
    try:
        uid = uuid.UUID(vault_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Vault {vault_id} not found"},
        )
    row = await fetch_one("SELECT * FROM vaults WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Vault {vault_id} not found"},
        )
    return row


# ---------------------------------------------------------------------------
# Vault CRUD
# ---------------------------------------------------------------------------


@router.post("", status_code=201, response_model=VaultResponse)
async def create_vault(body: CreateVaultRequest) -> VaultResponse:
    """Create a new vault resource."""
    vault_id = str(uuid.uuid4())
    metadata_json = json.dumps(body.metadata)

    row = await fetch_one(
        """
        INSERT INTO vaults (id, display_name, metadata)
        VALUES ($1, $2, $3::jsonb)
        RETURNING *
        """,
        uuid.UUID(vault_id),
        body.display_name,
        metadata_json,
    )
    return _row_to_vault(row)


@router.get("", response_model=PaginatedListResponse)
async def list_vaults(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedListResponse:
    """Return a paginated list of vaults."""
    rows = await fetch_all(
        "SELECT * FROM vaults ORDER BY created_at DESC LIMIT $1 OFFSET $2",
        limit + 1,
        offset,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return PaginatedListResponse(
        data=[_row_to_vault(r) for r in items],
        has_more=has_more,
        next_cursor=None,
    )


@router.get("/{vault_id}", response_model=VaultResponse)
async def get_vault(vault_id: str) -> VaultResponse:
    """Retrieve a vault by id."""
    row = await _get_vault_or_404(vault_id)
    return _row_to_vault(row)


@router.delete("/{vault_id}", status_code=204)
async def delete_vault(vault_id: str):
    """Delete a vault and all its credentials (cascade)."""
    try:
        uid = uuid.UUID(vault_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Vault {vault_id} not found"},
        )
    result = await execute("DELETE FROM vaults WHERE id = $1", uid)
    # result is e.g. "DELETE 1" or "DELETE 0"
    if result == "DELETE 0":
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Vault {vault_id} not found"},
        )


@router.post("/{vault_id}/archive", response_model=VaultResponse)
async def archive_vault(vault_id: str) -> VaultResponse:
    """Archive a vault: set archived_at, purge credential secrets."""
    row = await _get_vault_or_404(vault_id)

    if row["archived_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "message": f"Vault {vault_id} is already archived"},
        )

    uid = uuid.UUID(vault_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Archive the vault
            updated = await conn.fetchrow(
                """
                UPDATE vaults SET archived_at = now(), updated_at = now()
                WHERE id = $1
                RETURNING *
                """,
                uid,
            )
            # Archive all credentials and purge secrets
            await conn.execute(
                """
                UPDATE credentials
                SET archived_at = now(),
                    updated_at = now(),
                    encrypted_secrets = NULL
                WHERE vault_id = $1 AND archived_at IS NULL
                """,
                uid,
            )
    return _row_to_vault(updated)


# ---------------------------------------------------------------------------
# Credential CRUD
# ---------------------------------------------------------------------------


def _build_secrets_json(req: CreateCredentialRequest) -> str:
    """Extract secret fields from the creation request and return JSON string."""
    if req.credential_type == "bearer_token":
        return json.dumps({"token": req.token})
    elif req.credential_type == "api_key":
        return json.dumps({"api_key": req.api_key})
    elif req.credential_type == "oauth":
        secrets: dict = {"access_token": req.access_token}
        if req.refresh_token is not None:
            secrets["refresh_token"] = req.refresh_token
        if req.client_secret is not None:
            secrets["client_secret"] = req.client_secret
        return json.dumps(secrets)
    raise ValueError(f"Unknown credential_type: {req.credential_type}")


@router.post("/{vault_id}/credentials", status_code=201, response_model=CredentialResponse)
async def create_credential(vault_id: str, body: CreateCredentialRequest) -> CredentialResponse:
    """Create a credential within a vault."""
    vault_row = await _get_vault_or_404(vault_id)

    # Reject if vault is archived
    if vault_row["archived_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "message": f"Vault {vault_id} is archived"},
        )

    uid_vault = uuid.UUID(vault_id)

    # Enforce 20-credential limit
    count_row = await fetch_one(
        "SELECT count(*) AS cnt FROM credentials WHERE vault_id = $1 AND archived_at IS NULL",
        uid_vault,
    )
    if count_row["cnt"] >= MAX_CREDENTIALS_PER_VAULT:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conflict",
                "message": f"Vault {vault_id} already has {MAX_CREDENTIALS_PER_VAULT} active credentials",
            },
        )

    # Encrypt secrets
    secrets_json = _build_secrets_json(body)
    encrypted = get_encryption_service().encrypt(secrets_json)

    # Determine target fields
    mcp_server_url = getattr(body, "mcp_server_url", None)
    provider = getattr(body, "provider", None)
    client_id = getattr(body, "client_id", None)

    cred_id = str(uuid.uuid4())

    try:
        row = await fetch_one(
            """
            INSERT INTO credentials
                (id, vault_id, credential_type, mcp_server_url, provider, encrypted_secrets, client_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            uuid.UUID(cred_id),
            uid_vault,
            body.credential_type,
            mcp_server_url,
            provider,
            encrypted,
            client_id,
        )
    except asyncpg.UniqueViolationError:
        # Duplicate mcp_server_url or provider within the vault
        target = mcp_server_url or provider
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conflict",
                "message": f"A credential for '{target}' already exists in vault {vault_id}",
            },
        )

    return _row_to_credential(row)


@router.get("/{vault_id}/credentials", response_model=PaginatedListResponse)
async def list_credentials(
    vault_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedListResponse:
    """Return a paginated list of credentials in a vault (secrets omitted)."""
    await _get_vault_or_404(vault_id)
    uid = uuid.UUID(vault_id)

    rows = await fetch_all(
        """
        SELECT * FROM credentials
        WHERE vault_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        uid,
        limit + 1,
        offset,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return PaginatedListResponse(
        data=[_row_to_credential(r) for r in items],
        has_more=has_more,
        next_cursor=None,
    )


@router.get("/{vault_id}/credentials/{credential_id}", response_model=CredentialResponse)
async def get_credential(vault_id: str, credential_id: str) -> CredentialResponse:
    """Retrieve a credential by id (secrets omitted)."""
    await _get_vault_or_404(vault_id)

    try:
        cred_uid = uuid.UUID(credential_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Credential {credential_id} not found"},
        )

    row = await fetch_one(
        "SELECT * FROM credentials WHERE id = $1 AND vault_id = $2",
        cred_uid,
        uuid.UUID(vault_id),
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Credential {credential_id} not found"},
        )
    return _row_to_credential(row)


@router.patch("/{vault_id}/credentials/{credential_id}", response_model=CredentialResponse)
async def update_credential(
    vault_id: str, credential_id: str, body: UpdateCredentialRequest
) -> CredentialResponse:
    """Update secret fields on a credential."""
    await _get_vault_or_404(vault_id)

    try:
        cred_uid = uuid.UUID(credential_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Credential {credential_id} not found"},
        )

    row = await fetch_one(
        "SELECT * FROM credentials WHERE id = $1 AND vault_id = $2",
        cred_uid,
        uuid.UUID(vault_id),
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Credential {credential_id} not found"},
        )

    # Reject if credential is archived
    if row["archived_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "message": f"Credential {credential_id} is archived"},
        )

    # Build new secrets from the update body (only non-None fields)
    secrets: dict = {}
    for field in ("token", "api_key", "access_token", "refresh_token", "client_secret"):
        val = getattr(body, field, None)
        if val is not None:
            secrets[field] = val

    if not secrets:
        # Nothing to update — just return current state
        return _row_to_credential(row)

    encrypted = get_encryption_service().encrypt(json.dumps(secrets))

    updated = await fetch_one(
        """
        UPDATE credentials
        SET encrypted_secrets = $1, updated_at = now()
        WHERE id = $2 AND vault_id = $3
        RETURNING *
        """,
        encrypted,
        cred_uid,
        uuid.UUID(vault_id),
    )
    return _row_to_credential(updated)


@router.delete("/{vault_id}/credentials/{credential_id}", status_code=204)
async def delete_credential(vault_id: str, credential_id: str):
    """Delete a credential."""
    await _get_vault_or_404(vault_id)

    try:
        cred_uid = uuid.UUID(credential_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Credential {credential_id} not found"},
        )

    result = await execute(
        "DELETE FROM credentials WHERE id = $1 AND vault_id = $2",
        cred_uid,
        uuid.UUID(vault_id),
    )
    if result == "DELETE 0":
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Credential {credential_id} not found"},
        )


@router.post("/{vault_id}/credentials/{credential_id}/archive", response_model=CredentialResponse)
async def archive_credential(vault_id: str, credential_id: str) -> CredentialResponse:
    """Archive a credential: set archived_at, purge encrypted secrets."""
    await _get_vault_or_404(vault_id)

    try:
        cred_uid = uuid.UUID(credential_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Credential {credential_id} not found"},
        )

    row = await fetch_one(
        "SELECT * FROM credentials WHERE id = $1 AND vault_id = $2",
        cred_uid,
        uuid.UUID(vault_id),
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Credential {credential_id} not found"},
        )

    if row["archived_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "message": f"Credential {credential_id} is already archived"},
        )

    updated = await fetch_one(
        """
        UPDATE credentials
        SET archived_at = now(), updated_at = now(), encrypted_secrets = NULL
        WHERE id = $1 AND vault_id = $2
        RETURNING *
        """,
        cred_uid,
        uuid.UUID(vault_id),
    )
    return _row_to_credential(updated)
