"""Authentication middleware for Linchpin API.

Bearer token validation against the LINCHPIN_API_KEY environment variable.
Used as a FastAPI dependency on all /v1/ routes.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request


def _get_api_key() -> str:
    """Return the configured API key from the environment."""
    return os.environ.get("LINCHPIN_API_KEY", "")


async def verify_bearer_token(request: Request) -> None:
    """FastAPI dependency that validates the Authorization bearer token.

    Accepts either:
    - ``Authorization: Bearer <token>`` header (standard)
    - ``?token=<token>`` query parameter (for SSE/EventSource which can't send headers)

    Raises ``HTTPException(401)`` when the token is missing, malformed,
    or does not match ``LINCHPIN_API_KEY``.
    """
    api_key = _get_api_key()

    # Check query parameter first (for SSE/EventSource)
    query_token = request.query_params.get("token")
    if query_token:
        if not api_key or query_token != api_key:
            raise HTTPException(
                status_code=401,
                detail={"error": "unauthorized", "message": "Invalid API key"},
            )
        return

    # Fall back to Authorization header
    auth_header = request.headers.get("authorization", "")

    if not auth_header:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "Missing Authorization header"},
        )

    parts = auth_header.split(" ", 1)

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "Invalid Authorization header format, expected 'Bearer <token>'"},
        )

    token = parts[1]

    if not api_key or token != api_key:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "Invalid API key"},
        )
