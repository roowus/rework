"""Image attachments for chat messages: read a path, sniff the media type,
encode for the wire.

One helper shared by every adapter. Two rules matter:

- The file is read at REQUEST time, not when the message was built. Message
  dicts are checkpointed and replayed; a path costs bytes, a base64 blob costs
  bytes on every checkpoint write.
- An unreadable attachment raises rather than silently sending a text-only
  message. A model that quietly never saw the screenshot gives confident
  answers about an image it never received, which is worse than an error.
"""

from __future__ import annotations

import base64
from pathlib import Path

# Sniffed by magic bytes; the suffix lies often enough (screenshots saved as
# .png that are really JPEG) that trusting it produces API rejections.
_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # checked further below; RIFF also wraps wav/avi
]
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def media_type(path: Path) -> str:
    """The image's real type from its first bytes; raise if it is not one."""
    head = path.read_bytes()[:16]
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            if mime == "image/webp" and head[8:12] != b"WEBP":
                continue
            return mime
    raise ValueError(f"{path} is not a supported image (png/jpeg/gif/webp)")


def load_image(path_str: str) -> tuple[str, str]:
    """(base64 payload, media type) for one attachment path."""
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"image attachment not found: {path_str}")
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"{path.name} is {size // 1024} KiB; the limit is 4 MiB")
    return base64.b64encode(path.read_bytes()).decode("ascii"), media_type(path)
