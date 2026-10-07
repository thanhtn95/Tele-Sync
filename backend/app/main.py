"""FastAPI app: REST API + background sync task (single process, single user)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
import time
from contextlib import asynccontextmanager
from typing import Any

import secrets

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import events, utils
from telethon.errors import FloodWaitError, RPCError

from . import auth, db
from .config import settings
from .gphotos import GPhotos
from .service import service_text
from .sync import SyncWorker
from .tg import make_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("telesync")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await db.create_pool(settings.database_url)
    applied = await db.migrate(pool)
    if applied:
        await db.vacuum_after_migrations(pool)
    client = None
    if settings.tg_api_id and settings.tg_api_hash:
        client = make_client(settings)
    else:
        log.error("TG_API_ID / TG_API_HASH not set: Telegram sync disabled, viewer still works")
    gphotos = (
        GPhotos(settings.google_client_id, settings.google_client_secret, settings.google_refresh_token)
        if settings.gphotos_enabled
        else None
    )
    if gphotos is None:
        log.warning("Google Photos not configured: photos/videos will be stored on local disk")
    if client is not None:
        try:
            await client.connect()
            if not await client.is_user_authorized():
                log.error("Telegram session not authorised. Run: python -m scripts.tg_login")
        except Exception:
            log.exception("could not connect to Telegram")

    worker = SyncWorker(pool, client, gphotos, settings)
    task = asyncio.create_task(worker.run_forever(), name="sync-worker")
    if client is not None:
        _register_live_updates(client, worker)
    app.state.pool, app.state.client, app.state.gphotos, app.state.worker = pool, client, gphotos, worker
    app.state.dialogs_lock = asyncio.Lock()
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        if client is not None:
            await client.disconnect()
        if gphotos:
            await gphotos.aclose()
        await pool.close()


def _register_live_updates(client, worker: SyncWorker) -> None:
    """Save new/edited messages of synced chats as they happen (seconds, not 30 min)."""

    async def on_message(event):
        try:
            enabled = await worker.pool.fetchval(
                "SELECT sync_enabled FROM chats WHERE chat_id = $1", event.chat_id
            )
            if enabled:
                await worker.save_live(event.message, event.chat_id)
        except Exception:  # never let one bad update kill the handler
            log.exception("live update for chat %s failed", event.chat_id)

    client.add_event_handler(on_message, events.NewMessage())
    client.add_event_handler(on_message, events.MessageEdited())


app = FastAPI(title="Telegram Archiver", lifespan=lifespan)

# ---- auth -----------------------------------------------------------------------

# Without SESSION_SECRET (set by scripts/set_password) sessions only last until restart.
_session_secret = settings.session_secret or secrets.token_urlsafe(32)
_throttle = auth.LoginThrottle()
if not settings.auth_enabled:
    log.warning("WEB_PASSWORD_HASH not set: web login disabled (run scripts/set_password.py)")

PUBLIC_PATHS = ("/api/auth/", "/api/health")


def is_authenticated(request: Request) -> bool:
    if not settings.auth_enabled:
        return True
    return auth.check_token(
        request.cookies.get(auth.COOKIE_NAME), settings.web_username, _session_secret,
        settings.web_password_hash,
    )


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    protected = (path.startswith("/api/") and not path.startswith(PUBLIC_PATHS)) or path.startswith("/files/")
    if protected and not is_authenticated(request):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)


@app.post("/api/auth/login")
async def login(body: LoginRequest, request: Request, response: Response):
    if not settings.auth_enabled:
        return {"authenticated": True, "auth_enabled": False}
    wait = _throttle.retry_after()
    if wait:
        raise HTTPException(429, f"Too many attempts; try again in {wait}s", headers={"Retry-After": str(wait)})
    user_ok = secrets.compare_digest(body.username.encode(), settings.web_username.encode())
    pw_ok = await asyncio.to_thread(auth.verify_password, body.password, settings.web_password_hash)
    if not (user_ok and pw_ok):
        _throttle.failed()
        await asyncio.sleep(0.5)
        raise HTTPException(401, "Wrong username or password")
    _throttle.succeeded()
    token = auth.make_token(settings.web_username, _session_secret, settings.web_password_hash)
    response.set_cookie(
        auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )
    return {"authenticated": True, "auth_enabled": True, "username": settings.web_username}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"authenticated": False}


@app.get("/api/auth/me")
async def me(request: Request):
    ok = is_authenticated(request)
    return {
        "authenticated": ok,
        "auth_enabled": settings.auth_enabled,
        "username": settings.web_username if ok and settings.auth_enabled else None,
    }


@app.get("/api/auth/check", status_code=204)
async def auth_check(request: Request):
    """For nginx auth_request on /files/."""
    if not is_authenticated(request):
        raise HTTPException(401)
    return Response(status_code=204)


def pool_dep(request: Request):
    return request.app.state.pool


async def _require_tg(request: Request):
    client = request.app.state.client
    if client is None or not client.is_connected() or not await client.is_user_authorized():
        raise HTTPException(503, "Telegram not connected/authorised (run scripts/tg_login.py)")
    return client


# ---- dialogs / chats ------------------------------------------------------

CHAT_COLS = """
  c.chat_id, c.title, c.type, c.sync_enabled, c.sync_media, c.sync_since,
  c.last_msg_id, c.last_synced_at, (c.gphotos_album_id IS NOT NULL) AS has_album,
  c.message_count  -- kept by the sync worker; counting live got slow on big archives
"""


def _chat_out(r) -> dict:
    d = dict(r)
    d["chat_id"] = int(d["chat_id"])
    return d


def dialog_type(dialog) -> str:
    if dialog.is_user:
        return "user"
    if dialog.is_group:
        return "group"
    return "channel"


async def refresh_dialogs(request: Request) -> int:
    """Fetch all dialogs from Telegram and upsert them; sync flags are preserved."""
    client = await _require_tg(request)
    pool = request.app.state.pool
    async with request.app.state.dialogs_lock:
        rows = []
        async for d in client.iter_dialogs():
            title = d.name or utils.get_display_name(d.entity) or str(d.id)
            rows.append((d.id, title, dialog_type(d)))
        await pool.executemany(
            """
            INSERT INTO chats (chat_id, title, type) VALUES ($1, $2, $3)
            ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title, type = EXCLUDED.type
            """,
            rows,
        )
        return len(rows)


@app.get("/api/dialogs")
async def list_dialogs(request: Request, refresh: bool = False, pool=Depends(pool_dep)):
    """Chats cached in the DB. ?refresh=true re-reads them from Telegram (slow, FloodWait risk)."""
    empty = await pool.fetchval("SELECT NOT EXISTS (SELECT 1 FROM chats)")
    if refresh or empty:
        await refresh_dialogs(request)
    rows = await pool.fetch(f"SELECT {CHAT_COLS} FROM chats c ORDER BY c.sync_enabled DESC, c.title")
    return [_chat_out(r) for r in rows]


class ChatPatch(BaseModel):
    sync_enabled: bool | None = None
    sync_media: bool | None = None
    sync_since: dt.datetime | None = None


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: int, pool=Depends(pool_dep)):
    r = await pool.fetchrow(f"SELECT {CHAT_COLS} FROM chats c WHERE c.chat_id = $1", chat_id)
    if r is None:
        raise HTTPException(404, "chat not found")
    return _chat_out(r)


@app.patch("/api/chats/{chat_id}")
async def patch_chat(chat_id: int, body: ChatPatch, request: Request, pool=Depends(pool_dep)):
    fields = {k: getattr(body, k) for k in body.model_fields_set}
    for k in ("sync_enabled", "sync_media"):
        if k in fields and fields[k] is None:
            raise HTTPException(422, f"{k} cannot be null")
    if fields:
        cols = list(fields)
        sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(cols))
        res = await pool.execute(f"UPDATE chats SET {sets} WHERE chat_id = $1", chat_id, *fields.values())
        if res.endswith(" 0"):
            raise HTTPException(404, "chat not found")
        if fields.get("sync_enabled"):
            request.app.state.worker.trigger()  # start syncing the newly enabled chat now
    return await get_chat(chat_id, pool)


# ---- messages ---------------------------------------------------------------

MESSAGES_SQL = """
SELECT m.message_id, m.sender_id, m.date, m.text, m.reply_to, m.grouped_id, m.fwd_from_name,
       m.edit_date, m.out, m.pinned, m.raw->'entities' AS entities, m.raw->'action' AS action,
       m.raw->'media'->>'_' AS media_type,
       u.name AS sender_name, u.username AS sender_username, u.avatar_path AS sender_avatar,
       md.kind, md.mime, md.size, md.width, md.height, md.duration, md.thumb_b64,
       md.file_path, md.file_name, md.gphotos_media_id, md.error AS media_error,
       r.text AS reply_text, ru.name AS reply_sender_name, rmd.kind AS reply_media_kind,
       (r.message_id IS NOT NULL) AS reply_found
FROM messages m
LEFT JOIN users u ON u.user_id = m.sender_id
LEFT JOIN media md ON md.chat_id = m.chat_id AND md.message_id = m.message_id
LEFT JOIN messages r ON r.chat_id = m.chat_id AND r.message_id = m.reply_to
LEFT JOIN users ru ON ru.user_id = r.sender_id
LEFT JOIN media rmd ON rmd.chat_id = r.chat_id AND rmd.message_id = r.message_id
WHERE m.chat_id = $1 AND {where}
ORDER BY m.message_id {order}
LIMIT $3
"""


def _messages_sql(direction: str) -> str:
    """'older': id < $2 newest first; 'newer': id > $2 oldest first; 'upto': id <= $2."""
    where, order = {
        "older": ("($2::bigint IS NULL OR m.message_id < $2)", "DESC"),
        "upto": ("m.message_id <= $2", "DESC"),
        "newer": ("m.message_id > $2", "ASC"),
    }[direction]
    return MESSAGES_SQL.format(where=where, order=order)


def _message_out(r) -> dict[str, Any]:
    media = None
    if r["kind"]:
        media = {
            "kind": r["kind"], "mime": r["mime"], "size": r["size"],
            "width": r["width"], "height": r["height"], "duration": r["duration"],
            "thumb_b64": r["thumb_b64"], "file_path": r["file_path"], "file_name": r["file_name"],
            "gphotos_media_id": r["gphotos_media_id"], "error": r["media_error"],
        }
    reply = None
    if r["reply_to"]:
        snippet = (r["reply_text"] or "")[:200]
        reply = {
            "message_id": r["reply_to"],
            "found": r["reply_found"],
            "sender_name": r["reply_sender_name"],
            "text": snippet,
            "media_kind": r["reply_media_kind"],
        }
    action = r["action"]
    return {
        "id": r["message_id"],
        "sender_id": r["sender_id"],
        "sender_name": r["sender_name"],
        "sender_username": r["sender_username"],
        "sender_avatar": r["sender_avatar"],
        "date": r["date"],
        "edit_date": r["edit_date"],
        "text": r["text"],
        "entities": r["entities"] or [],
        # 64-bit random ids exceed JS Number precision -> send as string
        "grouped_id": str(r["grouped_id"]) if r["grouped_id"] is not None else None,
        "fwd_from_name": r["fwd_from_name"],
        "out": r["out"],
        "pinned": r["pinned"],
        "service": service_text(action, r["sender_name"]) if action else None,
        "media_type": r["media_type"],
        "media": media,
        "reply": reply,
    }


@app.get("/api/chats/{chat_id}/messages")
async def get_messages(
    chat_id: int,
    before: int | None = None,
    after: int | None = None,
    around: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    pool=Depends(pool_dep),
):
    """Always newest first.

    - default / ?before=<id>: older page (has_more = older messages exist)
    - ?after=<id>: the next newer page (has_newer = even newer ones exist)
    - ?around=<id>: a window centred on <id>, for jumping to a reply/pin anywhere in history
    """
    if around is not None:
        half = max(1, limit // 2)
        older = await pool.fetch(_messages_sql("upto"), chat_id, around, half + 1)
        newer = await pool.fetch(_messages_sql("newer"), chat_id, around, half + 1)
        rows = list(reversed(newer[:half])) + older[:half]
        return {"messages": [_message_out(r) for r in rows],
                "has_more": len(older) > half, "has_newer": len(newer) > half}
    if after is not None:
        rows = await pool.fetch(_messages_sql("newer"), chat_id, after, limit + 1)
        return {"messages": [_message_out(r) for r in reversed(rows[:limit])],
                "has_more": True, "has_newer": len(rows) > limit}
    rows = await pool.fetch(_messages_sql("older"), chat_id, before, limit + 1)
    has_more = len(rows) > limit
    return {"messages": [_message_out(r) for r in rows[:limit]], "has_more": has_more, "has_newer": False}


TOTAL_CAP = 1000


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.get("/api/chats/{chat_id}/search")
async def search_chat(
    chat_id: int,
    q: str = Query(..., min_length=1, max_length=200),
    before: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    pool=Depends(pool_dep),
):
    """Messages whose text, caption or file name contains q, ignoring case and accents.

    Newest first; ?before=<id> pages older. `total` is only computed on the first page.
    """
    q = q.strip()
    if not q:
        raise HTTPException(422, "empty query")
    pattern = "%" + _like_escape(q) + "%"
    # Matching ids from text and file names (a UNION keeps each side index-friendly;
    # an OR across the join would force a scan). search_text/search_name are folded
    # copies kept by Postgres; fold_text($2) folds the pattern the same way.
    matches = """
        SELECT message_id FROM messages
        WHERE chat_id = $1 AND search_text LIKE fold_text($2) ESCAPE '\\'
        UNION
        SELECT message_id FROM media
        WHERE chat_id = $1 AND search_name LIKE fold_text($2) ESCAPE '\\'
    """
    rows = await pool.fetch(
        f"""
        WITH hits AS ({matches})
        SELECT m.message_id, m.date, left(m.text, 1000) AS text, u.name AS sender_name,
               md.kind AS media_kind, md.file_name
        FROM hits
        JOIN messages m ON m.chat_id = $1 AND m.message_id = hits.message_id
        LEFT JOIN media md ON md.chat_id = m.chat_id AND md.message_id = m.message_id
        LEFT JOIN users u ON u.user_id = m.sender_id
        WHERE ($3::bigint IS NULL OR m.message_id < $3)
        ORDER BY m.message_id DESC
        LIMIT $4
        """,
        chat_id, pattern, before, limit + 1,
    )
    total = None
    if before is None:
        # Counting stops at TOTAL_CAP so a very common word stays fast ("1000+").
        total = await pool.fetchval(
            f"SELECT count(*) FROM (SELECT 1 FROM ({matches}) h LIMIT {TOTAL_CAP + 1}) c",
            chat_id, pattern,
        )
    return {
        "results": [
            {"id": r["message_id"], "date": r["date"], "text": r["text"], "sender_name": r["sender_name"],
             "media_kind": r["media_kind"], "file_name": r["file_name"]}
            for r in rows[:limit]
        ],
        "has_more": len(rows) > limit,
        "total": min(total, TOTAL_CAP) if total is not None else None,
        "total_capped": total is not None and total > TOTAL_CAP,
    }


MEDIA_GROUPS = {
    "media": ("photo", "video", "gif"),
    "files": ("document", "audio"),
    "voice": ("voice",),
}

MEDIA_SQL = """
SELECT md.message_id, m.date, m.text, m.grouped_id, u.name AS sender_name,
       md.kind, md.mime, md.size, md.width, md.height, md.duration, md.thumb_b64,
       md.file_path, md.file_name, md.gphotos_media_id, md.error AS media_error
FROM media md
JOIN messages m ON m.chat_id = md.chat_id AND m.message_id = md.message_id
LEFT JOIN users u ON u.user_id = m.sender_id
WHERE md.chat_id = $1 AND md.kind = ANY($2::text[]) AND {where}
ORDER BY md.message_id {order}
LIMIT $4
"""


@app.get("/api/chats/{chat_id}/media")
async def get_media(
    chat_id: int,
    group: str = Query("media", pattern="^(media|files|voice)$"),
    before: int | None = None,
    after: int | None = None,
    limit: int = Query(60, ge=1, le=200),
    pool=Depends(pool_dep),
):
    """Shared media of a chat (Telegram's Media / Files / Voice tabs), newest first.

    ?before=<id> pages older; ?after=<id> returns the next newer items (used by the
    viewer's next/previous buttons).
    """
    kinds = list(MEDIA_GROUPS[group])
    if after is not None:
        sql = MEDIA_SQL.format(where="md.message_id > $3", order="ASC")
        rows = list(reversed((await pool.fetch(sql, chat_id, kinds, after, limit + 1))[:limit]))
        has_more = None
    else:
        sql = MEDIA_SQL.format(where="($3::bigint IS NULL OR md.message_id < $3)", order="DESC")
        rows = await pool.fetch(sql, chat_id, kinds, before, limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
    items = [
        {
            "id": r["message_id"], "date": r["date"], "text": r["text"], "sender_name": r["sender_name"],
            "grouped_id": str(r["grouped_id"]) if r["grouped_id"] is not None else None,
            "media": {
                "kind": r["kind"], "mime": r["mime"], "size": r["size"], "width": r["width"],
                "height": r["height"], "duration": r["duration"], "thumb_b64": r["thumb_b64"],
                "file_path": r["file_path"], "file_name": r["file_name"],
                "gphotos_media_id": r["gphotos_media_id"], "error": r["media_error"],
            },
        }
        for r in rows
    ]
    counts = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE kind IN ('photo', 'video', 'gif')) AS media,
               count(*) FILTER (WHERE kind IN ('document', 'audio')) AS files,
               count(*) FILTER (WHERE kind = 'voice') AS voice
        FROM media WHERE chat_id = $1
        """,
        chat_id,
    ) if before is None and after is None else None
    return {"items": items, "has_more": has_more, "counts": dict(counts) if counts else None}


class SendRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4096)  # Telegram's message limit
    reply_to: int | None = None


@app.post("/api/chats/{chat_id}/send")
async def send_message(chat_id: int, body: SendRequest, request: Request, pool=Depends(pool_dep)):
    """Send a text message to the chat as you (your Telegram account), then store it."""
    client = await _require_tg(request)
    worker: SyncWorker = request.app.state.worker
    if not await pool.fetchval("SELECT EXISTS (SELECT 1 FROM chats WHERE chat_id = $1)", chat_id):
        raise HTTPException(404, "chat not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "empty message")
    try:
        entity = await worker._resolve(chat_id)
        # parse_mode=None: send exactly what was typed (no markdown surprises with * or _)
        msg = await client.send_message(entity, text, reply_to=body.reply_to, parse_mode=None)
    except FloodWaitError as e:
        raise HTTPException(429, f"Telegram rate limit: try again in {e.seconds}s")
    except RPCError as e:
        # e.g. CHAT_WRITE_FORBIDDEN in a channel where you can't post
        raise HTTPException(400, f"Telegram refused the message: {e.message or type(e).__name__}")
    await worker.save_live(msg, chat_id)
    return {"id": msg.id}


@app.get("/api/chats/{chat_id}/pinned")
async def get_pinned(chat_id: int, pool=Depends(pool_dep)):
    """Currently pinned messages, newest first (Telegram's pinned bar order)."""
    rows = await pool.fetch(
        """
        SELECT m.message_id, m.date, left(m.text, 300) AS text, m.raw->'media'->>'_' AS media_type,
               u.name AS sender_name, md.kind, md.thumb_b64
        FROM messages m
        LEFT JOIN users u ON u.user_id = m.sender_id
        LEFT JOIN media md ON md.chat_id = m.chat_id AND md.message_id = m.message_id
        WHERE m.chat_id = $1 AND m.pinned
        ORDER BY m.message_id DESC
        """,
        chat_id,
    )
    return [
        {"id": r["message_id"], "date": r["date"], "text": r["text"], "sender_name": r["sender_name"],
         "media_kind": r["kind"], "media_type": r["media_type"], "thumb_b64": r["thumb_b64"]}
        for r in rows
    ]


# ---- google photos ------------------------------------------------------------

class UrlsRequest(BaseModel):
    media_ids: list[str] = Field(default_factory=list, max_length=500)
    force: bool = False  # bypass the short server cache (client saw an expired URL)


@app.post("/api/gphotos/urls")
async def gphotos_urls(body: UrlsRequest, request: Request):
    gp: GPhotos | None = request.app.state.gphotos
    if gp is None:
        raise HTTPException(503, "Google Photos not configured")
    return {"urls": await gp.base_urls(body.media_ids, force=body.force)}


# ---- sync -----------------------------------------------------------------

_media_stats_cache: dict = {"at": 0.0, "value": None}
MEDIA_STATS_TTL = 60  # the status box polls every few seconds; counting can wait a minute


async def media_stats(pool) -> dict:
    """Where media files are: Google Photos, VM disk, failed, or not downloaded (yet)."""
    now = time.monotonic()
    if _media_stats_cache["value"] is not None and now - _media_stats_cache["at"] < MEDIA_STATS_TTL:
        return _media_stats_cache["value"]
    counts = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE gphotos_media_id IS NOT NULL) AS in_gphotos,
               count(*) FILTER (WHERE file_path IS NOT NULL) AS on_disk,
               count(*) FILTER (WHERE gphotos_media_id IS NULL AND file_path IS NULL
                                AND error IS NOT NULL AND error NOT LIKE 'skipped:%') AS failed,
               count(*) FILTER (WHERE error LIKE 'skipped:%') AS skipped,
               count(*) FILTER (WHERE gphotos_media_id IS NULL AND file_path IS NULL
                                AND error IS NULL) AS not_downloaded
        FROM media
        """
    )
    reasons = await pool.fetch(
        """
        SELECT left(regexp_replace(error, '\\s+', ' ', 'g'), 140) AS reason, count(*) AS n
        FROM media
        WHERE gphotos_media_id IS NULL AND file_path IS NULL
          AND error IS NOT NULL AND error NOT LIKE 'skipped:%'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 3
        """
    )
    value = {**dict(counts), "top_errors": [dict(r) for r in reasons]}
    _media_stats_cache.update(at=now, value=value)
    return value


def disk_stats() -> dict | None:
    try:
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        u = shutil.disk_usage(settings.media_dir)
    except OSError:
        return None
    # Same "Use%" as df: space reserved for root counts as neither used nor free.
    usable = (u.used + u.free) or 1
    return {"total": u.total, "used": u.used, "free": u.free, "percent": round(u.used * 100 / usable, 1)}


@app.get("/api/sync/status")
async def sync_status(request: Request, pool=Depends(pool_dep)):
    worker: SyncWorker = request.app.state.worker
    client = request.app.state.client
    st = worker.state
    rows = await pool.fetch(
        f"SELECT {CHAT_COLS} FROM chats c WHERE c.sync_enabled ORDER BY c.title"
    )
    try:
        authorised = client is not None and client.is_connected() and await client.is_user_authorized()
    except Exception:
        authorised = False
    return {
        "running": st.running,
        "current_chat_id": st.current_chat_id,
        "current_chat_title": st.current_chat_title,
        "progress": st.progress,
        "last_pass_started_at": st.last_pass_started_at,
        "last_pass_finished_at": st.last_pass_finished_at,
        "next_pass_at": st.next_pass_at,
        "flood_wait_until": st.flood_wait_until,
        "last_error": st.last_error,
        "telegram_configured": client is not None,
        "telegram_authorised": authorised,
        "gphotos_enabled": request.app.state.gphotos is not None,
        "chats": [{**_chat_out(r), "error": st.chat_errors.get(r["chat_id"])} for r in rows],
        "media": await media_stats(pool),
        "disk": disk_stats(),
        "disk_low": st.disk_low,
    }


@app.post("/api/sync/run", status_code=202)
async def sync_run(request: Request):
    worker: SyncWorker = request.app.state.worker
    worker.trigger()
    return {"queued": True, "running": worker.state.running}


@app.get("/api/health")
async def health(pool=Depends(pool_dep)):
    await pool.fetchval("SELECT 1")
    return {"ok": True}


# In production nginx serves /files/ directly; this mount is for local development.
settings.media_dir.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=settings.media_dir), name="files")
