"""Tests for the webhook subsystem (v0.2.0 item #14).

Covers HMAC signing, CRUD endpoints, the enqueue path's event-type
allowlist, delivery state transitions (success / failure / exhaustion),
and the worker's claim-batch logic for the parts that don't need a
live Postgres.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.webhooks import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    SECRET_PREFIX,
    SIGNATURE_HEADER,
    WEBHOOK_RELEVANT_EVENTS,
    _next_attempt_at,
    attempt_delivery,
    deliver_one,
    enqueue_event,
    generate_secret,
    sign_payload,
    verify_signature,
)


AUTH = {"Authorization": "Bearer test-secret-key"}


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


class TestSigning:
    def test_generate_secret_prefixed(self):
        s = generate_secret()
        assert s.startswith(SECRET_PREFIX)
        assert len(s) > len(SECRET_PREFIX) + 30  # 32 bytes b64 → ≥43 chars

    def test_generate_secret_unique(self):
        assert generate_secret() != generate_secret()

    def test_sign_then_verify_round_trip(self):
        secret = generate_secret()
        payload = '{"event_type": "session.created"}'
        sig = sign_payload(secret, payload)
        assert sig.startswith("t=")
        assert ",v1=" in sig
        assert verify_signature(secret, payload, sig) is True

    def test_verify_rejects_tampered_payload(self):
        secret = generate_secret()
        sig = sign_payload(secret, '{"a": 1}')
        assert verify_signature(secret, '{"a": 2}', sig) is False

    def test_verify_rejects_wrong_secret(self):
        secret_a = generate_secret()
        secret_b = generate_secret()
        sig = sign_payload(secret_a, "hi")
        assert verify_signature(secret_b, "hi", sig) is False

    def test_verify_rejects_malformed_header(self):
        assert verify_signature(generate_secret(), "p", "garbage") is False
        assert verify_signature(generate_secret(), "p", "t=abc,v1=x") is False


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


class TestBackoff:
    def test_first_failure_uses_first_backoff(self):
        now = datetime.now(tz=timezone.utc)
        result = _next_attempt_at(0)
        delta = (result - now).total_seconds()
        assert RETRY_BACKOFF_SECONDS[0] - 1 <= delta <= RETRY_BACKOFF_SECONDS[0] + 1

    def test_caps_at_last_backoff(self):
        now = datetime.now(tz=timezone.utc)
        result = _next_attempt_at(99)
        delta = (result - now).total_seconds()
        last = RETRY_BACKOFF_SECONDS[-1]
        assert last - 1 <= delta <= last + 1


# ---------------------------------------------------------------------------
# Delivery worker primitives
# ---------------------------------------------------------------------------


class TestAttemptDelivery:
    @pytest.mark.asyncio
    async def test_success_returns_status_and_no_error(self, monkeypatch):
        async def _fake_post(self, url, content=None, headers=None):
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "ok"
            return resp
        monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
        status, err = await attempt_delivery(
            "http://example.com/", generate_secret(), '{"x":1}'
        )
        assert status == 200
        assert err is None

    @pytest.mark.asyncio
    async def test_5xx_returns_error(self, monkeypatch):
        async def _fake_post(self, url, content=None, headers=None):
            resp = MagicMock()
            resp.status_code = 503
            resp.text = "down"
            return resp
        monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
        status, err = await attempt_delivery(
            "http://example.com/", generate_secret(), '{}'
        )
        assert status == 503
        assert "503" in err

    @pytest.mark.asyncio
    async def test_network_error_returns_none_status(self, monkeypatch):
        async def _fake_post(self, url, content=None, headers=None):
            raise httpx.ConnectError("connection refused")
        monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
        status, err = await attempt_delivery(
            "http://example.com/", generate_secret(), '{}'
        )
        assert status is None
        assert "ConnectError" in err

    @pytest.mark.asyncio
    async def test_signature_header_set(self, monkeypatch):
        captured: dict = {}
        async def _fake_post(self, url, content=None, headers=None):
            captured.update(headers)
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "ok"
            return resp
        monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
        secret = generate_secret()
        await attempt_delivery("http://example.com/", secret, '{"x":1}')
        assert SIGNATURE_HEADER in captured
        # Receiver should be able to verify what we sent
        assert verify_signature(secret, '{"x":1}', captured[SIGNATURE_HEADER])


class TestDeliverOne:
    @pytest.mark.asyncio
    async def test_success_marks_succeeded(self, monkeypatch):
        calls = []
        async def fake_execute(sql, *args):
            calls.append((sql, args))
        async def fake_attempt(url, secret, payload, client=None):
            return 200, None

        monkeypatch.setattr("app.webhooks.execute", fake_execute)
        monkeypatch.setattr("app.webhooks.attempt_delivery", fake_attempt)
        row = {
            "id": uuid.uuid4(),
            "endpoint_url": "http://x.com",
            "endpoint_secret": "whsec_x",
            "payload": "{}",
            "attempts": 0,
        }
        result = await deliver_one(row)
        assert result == "succeeded"
        # The UPDATE used the 'succeeded' branch
        assert any("status='succeeded'" in c[0] for c in calls)

    @pytest.mark.asyncio
    async def test_failure_schedules_retry(self, monkeypatch):
        calls = []
        async def fake_execute(sql, *args):
            calls.append((sql, args))
        async def fake_attempt(url, secret, payload, client=None):
            return 503, "HTTP 503"

        monkeypatch.setattr("app.webhooks.execute", fake_execute)
        monkeypatch.setattr("app.webhooks.attempt_delivery", fake_attempt)
        row = {
            "id": uuid.uuid4(),
            "endpoint_url": "http://x.com",
            "endpoint_secret": "whsec_x",
            "payload": "{}",
            "attempts": 0,  # first attempt
        }
        result = await deliver_one(row)
        assert result == "failed"
        assert any("status='failed'" in c[0] for c in calls)

    @pytest.mark.asyncio
    async def test_exhausts_after_max_attempts(self, monkeypatch):
        calls = []
        async def fake_execute(sql, *args):
            calls.append((sql, args))
        async def fake_attempt(url, secret, payload, client=None):
            return 503, "HTTP 503"

        monkeypatch.setattr("app.webhooks.execute", fake_execute)
        monkeypatch.setattr("app.webhooks.attempt_delivery", fake_attempt)
        row = {
            "id": uuid.uuid4(),
            "endpoint_url": "http://x.com",
            "endpoint_secret": "whsec_x",
            "payload": "{}",
            "attempts": MAX_ATTEMPTS - 1,  # one more failure = exhausted
        }
        result = await deliver_one(row)
        assert result == "exhausted"
        assert any("status='exhausted'" in c[0] for c in calls)


# ---------------------------------------------------------------------------
# enqueue_event
# ---------------------------------------------------------------------------


class TestEnqueueEvent:
    @pytest.mark.asyncio
    async def test_ignores_unrelated_event_types(self, monkeypatch):
        # If the event type is not in the allowlist, no DB calls happen
        # at all — even fetch_all should be skipped.
        async def fail(*a, **kw):
            raise AssertionError("should not query DB for unrelated events")
        monkeypatch.setattr("app.webhooks.fetch_all", fail)
        monkeypatch.setattr("app.webhooks.execute", fail)
        n = await enqueue_event("agent.unknown_event_type_xyz", {"foo": 1})
        assert n == 0

    @pytest.mark.asyncio
    async def test_inserts_one_row_per_matching_endpoint(self, monkeypatch):
        endpoints = [{"id": uuid.uuid4()}, {"id": uuid.uuid4()}]
        insert_calls = []

        async def fake_fetch_all(sql, *args):
            return endpoints

        async def fake_execute(sql, *args):
            insert_calls.append((sql, args))

        monkeypatch.setattr("app.webhooks.fetch_all", fake_fetch_all)
        monkeypatch.setattr("app.webhooks.execute", fake_execute)

        n = await enqueue_event("session.created", {"id": "s_1"})
        assert n == 2
        assert len(insert_calls) == 2
        # Each insert hits the deliveries table
        for sql, _ in insert_calls:
            assert "INSERT INTO webhook_deliveries" in sql

    def test_known_event_types_are_session_or_vault(self):
        for ev in WEBHOOK_RELEVANT_EVENTS:
            assert ev.startswith(("session.", "vault."))


# ---------------------------------------------------------------------------
# CRUD via the HTTP route
# ---------------------------------------------------------------------------


def _endpoint_row(
    *,
    id: uuid.UUID | None = None,
    url: str = "https://example.com/hook",
    enabled_events: list | None = None,
    enabled: bool = True,
    archived_at=None,
    secret: str | None = None,
    description: str | None = None,
):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    return {
        "id": id or uuid.uuid4(),
        "url": url,
        "secret": secret or "whsec_test",
        "enabled_events": enabled_events or [],
        "description": description,
        "enabled": enabled,
        "created_at": now,
        "updated_at": now,
        "archived_at": archived_at,
    }


@patch("app.routes.webhooks.fetch_one", new_callable=AsyncMock)
def test_create_endpoint_returns_secret_once(mock_fetch, client):
    mock_fetch.return_value = _endpoint_row(secret="whsec_real_value")
    payload = {
        "url": "https://example.com/hook",
        "enabled_events": ["session.created", "session.terminated"],
    }
    resp = client.post("/v1/webhook_endpoints", json=payload, headers=AUTH)
    assert resp.status_code == 201
    body = resp.json()
    assert body["secret"] == "whsec_real_value"
    assert body["enabled"] is True


def test_create_endpoint_rejects_non_http_url(client):
    payload = {"url": "ftp://example.com/", "enabled_events": []}
    resp = client.post("/v1/webhook_endpoints", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_create_endpoint_rejects_unknown_event(client):
    payload = {
        "url": "https://example.com/",
        "enabled_events": ["bogus.event"],
    }
    resp = client.post("/v1/webhook_endpoints", json=payload, headers=AUTH)
    assert resp.status_code == 422


@patch("app.routes.webhooks.fetch_one", new_callable=AsyncMock)
def test_get_endpoint_omits_secret(mock_fetch, client):
    mock_fetch.return_value = _endpoint_row(secret="whsec_should_be_hidden")
    eid = str(uuid.uuid4())
    resp = client.get(f"/v1/webhook_endpoints/{eid}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["secret"] is None


@patch("app.routes.webhooks.execute", new_callable=AsyncMock)
@patch("app.routes.webhooks.fetch_one", new_callable=AsyncMock)
def test_delete_endpoint_archives(mock_fetch, mock_execute, client):
    eid = uuid.uuid4()
    mock_fetch.return_value = {"id": eid}
    resp = client.delete(f"/v1/webhook_endpoints/{eid}", headers=AUTH)
    assert resp.status_code == 204
    mock_execute.assert_awaited_once()


@patch("app.routes.webhooks.fetch_one", new_callable=AsyncMock)
def test_patch_endpoint_rotates_secret_when_requested(mock_fetch, client):
    eid = uuid.uuid4()
    # Two fetch calls: existing row, then updated row
    mock_fetch.side_effect = [
        _endpoint_row(id=eid),
        _endpoint_row(id=eid, secret="whsec_new"),
    ]
    resp = client.patch(
        f"/v1/webhook_endpoints/{eid}",
        json={"rotate_secret": True},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["secret"] == "whsec_new"


def test_endpoints_require_auth(client):
    assert client.get("/v1/webhook_endpoints").status_code == 401
    assert client.post(
        "/v1/webhook_endpoints",
        json={"url": "https://x.com/", "enabled_events": []},
    ).status_code == 401
