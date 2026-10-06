"""Shared Telethon client (MTProto user API)."""
from __future__ import annotations

import base64
import datetime as dt
import logging
from typing import Any

from telethon import TelegramClient

from .config import Settings

log = logging.getLogger(__name__)


def make_client(s: Settings) -> TelegramClient:
    s.tg_session_path.parent.mkdir(parents=True, exist_ok=True)
    # Telethon appends ".session" itself.
    session = str(s.tg_session_path)
    if session.endswith(".session"):
        session = session[: -len(".session")]
    return TelegramClient(session, s.tg_api_id, s.tg_api_hash, flood_sleep_threshold=60)


def jsonable(obj: Any) -> Any:
    """Make msg.to_dict() output JSON-serialisable (datetimes, bytes)."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode()
    if isinstance(obj, str):
        # Postgres JSONB rejects NUL characters.
        return obj.replace("\x00", "")
    return obj
