"""Unit tests for auth middleware (bearer token validation).

Validates: Requirements 18.1, 18.2, 18.3
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import verify_bearer_token


@pytest.fixture()
def auth_client(monkeypatch: pytest.MonkeyPatch):
    """App with a single authenticated route for testing auth in isolation."""
    monkeypatch.setenv("LINCHPIN_API_KEY", "test-secret-key")

    app = FastAPI()
    router = APIRouter(prefix="/v1", dependencies=[Depends(verify_bearer_token)])

    @router.get("/ping")
    async def ping():
        return {"ok": True}

    app.include_router(router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_missing_authorization_header_returns_401(auth_client):
    """Request without Authorization header should be rejected."""
    resp = auth_client.get("/v1/ping")
    assert resp.status_code == 401
    body = resp.json()["detail"]
    assert body["error"] == "unauthorized"


def test_malformed_authorization_header_returns_401(auth_client):
    """Authorization header without 'Bearer ' prefix should be rejected."""
    resp = auth_client.get("/v1/ping", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401
    body = resp.json()["detail"]
    assert body["error"] == "unauthorized"


def test_wrong_token_returns_401(auth_client):
    """Bearer token that doesn't match LINCHPIN_API_KEY should be rejected."""
    resp = auth_client.get("/v1/ping", headers={"Authorization": "Bearer wrong-key"})
    assert resp.status_code == 401
    body = resp.json()["detail"]
    assert body["error"] == "unauthorized"


def test_valid_token_passes_auth(auth_client):
    """Request with correct bearer token should pass auth."""
    resp = auth_client.get("/v1/ping", headers={"Authorization": "Bearer test-secret-key"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_empty_token_returns_401(auth_client):
    """Bearer token that is empty should be rejected."""
    resp = auth_client.get("/v1/ping", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


def test_health_endpoint_no_auth_required(auth_client):
    """The /health endpoint should not require authentication."""
    resp = auth_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_bearer_case_insensitive(auth_client):
    """The 'Bearer' prefix should be case-insensitive."""
    resp = auth_client.get("/v1/ping", headers={"Authorization": "bearer test-secret-key"})
    assert resp.status_code == 200


def test_no_api_key_configured_rejects_all(auth_client, monkeypatch):
    """When LINCHPIN_API_KEY is empty/unset, all requests should be rejected."""
    monkeypatch.setenv("LINCHPIN_API_KEY", "")
    resp = auth_client.get("/v1/ping", headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 401
