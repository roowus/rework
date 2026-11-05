"""Image attachments: the adapters must put real image blocks on the wire, and
a model without vision must refuse rather than silently send text only."""

from __future__ import annotations

import base64
import struct
import zlib

import pytest

from rework.kernel.llm.types import ChatRequest


def _png(path):
    """A real 1x1 PNG, so magic-byte sniffing has something honest to read."""

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = zlib.compress(b"\x00\xff\x00\x00")
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
    )
    return path


def test_media_type_sniffs_real_bytes(tmp_path):
    from rework.kernel.llm.adapters.images import media_type

    assert media_type(_png(tmp_path / "shot.png")) == "image/png"
    jpeg = tmp_path / "photo.png"  # a lying suffix: content is what matters
    jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    assert media_type(jpeg) == "image/jpeg"


def test_media_type_rejects_non_images(tmp_path):
    from rework.kernel.llm.adapters.images import media_type

    text = tmp_path / "notes.txt"
    text.write_text("this is not an image")
    with pytest.raises(ValueError, match="not a supported image"):
        media_type(text)


def test_load_image_encodes_and_bounds_size(tmp_path):
    from rework.kernel.llm.adapters.images import load_image

    png = _png(tmp_path / "ok.png")
    data, mime = load_image(str(png))
    assert mime == "image/png"
    assert base64.b64decode(data) == png.read_bytes()

    with pytest.raises(FileNotFoundError):
        load_image(str(tmp_path / "gone.png"))


def test_anthropic_translate_emits_image_block(tmp_path):
    from rework.kernel.llm.adapters.anthropic import translate_request

    png = _png(tmp_path / "s.png")
    req = ChatRequest(
        model="m",
        messages=[{"role": "user", "content": "what is wrong here?", "images": [str(png)]}],
    )
    payload = translate_request(req)
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[0]["source"]["data"]
    # the words still ride along, after the image
    assert content[1] == {"type": "text", "text": "what is wrong here?"}


def test_openai_compat_translate_uses_data_uri(tmp_path):
    from rework.kernel.llm.adapters.openai_compat import translate_request

    png = _png(tmp_path / "s.png")
    req = ChatRequest(
        model="m", messages=[{"role": "user", "content": "look", "images": [str(png)]}]
    )
    payload = translate_request(req)
    parts = payload["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_only_messages_keep_the_old_plain_shape(tmp_path):
    """The image path must not change anything for ordinary messages: every
    checkpointed history from before this feature replays through here."""
    from rework.kernel.llm.adapters.anthropic import translate_request as anth
    from rework.kernel.llm.adapters.openai_compat import translate_request as oai

    req = ChatRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    assert anth(req)["messages"][0] == {"role": "user", "content": "hi"}
    assert oai(req)["messages"][0] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_chat_refuses_images_without_vision_capability():
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
    from rework.orchestrator.chat import OrchestratorChat

    catalog = ModelCatalog([ModelEntry(id="blind", provider="fake", model="f", capabilities=set())])
    chat = OrchestratorChat(_NoProvider(), catalog, "blind")
    assert chat.supports_images is False
    with pytest.raises(ValueError, match="no vision capability"):
        await chat.send("look at this", images=["/tmp/x.png"])


@pytest.mark.asyncio
async def test_chat_accepts_images_with_vision_capability(tmp_path):
    from rework.kernel.llm.catalog import ModelCatalog, ModelEntry
    from rework.kernel.llm.types import ChatResult
    from rework.orchestrator.chat import OrchestratorChat

    png = _png(tmp_path / "s.png")
    provider = _Capture(ChatResult(text="I see the bug."))
    catalog = ModelCatalog(
        [ModelEntry(id="eye", provider="fake", model="f", capabilities={"vision"})]
    )
    chat = OrchestratorChat(provider, catalog, "eye")
    assert chat.supports_images is True
    await chat.send("look", images=[str(png)])
    sent = provider.requests[0].messages[-1]
    assert sent["images"] == [str(png)]
    assert sent["content"] == "look"


class _NoProvider:
    async def complete(self, request):  # pragma: no cover - must not be called
        raise AssertionError("a vision-less model must not be asked")


class _Capture:
    def __init__(self, result):
        self._result = result
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self._result
