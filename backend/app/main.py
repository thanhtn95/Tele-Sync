"""FastAPI app: REST API + background sync task (single process, single user)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import asynccontextmanager
from typing import Any

import secrets

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telethon import utils

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
    await db.migrate(pool)
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
  (SELECT count(*) FROM messages m WHERE m.chat_id = c.chat_id) AS message_count  -- PK index range scan
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
WHERE m.chat_id = $1 AND ($2::bigint IS NULL OR m.message_id < $2)
ORDER BY m.message_id DESC
LIMIT $3
"""


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
    limit: int = Query(50, ge=1, le=200),
    pool=Depends(pool_dep),
):
    """Newest first; pass the smallest id you have as ?before= to page backwards."""
    rows = await pool.fetch(MESSAGES_SQL, chat_id, before, limit + 1)
    has_more = len(rows) > limit
    return {"messages": [_message_out(r) for r in rows[:limit]], "has_more": has_more}


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
