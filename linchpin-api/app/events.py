"""Event store with cursor generation and pagination.

Provides append_event (monotonic seq, opaque cursor, insert + session update)
and get_events (cursor-based pagination with after_cursor, limit, next_cursor).

Validates: Requirements 7.3, 8.1, 8.2, 8.3, 8.4
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone

from app.db import fetch_all, fetch_one
from app.models import Event, EventResponse, PaginatedEventsResponse


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def encode_cursor(seq: int) -> str:
    """Encode a sequence number into an opaque base64 cursor string."""
    return base64.urlsafe_b64encode(str(seq).encode()).decode()


def decode_cursor(cursor: str) -> int:
    """Decode an opaque base64 cursor string back to a sequence number."""
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, Exception) as exc:
        raise ValueError(f"Invalid cursor: {cursor}") from exc


# ---------------------------------------------------------------------------
# append_event
# ---------------------------------------------------------------------------


async def append_event(
    session_id: str,
    event_type: str,
    payload: dict,
) -> Event:
    """Append an event to a session's event log.

    - Assigns the next monotonic seq for this session.
    - Generates an opaque cursor (base64-encoded seq).
    - Inserts into the events table.
    - Updates the session's last_event_cursor and stats.
    - Returns the Event model.
    """
    sid = uuid.UUID(session_id)

    # Get next seq number
    row = await fetch_one(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE session_id = $1",
        sid,
    )
    next_seq: int = row["next_seq"]
    cursor = encode_cursor(next_seq)

    # Insert event
    payload_json = json.dumps(payload)
    event_row = await fetch_one(
        """
        INSERT INTO events (session_id, cursor, seq, type, payload)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        RETURNING *
        """,
        sid,
        cursor,
        next_seq,
        event_type,
        payload_json,
    )

    # Update session stats and last_event_cursor
    # Increment total_events; also increment tool_calls if it's a tool_use event,
    # and model_turns if it's an agent.message event.
    stats_update_parts = ["stats = jsonb_set(stats, '{total_events}', to_jsonb((stats->>'total_events')::int + 1))"]
    if event_type == "agent.tool_use":
        stats_update_parts.append(
            "stats = jsonb_set(stats, '{tool_calls}', to_jsonb((stats->>'tool_calls')::int + 1))"
        )
    if event_type == "agent.message":
        stats_update_parts.append(
            "stats = jsonb_set(stats, '{model_turns}', to_jsonb((stats->>'model_turns')::int + 1))"
        )

    # Build a single UPDATE that chains the jsonb_set calls
    # We need to nest them for multiple updates in one statement
    if event_type == "agent.tool_use":
        stats_expr = (
            "jsonb_set("
            "jsonb_set(stats, '{total_events}', to_jsonb((stats->>'total_events')::int + 1)), "
            "'{tool_calls}', to_jsonb((stats->>'tool_calls')::int + 1))"
        )
    elif event_type == "agent.message":
        stats_expr = (
            "jsonb_set("
            "jsonb_set(stats, '{total_events}', to_jsonb((stats->>'total_events')::int + 1)), "
            "'{model_turns}', to_jsonb((stats->>'model_turns')::int + 1))"
        )
    else:
        stats_expr = "jsonb_set(stats, '{total_events}', to_jsonb((stats->>'total_events')::int + 1))"

    await fetch_one(
        f"""
        UPDATE sessions
        SET last_event_cursor = $1,
            stats = {stats_expr},
            updated_at = now()
        WHERE id = $2
        RETURNING id
        """,
        cursor,
        sid,
    )

    return Event(
        session_id=str(event_row["session_id"]),
        cursor=event_row["cursor"],
        seq=event_row["seq"],
        type=event_row["type"],
        payload=json.loads(event_row["payload"]) if isinstance(event_row["payload"], str) else event_row["payload"],
        processed_at=event_row["processed_at"],
    )


# ---------------------------------------------------------------------------
# get_events
# ---------------------------------------------------------------------------


async def get_events(
    session_id: str,
    after_cursor: str | None = None,
    limit: int = 50,
) -> PaginatedEventsResponse:
    """Retrieve events for a session with cursor-based pagination.

    - If after_cursor is provided, returns events with seq > decoded cursor seq.
    - Results ordered by seq ASC.
    - Fetches limit + 1 rows to detect has_more / next_cursor.
    """
    sid = uuid.UUID(session_id)

    if after_cursor is not None:
        after_seq = decode_cursor(after_cursor)
        rows = await fetch_all(
            """
            SELECT * FROM events
            WHERE session_id = $1 AND seq > $2
            ORDER BY seq ASC
            LIMIT $3
            """,
            sid,
            after_seq,
            limit + 1,
        )
    else:
        rows = await fetch_all(
            """
            SELECT * FROM events
            WHERE session_id = $1
            ORDER BY seq ASC
            LIMIT $2
            """,
            sid,
            limit + 1,
        )

    has_more = len(rows) > limit
    result_rows = rows[:limit]

    events = [
        EventResponse(
            session_id=str(r["session_id"]),
            cursor=r["cursor"],
            seq=r["seq"],
            type=r["type"],
            payload=json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
            processed_at=r["processed_at"],
        )
        for r in result_rows
    ]

    next_cursor = events[-1].cursor if has_more and events else None

    return PaginatedEventsResponse(events=events, next_cursor=next_cursor)
