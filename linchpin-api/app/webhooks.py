"""Webhook subsystem (v0.2.0 item #14).

Three concerns:

1. **Endpoint management.** Caller-registered HTTP destinations with an
   ``enabled_events`` allowlist. CRUD lives in
   ``app/routes/webhooks.py``; rows live in ``webhook_endpoints``.

2. **Signing.** Each endpoint has an HMAC secret prefixed ``whsec_``.
   Outgoing requests carry a ``Linchpin-Signature: t=<ts>,v1=<hex>``
   header where ``hex = HMAC-SHA256(secret, f"{ts}.{json_payload}")``.
   This is the same scheme Stripe + Vercel use; consumers can lift any
   off-the-shelf verifier.

3. **At-least-once delivery.** Events flow through ``enqueue_event()``,
   which insert one ``webhook_deliveries`` row per matching endpoint.
   The background worker (``delivery_worker_loop``) polls rows whose
   ``status IN ('pending','failed')`` and ``next_attempt_at <= now``,
   POSTs them, and either marks them ``succeeded`` or schedules the
   next attempt with exponential backoff. After ``MAX_ATTEMPTS`` the
   row is retired as ``exhausted`` and the operator is expected to
   investigate via ``GET /v1/webhook_endpoints/{id}/deliveries``.

Vault-credential events are routed through the same enqueue path —
see ``WEBHOOK_RELEVANT_EVENTS`` for the current allowlist of event
types.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.db import execute, fetch_all, fetch_one


logger = logging.getLogger("linchpin-api.webhooks")


SECRET_PREFIX = "whsec_"
SIGNATURE_HEADER = "Linchpin-Signature"

MAX_ATTEMPTS = 5
# Backoff in seconds for attempts 1..MAX_ATTEMPTS. Caps at 1h so a
# half-day outage at the receiver doesn't gum up the queue for days.
RETRY_BACKOFF_SECONDS = (10, 60, 300, 1800, 3600)

# Event types the webhook subsystem can fan out. Adding a new entry here
# requires no other code change — the event-emission hook reads this
# allowlist at runtime.
WEBHOOK_RELEVANT_EVENTS: frozenset[str] = frozenset({
    "session.created",
    "session.status_idle",
    "session.terminated",
    "session.failed",
    "session.requires_action",
    "vault.credential.created",
    "vault.credential.updated",
    "vault.credential.deleted",
})


def generate_secret() -> str:
    """Return a new ``whsec_<base64-32-bytes>`` HMAC secret.

    Each endpoint gets its own. Stored verbatim in the DB; never returned
    on read after creation (the create response is the only place it
    surfaces in clear text — matches Stripe's model).
    """
    raw = secrets.token_urlsafe(32)
    return f"{SECRET_PREFIX}{raw}"


def sign_payload(secret: str, payload: str, *, timestamp: int | None = None) -> str:
    """Build the ``Linchpin-Signature`` header value for ``payload``.

    Signed content is ``f"{timestamp}.{payload}"`` exactly — receivers
    must reconstruct the same string before verifying. Returns
    ``"t=<ts>,v1=<hex>"``.
    """
    if timestamp is None:
        timestamp = int(datetime.now(tz=timezone.utc).timestamp())
    signing_input = f"{timestamp}.{payload}".encode("utf-8")
    digest = hmac.new(
        secret.encode("utf-8"), signing_input, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(secret: str, payload: str, header_value: str) -> bool:
    """Verify a ``Linchpin-Signature`` header against a payload.

    Receivers don't use this — it's exposed for the integration tests
    and any in-process consumer (e.g. the console exercising the
    pipeline against a localhost endpoint).
    """
    parts = dict(p.split("=", 1) for p in header_value.split(",") if "=" in p)
    try:
        ts = int(parts["t"])
        provided = parts["v1"]
    except (KeyError, ValueError):
        return False
    expected = sign_payload(secret, payload, timestamp=ts).split(",", 1)[1]
    return hmac.compare_digest(expected.split("=", 1)[1], provided)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def enqueue_event(event_type: str, payload: dict[str, Any]) -> int:
    """Fan an event out to every endpoint whose ``enabled_events`` matches.

    Returns the number of delivery rows inserted. Safe to call from
    inside ``append_event`` — failures here are logged and swallowed so
    the agent loop doesn't crash on webhook glitches.
    """
    if event_type not in WEBHOOK_RELEVANT_EVENTS:
        return 0
    try:
        rows = await fetch_all(
            """
            SELECT id FROM webhook_endpoints
            WHERE enabled = true
              AND archived_at IS NULL
              AND enabled_events ? $1
            """,
            event_type,
        )
    except Exception:
        logger.exception("webhooks: failed to fetch endpoints for %s", event_type)
        return 0

    if not rows:
        return 0

    delivery_payload = {
        "event_type": event_type,
        "payload": payload,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }
    payload_json = json.dumps(delivery_payload, default=str)

    inserted = 0
    for row in rows:
        try:
            await execute(
                """
                INSERT INTO webhook_deliveries
                    (id, endpoint_id, event_type, payload, next_attempt_at)
                VALUES ($1, $2, $3, $4::jsonb, NOW())
                """,
                uuid.uuid4(),
                row["id"],
                event_type,
                payload_json,
            )
            inserted += 1
        except Exception:
            logger.exception(
                "webhooks: failed to enqueue delivery for endpoint %s", row["id"]
            )
    return inserted


# ---------------------------------------------------------------------------
# Delivery worker
# ---------------------------------------------------------------------------


async def attempt_delivery(
    endpoint_url: str,
    secret: str,
    payload_json: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[int | None, str | None]:
    """POST the payload to the endpoint, returning ``(status_code, error)``.

    ``error`` is ``None`` on success or a short string on failure.
    Network errors yield ``(None, str(exc))``. The signature header is
    computed against the *exact bytes* sent on the wire so receivers
    can verify without re-serializing.
    """
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign_payload(secret, payload_json),
    }
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.post(endpoint_url, content=payload_json, headers=headers)
        if 200 <= resp.status_code < 300:
            return resp.status_code, None
        return resp.status_code, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if own_client:
            await client.aclose()


def _next_attempt_at(attempts: int) -> datetime:
    """Compute the next-attempt timestamp for a delivery with ``attempts``
    failed attempts so far (0 = never attempted; 1 = one failure)."""
    idx = min(attempts, len(RETRY_BACKOFF_SECONDS) - 1)
    return datetime.now(tz=timezone.utc) + timedelta(
        seconds=RETRY_BACKOFF_SECONDS[idx]
    )


async def deliver_one(row: dict, *, client: httpx.AsyncClient | None = None) -> str:
    """Attempt a single delivery; return the resulting status string.

    Mutates ``webhook_deliveries`` to reflect the outcome. The result
    string is one of ``succeeded`` / ``failed`` / ``exhausted``.
    """
    payload_raw = row["payload"]
    if isinstance(payload_raw, str):
        payload_json = payload_raw
    else:
        payload_json = json.dumps(payload_raw, default=str)

    status_code, error = await attempt_delivery(
        row["endpoint_url"], row["endpoint_secret"], payload_json, client=client
    )
    new_attempts = row["attempts"] + 1
    now = datetime.now(tz=timezone.utc)

    if status_code is not None and 200 <= status_code < 300:
        await execute(
            """
            UPDATE webhook_deliveries
            SET status='succeeded',
                attempts=$1,
                last_attempt_at=$2,
                last_status_code=$3,
                last_error=NULL,
                completed_at=$2
            WHERE id=$4
            """,
            new_attempts,
            now,
            status_code,
            row["id"],
        )
        return "succeeded"

    if new_attempts >= MAX_ATTEMPTS:
        await execute(
            """
            UPDATE webhook_deliveries
            SET status='exhausted',
                attempts=$1,
                last_attempt_at=$2,
                last_status_code=$3,
                last_error=$4,
                completed_at=$2
            WHERE id=$5
            """,
            new_attempts,
            now,
            status_code,
            error,
            row["id"],
        )
        return "exhausted"

    await execute(
        """
        UPDATE webhook_deliveries
        SET status='failed',
            attempts=$1,
            last_attempt_at=$2,
            last_status_code=$3,
            last_error=$4,
            next_attempt_at=$5
        WHERE id=$6
        """,
        new_attempts,
        now,
        status_code,
        error,
        _next_attempt_at(new_attempts),
        row["id"],
    )
    return "failed"


async def claim_pending_batch(limit: int = 20) -> list[dict]:
    """Atomically claim a batch of pending/failed-and-due deliveries.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple worker
    instances (future) don't double-deliver, plus an immediate
    ``next_attempt_at = far_future`` to take rows out of contention for
    the duration of the attempt.
    """
    rows = await fetch_all(
        """
        WITH claimed AS (
          SELECT wd.id
          FROM webhook_deliveries wd
          WHERE wd.status IN ('pending','failed')
            AND wd.next_attempt_at <= NOW()
          ORDER BY wd.next_attempt_at
          LIMIT $1
          FOR UPDATE SKIP LOCKED
        )
        UPDATE webhook_deliveries wd
        SET next_attempt_at = NOW() + INTERVAL '1 hour'
        FROM claimed
        WHERE wd.id = claimed.id
        RETURNING wd.id, wd.endpoint_id, wd.event_type, wd.payload, wd.attempts
        """,
        limit,
    )
    if not rows:
        return []

    # Hydrate endpoint info for each row.
    out: list[dict] = []
    for r in rows:
        ep = await fetch_one(
            "SELECT url, secret, enabled FROM webhook_endpoints WHERE id = $1",
            r["endpoint_id"],
        )
        if ep is None or not ep["enabled"]:
            # Endpoint vanished or got disabled between enqueue and
            # delivery — retire the row instead of looping forever.
            await execute(
                """UPDATE webhook_deliveries SET status='exhausted',
                       last_error='endpoint missing or disabled',
                       completed_at=NOW() WHERE id=$1""",
                r["id"],
            )
            continue
        out.append({
            "id": r["id"],
            "endpoint_id": r["endpoint_id"],
            "endpoint_url": ep["url"],
            "endpoint_secret": ep["secret"],
            "event_type": r["event_type"],
            "payload": r["payload"],
            "attempts": r["attempts"],
        })
    return out


async def delivery_worker_loop(*, poll_interval: float = 2.0) -> None:
    """Long-running task: claim and deliver pending webhooks forever.

    Spawned from ``main.lifespan``. The poll interval defaults to 2s
    (low because retries are scheduled to-the-second); on each tick we
    process up to 20 due deliveries.
    """
    logger.info("webhooks: delivery worker started (poll=%.1fs)", poll_interval)
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                batch = await claim_pending_batch()
                for row in batch:
                    try:
                        result = await deliver_one(row, client=client)
                        logger.info(
                            "webhooks: delivery %s for %s → %s",
                            row["id"], row["event_type"], result,
                        )
                    except Exception:
                        logger.exception(
                            "webhooks: deliver_one crashed for %s", row["id"]
                        )
            except asyncio.CancelledError:
                logger.info("webhooks: delivery worker cancelled, exiting")
                raise
            except Exception:
                logger.exception("webhooks: worker loop tick failed")
            await asyncio.sleep(poll_interval)


def worker_enabled() -> bool:
    """Whether the delivery worker should run at all in this process.

    Controlled by ``LINCHPIN_WEBHOOKS_WORKER`` env var; default ``true``.
    Tests use ``LINCHPIN_WEBHOOKS_WORKER=false`` to skip the background
    task so they don't have to mock HTTP calls.
    """
    return os.environ.get("LINCHPIN_WEBHOOKS_WORKER", "true").lower() != "false"
