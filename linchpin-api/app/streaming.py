"""In-memory streaming buffer for token deltas.

Deltas are ephemeral — they live in memory only while the model is generating.
The final agent.message is persisted to Postgres as before. This avoids
hundreds of DB writes per response and keeps the connection pool healthy.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("linchpin-api.streaming")


@dataclass
class StreamingState:
    """In-flight streaming state for a single model turn."""
    message_id: str
    accumulated_text: str = ""
    done: bool = False


# Global registry: session_id -> StreamingState
_streams: dict[str, StreamingState] = {}
_lock = asyncio.Lock()


async def start_stream(session_id: str, message_id: str) -> None:
    """Register a new streaming turn for a session."""
    async with _lock:
        _streams[session_id] = StreamingState(message_id=message_id)


async def append_delta(session_id: str, delta: str) -> None:
    """Append a text delta to the in-flight stream."""
    async with _lock:
        state = _streams.get(session_id)
        if state:
            state.accumulated_text += delta


async def finish_stream(session_id: str) -> None:
    """Mark the stream as done and clean up after a short delay."""
    async with _lock:
        state = _streams.get(session_id)
        if state:
            state.done = True
    # Clean up after 5 seconds (gives polling time to pick up the final state)
    await asyncio.sleep(5)
    async with _lock:
        _streams.pop(session_id, None)


async def get_stream(session_id: str) -> dict | None:
    """Get the current streaming state for a session.

    Returns None if no active stream, or a dict with:
    - message_id: str
    - text: str (accumulated so far)
    - done: bool
    """
    async with _lock:
        state = _streams.get(session_id)
        if state is None:
            return None
        return {
            "message_id": state.message_id,
            "text": state.accumulated_text,
            "done": state.done,
        }
