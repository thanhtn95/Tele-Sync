"""Classify Telegram media and pull out the metadata the viewer needs."""
from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass

from telethon.tl import types

# Kinds that go to Google Photos (when configured and within size limits).
GPHOTOS_KINDS = {"photo", "video", "gif"}

_STICKER_EXT = {"image/webp": ".webp", "video/webm": ".webm", "application/x-tgsticker": ".tgs"}


@dataclass
class MediaInfo:
    kind: str  # photo / video / gif / voice / audio / sticker / document
    mime: str | None
    size: int | None
    width: int | None
    height: int | None
    duration: int | None
    thumb_b64: str | None
    file_name: str | None
    ext: str


def _stripped_thumb(sizes) -> str | None:
    for s in sizes or ():
        if isinstance(s, types.PhotoStrippedSize):
            return base64.b64encode(s.bytes).decode()
    return None


def _kind(msg) -> str:
    if msg.sticker:
        return "sticker"
    if msg.voice:
        return "voice"
    if msg.gif:
        return "gif"
    if msg.video or msg.video_note:
        return "video"
    if msg.audio:
        return "audio"
    if msg.photo:
        return "photo"
    return "document"


def classify(msg) -> MediaInfo | None:
    """Return MediaInfo for downloadable photo/document media, else None.

    Web page previews, polls, locations, contacts... are left to raw JSON.
    """
    media = msg.media
    if isinstance(media, types.MessageMediaPhoto):
        if not isinstance(media.photo, types.Photo):  # expired self-destructing photo
            return None
        thumbs = media.photo.sizes
    elif isinstance(media, types.MessageMediaDocument):
        if not isinstance(media.document, types.Document):
            return None
        thumbs = media.document.thumbs
    else:
        return None

    f = msg.file
    kind = _kind(msg)
    mime = f.mime_type
    if kind == "sticker":
        ext = _STICKER_EXT.get(mime or "", ".webp")
    else:
        ext = f.ext or mimetypes.guess_extension(mime or "") or ""
    if kind == "voice" and ext in ("", ".oga"):
        ext = ".ogg"
    duration = f.duration
    return MediaInfo(
        kind=kind,
        mime=mime,
        size=f.size,
        width=f.width or None,
        height=f.height or None,
        duration=int(round(duration)) if duration else None,
        thumb_b64=_stripped_thumb(thumbs),
        file_name=f.name,
        ext=ext,
    )


_UNSAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)


def local_rel_path(chat_id: int, message_id: int, info: MediaInfo) -> str:
    """Path relative to MEDIA_DIR; served by nginx under /files/."""
    if info.file_name:
        name = _UNSAFE.sub("_", info.file_name).strip(" .")[:120] or "file"
        if not name.lower().endswith(info.ext.lower()):
            name += info.ext
        return f"{chat_id}/{message_id}_{name}"
    return f"{chat_id}/{message_id}{info.ext}"
