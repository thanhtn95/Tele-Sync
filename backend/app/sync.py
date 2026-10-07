"""Background sync worker: one asyncio task inside the FastAPI process."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import InputMessagesFilterPinned

from .config import Settings
from .gphotos import PHOTO_MAX_BYTES, VIDEO_MAX_BYTES, GPhotos
from .media import GPHOTOS_KINDS, MediaInfo, classify, local_rel_path
from .tg import jsonable

log = logging.getLogger(__name__)

# Persist the cursor (and flush pending Google Photos items) every N messages.
CHECKPOINT_EVERY = 50
# Stop downloading files when the disk would drop below this much free space: a full
# disk would also stop Postgres. Such files are retried once space is freed.
DISK_RESERVE_BYTES = 1536 * 1024 * 1024

# Media rows retried/backfilled per chat per pass.
RETRY_LIMIT = 100


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fwd_name(msg) -> str | None:
    fwd = msg.forward
    if fwd is None:
        return None
    try:
        if fwd.from_name:
            return fwd.from_name
        ent = fwd.sender or fwd.chat
        if ent is not None:
            return utils.get_display_name(ent) or None
    except Exception:  # entity not in response; fall through
        pass
    return "Unknown"


@dataclass
class _Pending:
    message_id: int
    upload_token: str
    description: str


@dataclass
class SyncState:
    running: bool = False
    current_chat_id: int | None = None
    current_chat_title: str | None = None
    progress: int = 0  # messages processed in current chat this pass
    last_pass_started_at: dt.datetime | None = None
    last_pass_finished_at: dt.datetime | None = None
    next_pass_at: dt.datetime | None = None
    flood_wait_until: dt.datetime | None = None
    last_error: str | None = None
    disk_low: bool = False  # downloads paused because the disk is almost full
    retrying_failed: bool = False  # "Retry failed" asked for or running
    media_epoch: int = 0  # bumped after a retry, so the status recounts media right away
    chat_errors: dict[int, str] = field(default_factory=dict)


class SyncWorker:
    def __init__(
        self,
        pool: asyncpg.Pool,
        client: TelegramClient | None,
        gphotos: GPhotos | None,
        settings: Settings,
    ):
        self.pool = pool
        self.client = client
        self.gphotos = gphotos
        self.s = settings
        self.state = SyncState()
        self._wake = asyncio.Event()
        self._full = False  # a full pass was asked for ("Sync now")
        self._requested: list[int] = []  # single chats asked for, in order
        self._retry_failed = False  # "Retry failed": failed media in every chat
        self._known_users: set[int] = set()
        self._lock = asyncio.Lock()

    # ---- scheduling -----------------------------------------------------

    def trigger(self) -> None:
        """Run a full pass now."""
        self._full = True
        self._wake.set()

    def request_chat(self, chat_id: int) -> None:
        """Sync one chat soon: next in a running pass, otherwise right away (no full pass)."""
        if chat_id not in self._requested:
            self._requested.append(chat_id)
        self._wake.set()

    def request_failed_retry(self) -> None:
        """Retry failed media in every chat soon, whatever its sync settings."""
        self._retry_failed = True
        self.state.retrying_failed = True
        self._wake.set()

    @property
    def queued_chat_ids(self) -> list[int]:
        return list(self._requested)

    async def run_forever(self) -> None:
        interval = self.s.sync_interval_seconds
        loop = asyncio.get_running_loop()
        next_full = loop.time()  # first full pass right away
        while True:
            full = self._full or loop.time() >= next_full
            try:
                if full:
                    self._full = False
                    await self.run_pass()
                else:
                    await self.run_pass(only_requested=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the loop die
                log.exception("sync pass crashed")
                self.state.last_error = f"{type(e).__name__}: {e}"
            if full:
                next_full = loop.time() + interval if interval > 0 else float("inf")
                self.state.next_pass_at = _now() + dt.timedelta(seconds=interval) if interval > 0 else None
            self._wake.clear()
            if self._full or self._requested or self._retry_failed:  # asked for while the pass was finishing
                continue
            timeout = None if next_full == float("inf") else max(0.0, next_full - loop.time())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    async def run_pass(self, only_requested: bool = False) -> None:
        """Sync every enabled chat (single-chat requests jump the queue), or only the requested ones."""
        if self._lock.locked():
            return
        async with self._lock:
            if self.client is None:
                self.state.last_error = "Telegram not configured (TG_API_ID / TG_API_HASH)"
                return
            if not self.client.is_connected():
                try:
                    await self.client.connect()
                except Exception as e:
                    self.state.last_error = f"Cannot connect to Telegram: {e}"
                    return
            if not await self.client.is_user_authorized():
                self.state.last_error = "Telegram session not authorised; run scripts/tg_login.py"
                return
            st = self.state
            st.running = True
            st.last_error = None
            if not only_requested:
                st.last_pass_started_at = _now()
                st.next_pass_at = None
            try:
                queue = [] if only_requested else [
                    r["chat_id"]
                    for r in await self.pool.fetch(
                        "SELECT chat_id FROM chats WHERE sync_enabled ORDER BY last_synced_at NULLS FIRST, chat_id"
                    )
                ]
                done: set[int] = set()
                while self._requested or queue or self._retry_failed:
                    if self._retry_failed:
                        await self._retry_all_failed()
                        continue
                    chat_id = self._requested.pop(0) if self._requested else queue.pop(0)
                    if chat_id in done:
                        continue
                    done.add(chat_id)
                    # Fresh row: settings or the cursor may have changed since the pass started.
                    chat = await self.pool.fetchrow("SELECT * FROM chats WHERE chat_id = $1 AND sync_enabled", chat_id)
                    if chat is None:
                        continue
                    st.current_chat_id = chat["chat_id"]
                    st.current_chat_title = chat["title"]
                    st.progress = 0
                    try:
                        await self.sync_chat(chat)
                        st.chat_errors.pop(chat["chat_id"], None)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception("sync of chat %s failed", chat["chat_id"])
                        st.chat_errors[chat["chat_id"]] = f"{type(e).__name__}: {e}"
            finally:
                st.running = False
                st.current_chat_id = None
                st.current_chat_title = None
                if not only_requested:
                    st.last_pass_finished_at = _now()

    # ---- per chat -------------------------------------------------------

    async def _resolve(self, chat_id: int):
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            # Not in the session's entity cache yet: a dialogs fetch fills it.
            await self.client.get_dialogs()
            return await self.client.get_input_entity(chat_id)

    async def sync_chat(self, chat: asyncpg.Record) -> None:
        chat_id = chat["chat_id"]
        entity = await self._resolve(chat_id)
        ctx = _ChatCtx(chat)

        if chat["sync_media"]:
            await self._retry_media(entity, ctx)

        cursor = chat["last_msg_id"] or 0
        while True:
            kwargs: dict = {"min_id": cursor, "reverse": True}
            if chat["sync_since"] is not None:
                kwargs["offset_date"] = chat["sync_since"]
            try:
                async for msg in self.client.iter_messages(entity, **kwargs):
                    await self.process_message(msg, ctx)
                    cursor = max(cursor, msg.id)
                    ctx.seen_since_checkpoint += 1
                    self.state.progress += 1
                    if ctx.seen_since_checkpoint >= CHECKPOINT_EVERY:
                        if not await self._checkpoint(ctx, cursor):
                            log.info("chat %s disabled mid-sync; stopping", chat_id)
                            return
                break
            except FloodWaitError as e:
                await self._checkpoint(ctx, cursor)
                log.warning("FloodWait %ss on chat %s", e.seconds, chat_id)
                self.state.flood_wait_until = _now() + dt.timedelta(seconds=e.seconds)
                await asyncio.sleep(e.seconds + 1)
                self.state.flood_wait_until = None
        await self._flood_retry(lambda: self._sync_pins(entity, ctx), chat_id)
        await self._checkpoint(ctx, cursor, finished=True)

    async def _flood_retry(self, fn, chat_id: int, attempts: int = 3):
        for attempt in range(attempts):
            try:
                return await fn()
            except FloodWaitError as e:
                if attempt == attempts - 1:
                    raise
                log.warning("FloodWait %ss on chat %s", e.seconds, chat_id)
                self.state.flood_wait_until = _now() + dt.timedelta(seconds=e.seconds)
                await asyncio.sleep(e.seconds + 1)
                self.state.flood_wait_until = None

    async def _sync_pins(self, entity, ctx: "_ChatCtx") -> None:
        """Mirror Telegram's current pinned set (pins/unpins of old messages included).

        Pinned messages not yet archived (e.g. older than sync_since) are saved too,
        so the pinned bar can always show them.
        """
        pinned = [m async for m in self.client.iter_messages(entity, filter=InputMessagesFilterPinned)]
        ids = [m.id for m in pinned]
        have = {
            r["message_id"]
            for r in await self.pool.fetch(
                "SELECT message_id FROM messages WHERE chat_id = $1 AND message_id = ANY($2::bigint[])",
                ctx.chat_id, ids,
            )
        }
        for m in pinned:
            if m.id not in have:
                await self.process_message(m, ctx)
        await self.pool.execute(
            """
            UPDATE messages SET pinned = (message_id = ANY($2::bigint[]))
            WHERE chat_id = $1 AND (pinned OR message_id = ANY($2::bigint[]))
            """,
            ctx.chat_id, ids,
        )

    async def _checkpoint(self, ctx: "_ChatCtx", cursor: int, finished: bool = False) -> bool:
        """Flush pending uploads, persist cursor. Returns False if the chat got disabled."""
        await self._flush_uploads(ctx)
        ctx.seen_since_checkpoint = 0
        new, ctx.new_messages = ctx.new_messages, 0
        row = await self.pool.fetchrow(
            "UPDATE chats SET last_msg_id = GREATEST(COALESCE(last_msg_id, 0), $2),"
            " last_synced_at = CASE WHEN $3 THEN now() ELSE last_synced_at END,"
            " message_count = message_count + $4"
            " WHERE chat_id = $1 RETURNING sync_enabled",
            ctx.chat_id, cursor, finished, new,
        )
        return bool(row and row["sync_enabled"])

    # ---- messages -------------------------------------------------------

    async def process_message(self, msg, ctx: "_ChatCtx") -> None:
        await self._upsert_sender(msg)
        if await self.save_message(msg, ctx.chat_id):
            ctx.new_messages += 1
        info = classify(msg)
        if info is not None:
            await self._save_media_meta(ctx.chat_id, msg.id, info)
            if ctx.sync_media:
                await self._store_media(msg, info, ctx)

    async def save_live(self, msg, chat_id: int) -> bool:
        """Store one message right away (sent from the web app, or a live Telegram update).

        Text, sender and media metadata are saved now; the media file itself is fetched by
        the next sync pass (its retry step picks up media rows without a stored file), so a
        big download never delays the chat. Returns False if the chat isn't in the DB.
        """
        chat = await self.pool.fetchrow("SELECT * FROM chats WHERE chat_id = $1", chat_id)
        if chat is None:
            return False
        ctx = _ChatCtx(chat)
        ctx.sync_media = False  # metadata only here
        await self.process_message(msg, ctx)
        if ctx.new_messages:
            await self.pool.execute(
                "UPDATE chats SET message_count = message_count + $2 WHERE chat_id = $1",
                chat_id, ctx.new_messages,
            )
        return True

    async def save_message(self, msg, chat_id: int) -> bool:
        """Upsert one message. Returns True if it was new (not an update)."""
        raw = jsonable(msg.to_dict())
        text = msg.message if isinstance(getattr(msg, "message", None), str) else None
        return await self.pool.fetchval(
            """
            INSERT INTO messages (chat_id, message_id, sender_id, date, text, reply_to,
                                  grouped_id, fwd_from_name, edit_date, raw, out, pinned)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET
              sender_id = EXCLUDED.sender_id, date = EXCLUDED.date, text = EXCLUDED.text,
              reply_to = EXCLUDED.reply_to, grouped_id = EXCLUDED.grouped_id,
              fwd_from_name = EXCLUDED.fwd_from_name, edit_date = EXCLUDED.edit_date,
              raw = EXCLUDED.raw, out = EXCLUDED.out, pinned = EXCLUDED.pinned
            RETURNING (xmax = 0) AS inserted
            """,
            chat_id, msg.id, msg.sender_id, msg.date,
            text.replace("\x00", "") if text else text,
            msg.reply_to_msg_id, msg.grouped_id, fwd_name(msg), msg.edit_date,
            raw, bool(msg.out), bool(getattr(msg, "pinned", False)),
        )

    async def _upsert_sender(self, msg) -> None:
        sid = msg.sender_id
        if sid is None or sid in self._known_users:
            return
        sender = msg.sender
        if sender is None:
            try:
                sender = await msg.get_sender()
            except Exception:
                sender = None
        name = utils.get_display_name(sender) if sender is not None else None
        username = getattr(sender, "username", None)
        row = await self.pool.fetchrow(
            """
            INSERT INTO users (user_id, name, username) VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET
              name = COALESCE(EXCLUDED.name, users.name),
              username = COALESCE(EXCLUDED.username, users.username)
            RETURNING avatar_checked
            """,
            sid, name or None, username,
        )
        if sender is not None and not row["avatar_checked"]:
            # Download the avatar once; later passes keep the stored one.
            path = None
            try:
                dest = self.s.media_dir / "avatars" / f"{sid}.jpg"
                dest.parent.mkdir(parents=True, exist_ok=True)
                got = await self.client.download_profile_photo(sender, file=str(dest), download_big=False)
                if got:
                    path = f"avatars/{sid}.jpg"
            except FloodWaitError:
                raise
            except Exception as e:
                log.warning("avatar download for %s failed: %s", sid, e)
            await self.pool.execute(
                "UPDATE users SET avatar_path = $2, avatar_checked = true WHERE user_id = $1", sid, path
            )
        self._known_users.add(sid)

    # ---- media ----------------------------------------------------------

    async def _save_media_meta(self, chat_id: int, message_id: int, info: MediaInfo) -> None:
        await self.pool.execute(
            """
            INSERT INTO media (chat_id, message_id, kind, mime, size, width, height, duration,
                               thumb_b64, file_name)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET
              kind = EXCLUDED.kind, mime = EXCLUDED.mime, size = EXCLUDED.size,
              width = EXCLUDED.width, height = EXCLUDED.height, duration = EXCLUDED.duration,
              thumb_b64 = EXCLUDED.thumb_b64, file_name = EXCLUDED.file_name
            """,
            chat_id, message_id, info.kind, info.mime, info.size, info.width, info.height,
            info.duration, info.thumb_b64, info.file_name,
        )

    async def _set_media(self, chat_id: int, message_id: int, **cols) -> None:
        keys = list(cols)
        sets = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(keys))
        await self.pool.execute(
            f"UPDATE media SET {sets} WHERE chat_id = $1 AND message_id = $2",
            chat_id, message_id, *cols.values(),
        )

    def _use_gphotos(self, info: MediaInfo) -> bool:
        if self.gphotos is None or info.kind not in GPHOTOS_KINDS:
            return False
        limit = PHOTO_MAX_BYTES if info.kind == "photo" else VIDEO_MAX_BYTES
        return (info.size or 0) <= limit

    async def _store_media(self, msg, info: MediaInfo, ctx: "_ChatCtx") -> None:
        chat_id, mid = ctx.chat_id, msg.id
        existing = await self.pool.fetchrow(
            "SELECT gphotos_media_id, file_path FROM media WHERE chat_id = $1 AND message_id = $2",
            chat_id, mid,
        )
        if existing and (existing["gphotos_media_id"] or existing["file_path"]):
            return  # already stored on an earlier pass
        if self.s.max_file_bytes and (info.size or 0) > self.s.max_file_bytes:
            await self._set_media(chat_id, mid, error=f"skipped: larger than MAX_FILE_MB ({info.size} bytes)")
            return

        free = self.disk_free()
        if free is not None and free - (info.size or 0) < DISK_RESERVE_BYTES:
            self.state.disk_low = True
            # Not "skipped:" so the retry step tries again once space is freed.
            await self._set_media(chat_id, mid, error=f"Disk almost full ({free / 2**30:.1f} GB free)")
            return
        self.state.disk_low = False

        if self._use_gphotos(info):
            self.s.temp_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.s.temp_dir / f"{chat_id}_{mid}{info.ext}"
            try:
                got = await self.client.download_media(msg, file=str(tmp))
                if not got:
                    raise RuntimeError("download returned nothing")
                token = await self.gphotos.upload(Path(got), info.mime or "application/octet-stream")
                ctx.pending.append(_Pending(mid, token, (msg.message or "")))
            except FloodWaitError:
                raise
            except Exception as e:
                log.warning("gphotos upload for %s/%s failed: %s", chat_id, mid, e)
                await self._set_media(chat_id, mid, error=f"{type(e).__name__}: {e}"[:500])
            finally:
                tmp.unlink(missing_ok=True)
            if len(ctx.pending) >= 50:
                await self._flush_uploads(ctx)
            return

        rel = local_rel_path(chat_id, mid, info)
        dest = self.s.media_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            got = await self.client.download_media(msg, file=str(part))
            if not got:
                raise RuntimeError("download returned nothing")
            Path(got).replace(dest)
            await self._set_media(chat_id, mid, file_path=rel, error=None)
        except FloodWaitError:
            raise
        except Exception as e:
            log.warning("download of %s/%s failed: %s", chat_id, mid, e)
            part.unlink(missing_ok=True)
            await self._set_media(chat_id, mid, error=f"{type(e).__name__}: {e}"[:500])

    def disk_free(self) -> int | None:
        try:
            self.s.media_dir.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.s.media_dir).free
        except OSError:
            return None

    async def _flush_uploads(self, ctx: "_ChatCtx") -> None:
        if not ctx.pending or self.gphotos is None:
            return
        pending, ctx.pending = ctx.pending, []
        if ctx.album_id is None:
            try:
                ctx.album_id = await self.gphotos.create_album(ctx.title or f"Telegram {ctx.chat_id}")
                await self.pool.execute(
                    "UPDATE chats SET gphotos_album_id = $2 WHERE chat_id = $1", ctx.chat_id, ctx.album_id
                )
            except Exception as e:
                log.warning("album creation for chat %s failed: %s", ctx.chat_id, e)
        try:
            results = await self.gphotos.batch_create(
                [(p.upload_token, p.description) for p in pending], ctx.album_id
            )
        except Exception as e:
            results = None
            err = f"{type(e).__name__}: {e}"[:500]
            for p in pending:
                await self._set_media(ctx.chat_id, p.message_id, error=err)
        if results is None:
            return
        for p, res in zip(pending, results):
            if res.media_id:
                await self._set_media(ctx.chat_id, p.message_id, gphotos_media_id=res.media_id, error=None)
            else:
                await self._set_media(ctx.chat_id, p.message_id, error=(res.error or "unknown")[:500])

    async def _retry_all_failed(self) -> None:
        """Retry failed media in every chat, including chats whose sync or media is now off
        (a normal pass only retries chats with both on, so old failures there stayed failed)."""
        self._retry_failed = False
        st = self.state
        try:
            chats = await self.pool.fetch(
                """
                SELECT c.* FROM chats c
                WHERE EXISTS (SELECT 1 FROM media m WHERE m.chat_id = c.chat_id
                                AND m.gphotos_media_id IS NULL AND m.file_path IS NULL
                                AND m.error IS NOT NULL AND m.error NOT LIKE 'skipped:%')
                ORDER BY c.chat_id
                """
            )
            for chat in chats:
                st.current_chat_id = chat["chat_id"]
                st.current_chat_title = f"{chat['title']} (retrying failed media)"
                st.progress = 0
                try:
                    entity = await self._resolve(chat["chat_id"])
                    await self._retry_media(entity, _ChatCtx(chat), failed_only=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("retrying failed media of chat %s failed", chat["chat_id"])
                    st.chat_errors[chat["chat_id"]] = f"{type(e).__name__}: {e}"
        finally:
            st.retrying_failed = self._retry_failed
            st.media_epoch += 1

    async def _retry_media(self, entity, ctx: "_ChatCtx", failed_only: bool = False) -> None:
        """Retry failed downloads/uploads and backfill media synced while sync_media was off
        (only the failed ones with `failed_only`).

        Walks all pending rows once per pass, RETRY_LIMIT at a time; anything that fails
        again is left for the next pass.
        """
        after = 0
        while True:
            rows = await self.pool.fetch(
                """
                SELECT message_id FROM media
                WHERE chat_id = $1 AND message_id > $3
                  AND gphotos_media_id IS NULL AND file_path IS NULL
                  AND (error IS NULL OR error NOT LIKE 'skipped:%')
                  AND (error IS NOT NULL OR NOT $4)
                ORDER BY message_id LIMIT $2
                """,
                ctx.chat_id, RETRY_LIMIT, after, failed_only,
            )
            if not rows:
                break
            ids = [r["message_id"] for r in rows]
            after = ids[-1]
            msgs = await self._flood_retry(lambda: self.client.get_messages(entity, ids=ids), ctx.chat_id)
            for mid, msg in zip(ids, msgs):
                if msg is None:
                    await self._set_media(ctx.chat_id, mid, error="skipped: message no longer exists")
                    continue
                info = classify(msg)
                if info is None:
                    await self._set_media(ctx.chat_id, mid, error="skipped: media no longer available")
                    continue
                await self._save_media_meta(ctx.chat_id, mid, info)
                await self._store_media(msg, info, ctx)
            await self._flush_uploads(ctx)


class _ChatCtx:
    def __init__(self, chat: asyncpg.Record):
        self.chat_id: int = chat["chat_id"]
        self.title: str | None = chat["title"]
        self.sync_media: bool = bool(chat["sync_media"])
        self.album_id: str | None = chat["gphotos_album_id"]
        self.pending: list[_Pending] = []
        self.seen_since_checkpoint = 0
        self.new_messages = 0  # inserted since the last checkpoint (for chats.message_count)
