"""Unit tests for event store: cursor encoding/decoding, append_event, get_events.

Validates: Requirements 7.3, 8.1, 8.2, 8.3, 8.4
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.events import append_event, decode_cursor, encode_cursor, get_events


# ---------------------------------------------------------------------------
# Cursor encoding / decoding
# ---------------------------------------------------------------------------


class TestCursorEncoding:
    """Tests for encode_cursor and decode_cursor helpers."""

    def test_round_trip(self):
        """Encoding then decoding returns the original seq."""
        for seq in (1, 10, 100, 999999):
            assert decode_cursor(encode_cursor(seq)) == seq

    def test_encode_produces_string(self):
        """encode_cursor returns a non-empty string."""
        cursor = encode_cursor(42)
        assert isinstance(cursor, str)
        assert len(cursor) > 0

    def test_different_seqs_produce_different_cursors(self):
        """Different sequence numbers produce different cursors."""
        c1 = encode_cursor(1)
        c2 = encode_cursor(2)
        assert c1 != c2

    def test_decode_invalid_cursor_raises(self):
        """Decoding a non-base64 / non-integer cursor raises ValueError."""
        with pytest.raises(ValueError, match="Invalid cursor"):
            decode_cursor("not-valid-base64!!!")

    def test_decode_non_integer_content_raises(self):
        """Decoding a valid base64 string that isn't an integer raises ValueError."""
        import base64
        bad = base64.urlsafe_b64encode(b"hello").decode()
        with pytest.raises(ValueError, match="Invalid cursor"):
            decode_cursor(bad)

    def test_encode_seq_zero(self):
        """Seq 0 round-trips correctly (edge case)."""
        assert decode_cursor(encode_cursor(0)) == 0


# ---------------------------------------------------------------------------
# append_event (mocked DB)
# ---------------------------------------------------------------------------


def _fake_event_row(session_id, cursor, seq, event_type, payload):
    """Build a fake asyncpg Record-like dict for an event row."""
    return {
        "session_id": uuid.UUID(session_id),
        "cursor": cursor,
        "seq": seq,
        "type": event_type,
        "payload": payload,
        "processed_at": None,
    }


class TestAppendEvent:
    """Tests for append_event with mocked DB calls."""

    @pytest.mark.asyncio
    @patch("app.events.fetch_one", new_callable=AsyncMock)
    async def test_append_first_event(self, mock_fetch):
        """Appending the first event to a session assigns seq=1."""
        sid = str(uuid.uuid4())
        payload = {"content": "hello"}

        # Call 1: SELECT next_seq → 1
        # Call 2: INSERT RETURNING → event row
        # Call 3: UPDATE sessions → id
        mock_fetch.side_effect = [
            {"next_seq": 1},
            _fake_event_row(sid, encode_cursor(1), 1, "user.message", payload),
            {"id": uuid.UUID(sid)},
        ]

        event = await append_event(sid, "user.message", payload)

        assert event.seq == 1
        assert event.cursor == encode_cursor(1)
        assert event.type == "user.message"
        assert event.payload == payload
        assert event.session_id == sid

    @pytest.mark.asyncio
    @patch("app.events.fetch_one", new_callable=AsyncMock)
    async def test_append_increments_seq(self, mock_fetch):
        """Appending to a session with existing events increments seq."""
        sid = str(uuid.uuid4())

        mock_fetch.side_effect = [
            {"next_seq": 5},
            _fake_event_row(sid, encode_cursor(5), 5, "agent.message", {}),
            {"id": uuid.UUID(sid)},
        ]

        event = await append_event(sid, "agent.message", {})

        assert event.seq == 5
        assert event.cursor == encode_cursor(5)

    @pytest.mark.asyncio
    @patch("app.events.fetch_one", new_callable=AsyncMock)
    async def test_append_tool_use_updates_tool_calls_stat(self, mock_fetch):
        """Appending an agent.tool_use event updates tool_calls in stats."""
        sid = str(uuid.uuid4())

        mock_fetch.side_effect = [
            {"next_seq": 2},
            _fake_event_row(sid, encode_cursor(2), 2, "agent.tool_use", {"tool": "bash"}),
            {"id": uuid.UUID(sid)},
        ]

        event = await append_event(sid, "agent.tool_use", {"tool": "bash"})

        assert event.type == "agent.tool_use"
        # Verify the UPDATE query includes tool_calls increment
        update_call = mock_fetch.call_args_list[2]
        update_query = update_call[0][0]
        assert "tool_calls" in update_query

    @pytest.mark.asyncio
    @patch("app.events.fetch_one", new_callable=AsyncMock)
    async def test_append_agent_message_updates_model_turns_stat(self, mock_fetch):
        """Appending an agent.message event updates model_turns in stats."""
        sid = str(uuid.uuid4())

        mock_fetch.side_effect = [
            {"next_seq": 3},
            _fake_event_row(sid, encode_cursor(3), 3, "agent.message", {"text": "hi"}),
            {"id": uuid.UUID(sid)},
        ]

        event = await append_event(sid, "agent.message", {"text": "hi"})

        assert event.type == "agent.message"
        update_call = mock_fetch.call_args_list[2]
        update_query = update_call[0][0]
        assert "model_turns" in update_query


# ---------------------------------------------------------------------------
# get_events (mocked DB)
# ---------------------------------------------------------------------------


def _fake_event_rows(session_id, count, start_seq=1):
    """Build a list of fake event rows."""
    sid = uuid.UUID(session_id)
    return [
        {
            "session_id": sid,
            "cursor": encode_cursor(start_seq + i),
            "seq": start_seq + i,
            "type": "user.message",
            "payload": {"i": i},
            "processed_at": None,
        }
        for i in range(count)
    ]


class TestGetEvents:
    """Tests for get_events with mocked DB calls."""

    @pytest.mark.asyncio
    @patch("app.events.fetch_all", new_callable=AsyncMock)
    async def test_get_events_no_cursor(self, mock_fetch):
        """Without after_cursor, returns events from the beginning."""
        sid = str(uuid.uuid4())
        mock_fetch.return_value = _fake_event_rows(sid, 3)

        result = await get_events(sid, after_cursor=None, limit=10)

        assert len(result.events) == 3
        assert result.next_cursor is None
        # Verify ordering
        seqs = [e.seq for e in result.events]
        assert seqs == sorted(seqs)

    @pytest.mark.asyncio
    @patch("app.events.fetch_all", new_callable=AsyncMock)
    async def test_get_events_with_cursor(self, mock_fetch):
        """With after_cursor, returns only events after that cursor."""
        sid = str(uuid.uuid4())
        # Simulate events with seq 4, 5 (after cursor for seq 3)
        mock_fetch.return_value = _fake_event_rows(sid, 2, start_seq=4)

        result = await get_events(sid, after_cursor=encode_cursor(3), limit=10)

        assert len(result.events) == 2
        assert result.events[0].seq == 4
        assert result.next_cursor is None

    @pytest.mark.asyncio
    @patch("app.events.fetch_all", new_callable=AsyncMock)
    async def test_get_events_pagination_has_more(self, mock_fetch):
        """When more events exist than limit, next_cursor is set."""
        sid = str(uuid.uuid4())
        # Return limit + 1 rows to signal has_more
        mock_fetch.return_value = _fake_event_rows(sid, 4)

        result = await get_events(sid, after_cursor=None, limit=3)

        assert len(result.events) == 3
        assert result.next_cursor is not None
        # next_cursor should be the cursor of the last returned event
        assert result.next_cursor == result.events[-1].cursor

    @pytest.mark.asyncio
    @patch("app.events.fetch_all", new_callable=AsyncMock)
    async def test_get_events_pagination_no_more(self, mock_fetch):
        """When events fit within limit, next_cursor is None."""
        sid = str(uuid.uuid4())
        mock_fetch.return_value = _fake_event_rows(sid, 2)

        result = await get_events(sid, after_cursor=None, limit=5)

        assert len(result.events) == 2
        assert result.next_cursor is None

    @pytest.mark.asyncio
    @patch("app.events.fetch_all", new_callable=AsyncMock)
    async def test_get_events_empty(self, mock_fetch):
        """When no events exist, returns empty list with no cursor."""
        sid = str(uuid.uuid4())
        mock_fetch.return_value = []

        result = await get_events(sid, after_cursor=None, limit=10)

        assert result.events == []
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_get_events_invalid_cursor_raises(self):
        """Passing an invalid cursor raises ValueError."""
        sid = str(uuid.uuid4())
        with pytest.raises(ValueError, match="Invalid cursor"):
            await get_events(sid, after_cursor="bad-cursor!!!", limit=10)

    @pytest.mark.asyncio
    @patch("app.events.fetch_all", new_callable=AsyncMock)
    async def test_get_events_limit_exactly_met(self, mock_fetch):
        """When exactly limit events exist, next_cursor is None."""
        sid = str(uuid.uuid4())
        mock_fetch.return_value = _fake_event_rows(sid, 5)

        result = await get_events(sid, after_cursor=None, limit=5)

        assert len(result.events) == 5
        assert result.next_cursor is None

    # ---- v0.2.0 item #12 — types[] filter ----

    @pytest.mark.asyncio
    @patch("app.events.fetch_all", new_callable=AsyncMock)
    async def test_get_events_types_filter_appears_in_sql(self, mock_fetch):
        """Passing types= adds a `type = ANY($N)` clause to the SQL."""
        sid = str(uuid.uuid4())
        mock_fetch.return_value = []

        await get_events(sid, after_cursor=None, limit=10, types=["agent.message"])
        sql = mock_fetch.await_args.args[0]
        assert "type = ANY" in sql
        # The types list is passed positionally as the second arg (after sid).
        positional = mock_fetch.await_args.args
        assert positional[2] == ["agent.message"]

    @pytest.mark.asyncio
    @patch("app.events.fetch_all", new_callable=AsyncMock)
    async def test_get_events_types_and_cursor_compose(self, mock_fetch):
        """types= and after_cursor= both apply when both are present."""
        sid = str(uuid.uuid4())
        mock_fetch.return_value = []

        await get_events(
            sid,
            after_cursor=encode_cursor(3),
            limit=10,
            types=["agent.message", "agent.tool_use"],
        )
        sql = mock_fetch.await_args.args[0]
        assert "seq >" in sql and "type = ANY" in sql

    @pytest.mark.asyncio
    @patch("app.events.fetch_all", new_callable=AsyncMock)
    async def test_get_events_empty_types_list_skips_filter(self, mock_fetch):
        """An empty types=[] list is the same as no filter — no ANY clause."""
        sid = str(uuid.uuid4())
        mock_fetch.return_value = []

        await get_events(sid, after_cursor=None, limit=10, types=[])
        sql = mock_fetch.await_args.args[0]
        assert "type = ANY" not in sql
