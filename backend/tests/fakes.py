"""Fake Telegram client / Google Photos built around real Telethon TL objects."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.patched import Message

from app.gphotos import CreateResult

CHAT_ID = -1001234567890  # marked supergroup id
CHANNEL_RAW = 1234567890
T0 = dt.datetime(2024, 5, 1, 12, 0, tzinfo=dt.timezone.utc)

ALICE = types.User(id=111, first_name="Alice", last_name="A", username="alice", photo=None)
BOB = types.User(id=222, first_name="Bob", username=None,
                 photo=types.UserProfilePhoto(photo_id=9, dc_id=2, stripped_thumb=None))
CHANNEL = types.Channel(id=CHANNEL_RAW, title="Test Group", photo=types.ChatPhotoEmpty(),
                        date=T0, megagroup=True, access_hash=42)
ENTITIES = {111: ALICE, 222: BOB, CHAT_ID: CHANNEL}

STRIPPED = bytes([1, 40, 30]) + b"\x00" * 10


def _photo(pid: int) -> types.MessageMediaPhoto:
    return types.MessageMediaPhoto(photo=types.Photo(
        id=pid, access_hash=1, file_reference=b"", date=T0, dc_id=2,
        sizes=[types.PhotoStrippedSize(type="i", bytes=STRIPPED),
               types.PhotoSize(type="x", w=1280, h=960, size=150000)],
    ))


def _doc(did: int, mime: str, attrs, size=5000) -> types.MessageMediaDocument:
    return types.MessageMediaDocument(document=types.Document(
        id=did, access_hash=1, file_reference=b"", date=T0, mime_type=mime, size=size,
        dc_id=2, attributes=attrs, thumbs=None,
    ))


def make_msg(client, mid: int, sender: int | None, text: str = "", **kw) -> Message:
    m = Message(
        id=mid, peer_id=types.PeerChannel(CHANNEL_RAW), date=T0 + dt.timedelta(minutes=mid),
        message=text, out=kw.pop("out", False),
        from_id=types.PeerUser(sender) if sender else None, **kw,
    )
    m._finish_init(client, ENTITIES, None)
    return m


def make_service(client, mid: int, sender: int, action) -> types.MessageService:
    from telethon.tl.patched import MessageService
    m = MessageService(id=mid, peer_id=types.PeerChannel(CHANNEL_RAW), date=T0 + dt.timedelta(minutes=mid),
                       action=action, from_id=types.PeerUser(sender))
    m._finish_init(client, ENTITIES, None)
    return m


class FakeTG(TelegramClient):
    def __init__(self):
        super().__init__(StringSession(), 1, "x")
        self.messages: list = []
        self.flood_after: int | None = None  # raise FloodWait once after yielding N messages
        self.downloads: list[str] = []
        self.dialogs: list = []

    async def is_user_authorized(self):
        return True

    def is_connected(self):
        return True

    async def get_input_entity(self, peer):
        return types.InputPeerChannel(CHANNEL_RAW, 42)

    async def iter_messages(self, entity, min_id=0, reverse=False, offset_date=None, filter=None, **kw):
        if filter is types.InputMessagesFilterPinned or isinstance(filter, types.InputMessagesFilterPinned):
            for m in sorted(self.messages, key=lambda m: -m.id):  # newest first, like Telegram
                if getattr(m, "pinned", False):
                    yield m
            return
        assert reverse
        n = 0
        for m in sorted(self.messages, key=lambda m: m.id):
            if m.id <= min_id or (offset_date and m.date <= offset_date):
                continue
            if self.flood_after is not None and n == self.flood_after:
                self.flood_after = None
                raise FloodWaitError(request=None, capture=0)
            n += 1
            yield m

    async def get_messages(self, entity, ids):
        by_id = {m.id: m for m in self.messages}
        return [by_id.get(i) for i in ids]

    async def download_media(self, msg, file=None, **kw):
        Path(file).write_bytes(b"data-%d" % msg.id)
        self.downloads.append(file)
        return file

    async def download_profile_photo(self, entity, file=None, download_big=False):
        if not getattr(entity, "photo", None) or isinstance(entity.photo, types.UserProfilePhotoEmpty):
            return None
        Path(file).write_bytes(b"avatar")
        return file

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d


class FakeGPhotos:
    def __init__(self):
        self.uploads: list[tuple[str, str]] = []
        self.created: list[tuple[list, str | None]] = []
        self.albums: list[str] = []
        self.fail_tokens: set[str] = set()

    async def upload(self, path: Path, mime: str) -> str:
        assert path.exists()
        tok = f"tok-{len(self.uploads)}"
        self.uploads.append((path.name, mime))
        return tok

    async def batch_create(self, items, album_id=None):
        self.created.append((items, album_id))
        return [
            CreateResult(t, None, "boom") if t in self.fail_tokens else CreateResult(t, f"gp-{t}", None)
            for t, _ in items
        ]

    async def create_album(self, title):
        self.albums.append(title)
        return f"album-{len(self.albums)}"

    async def base_urls(self, ids, force=False):
        return {i: f"https://lh3.example/{i}" for i in ids}
