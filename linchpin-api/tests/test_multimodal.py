"""Tests for v0.5.0 PR1 — multimodal content blocks.

Covers:
- ``ImageBlock`` / ``DocumentBlock`` / ``TextBlock`` discriminator parsing.
- ``validate_user_message_payload`` accepts strings + valid block lists,
  rejects unknown block types + malformed sources.
- ``EventPayload`` runs the validator for ``user.message`` events only.
- ``_block_to_openai`` translation matrix (text, image base64/url/file,
  document pass-through).
- ``_convert_messages_for_openai`` walks the messages list and rewrites
  structured content; leaves string content untouched.
- Orchestrator ``_resolve_file_sources`` reads bytes through the
  FileStore and inlines as base64 with the file's content_type.
"""

from __future__ import annotations

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import (
    DocumentBlock,
    EventPayload,
    ImageBlock,
    TextBlock,
    validate_user_message_payload,
)
from app.providers import _block_to_openai, _convert_messages_for_openai


# ---------------------------------------------------------------------------
# Block parsing
# ---------------------------------------------------------------------------


class TestBlockParsing:
    def test_text_block(self):
        block = TextBlock.model_validate({"type": "text", "text": "hi"})
        assert block.text == "hi"

    def test_image_block_base64(self):
        block = ImageBlock.model_validate({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "Zm9v"},
        })
        assert block.source.media_type == "image/png"

    def test_image_block_url(self):
        block = ImageBlock.model_validate({
            "type": "image",
            "source": {"type": "url", "url": "https://example.com/a.png"},
        })
        assert block.source.url == "https://example.com/a.png"

    def test_image_block_file(self):
        block = ImageBlock.model_validate({
            "type": "image",
            "source": {"type": "file", "file_id": "fi_123"},
        })
        assert block.source.file_id == "fi_123"

    def test_image_block_rejects_bad_media_type(self):
        with pytest.raises(Exception, match="image media_type"):
            ImageBlock.model_validate({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/bmp", "data": "x"},
            })

    def test_document_block_pdf(self):
        block = DocumentBlock.model_validate({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "x"},
        })
        assert block.source.media_type == "application/pdf"

    def test_document_block_rejects_bad_media_type(self):
        with pytest.raises(Exception, match="document media_type"):
            DocumentBlock.model_validate({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/zip", "data": "x"},
            })


# ---------------------------------------------------------------------------
# validate_user_message_payload
# ---------------------------------------------------------------------------


class TestValidateUserMessagePayload:
    def test_string_content_passes_through(self):
        payload = {"content": "plain text"}
        assert validate_user_message_payload(payload) == payload

    def test_missing_content_passes_through(self):
        payload = {}
        assert validate_user_message_payload(payload) == payload

    def test_text_block_list_accepted(self):
        payload = {"content": [{"type": "text", "text": "hello"}]}
        validate_user_message_payload(payload)  # does not raise

    def test_mixed_blocks_accepted(self):
        payload = {"content": [
            {"type": "text", "text": "What's in this?"},
            {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
        ]}
        validate_user_message_payload(payload)

    def test_unknown_block_type_rejected(self):
        with pytest.raises(ValueError, match="content\\[0\\]"):
            validate_user_message_payload({
                "content": [{"type": "audio", "data": "x"}],
            })

    def test_non_list_non_string_rejected(self):
        with pytest.raises(ValueError, match="string or a list"):
            validate_user_message_payload({"content": 42})


# ---------------------------------------------------------------------------
# EventPayload integration
# ---------------------------------------------------------------------------


class TestEventPayloadValidation:
    def test_user_message_with_blocks_passes(self):
        ev = EventPayload.model_validate({
            "type": "user.message",
            "payload": {"content": [{"type": "text", "text": "hi"}]},
        })
        assert ev.type == "user.message"

    def test_user_message_with_bad_block_rejected(self):
        with pytest.raises(Exception, match="content\\[0\\]"):
            EventPayload.model_validate({
                "type": "user.message",
                "payload": {"content": [{"type": "video"}]},
            })

    def test_other_event_types_skip_block_validation(self):
        # agent.message can carry arbitrary content; we don't validate it
        # as a UserMessageBlock list.
        EventPayload.model_validate({
            "type": "agent.message",
            "payload": {"content": [{"type": "video"}]},
        })


# ---------------------------------------------------------------------------
# Provider translation
# ---------------------------------------------------------------------------


class TestBlockToOpenAI:
    def test_text(self):
        assert _block_to_openai({"type": "text", "text": "hi"}) == {
            "type": "text", "text": "hi",
        }

    def test_image_url(self):
        out = _block_to_openai({
            "type": "image",
            "source": {"type": "url", "url": "https://x/y.png"},
        })
        assert out == {"type": "image_url", "image_url": {"url": "https://x/y.png"}}

    def test_image_base64(self):
        out = _block_to_openai({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"},
        })
        assert out == {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,abc"},
        }

    def test_document_passes_through(self):
        block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "x"},
        }
        assert _block_to_openai(block) == block

    def test_unknown_dropped(self):
        assert _block_to_openai({"type": "unknown"}) is None


def test_convert_messages_walks_list_content():
    msgs = [
        {"role": "user", "content": "string content unchanged"},
        {"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
        ]},
    ]
    out = _convert_messages_for_openai(msgs)
    # First message untouched (string content)
    assert out[0]["content"] == "string content unchanged"
    # Second message rewritten with OpenAI image_url shape
    assert out[1]["content"] == [
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]


def test_convert_messages_drops_unknown_block_kinds():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "ok"},
        {"type": "bogus"},
    ]}]
    out = _convert_messages_for_openai(msgs)
    assert out[0]["content"] == [{"type": "text", "text": "ok"}]


# ---------------------------------------------------------------------------
# _resolve_file_sources (orchestrator)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_file_source_inlines_base64(tmp_path):
    """A file-source block becomes a base64 block with the file's
    actual bytes + content_type."""
    from app.orchestrator import _resolve_file_sources

    # Write some "file" bytes under a fake FileStore root.
    img_bytes = b"\x89PNG\r\n\x1a\nfake png bytes"
    storage_path = "ab/cd/abcdef"
    abs_path = tmp_path / storage_path
    abs_path.parent.mkdir(parents=True)
    abs_path.write_bytes(img_bytes)

    file_id = str(uuid.uuid4())
    fake_store = MagicMock()
    fake_store.absolute_path = lambda sp: str(tmp_path / sp)

    block = {
        "type": "image",
        "source": {"type": "file", "file_id": file_id},
    }
    with (
        patch("app.orchestrator.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.files.get_file_store", return_value=fake_store),
    ):
        fetch_one.return_value = {
            "content_type": "image/png",
            "storage_path": storage_path,
        }
        out = await _resolve_file_sources([block])

    assert out[0]["source"]["type"] == "base64"
    assert out[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(out[0]["source"]["data"]) == img_bytes


@pytest.mark.asyncio
async def test_resolve_file_source_missing_left_as_is():
    """File-source with a missing/archived file_id is left untouched
    so the downstream provider can surface its own error."""
    from app.orchestrator import _resolve_file_sources

    file_id = str(uuid.uuid4())
    block = {
        "type": "image",
        "source": {"type": "file", "file_id": file_id},
    }
    with patch("app.orchestrator.fetch_one", new_callable=AsyncMock) as fetch_one:
        fetch_one.return_value = None
        out = await _resolve_file_sources([block])
    assert out[0] == block


@pytest.mark.asyncio
async def test_resolve_file_source_non_file_blocks_untouched():
    """A url/base64/text block is returned as-is."""
    from app.orchestrator import _resolve_file_sources

    blocks = [
        {"type": "text", "text": "hi"},
        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
    ]
    out = await _resolve_file_sources(blocks)
    assert out == blocks
