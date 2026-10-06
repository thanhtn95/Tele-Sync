import dataclasses

from telethon.tl import types

from app.config import load_settings
from app.sync import SyncWorker

from .fakes import (ALICE, BOB, CHAT_ID, FakeGPhotos, FakeTG, T0, _doc, _photo, make_msg,
                    make_service)


def _settings(tmp_path):
    return dataclasses.replace(load_settings(), media_dir=tmp_path / "files", temp_dir=tmp_path / "tmp",
                               max_file_bytes=10_000_000)


async def _add_chat(pool, **kw):
    cols = {"chat_id": CHAT_ID, "title": "Test Group", "type": "group", "sync_enabled": True, **kw}
    await pool.execute(
        f"INSERT INTO chats ({', '.join(cols)}) VALUES ({', '.join(f'${i+1}' for i in range(len(cols)))})",
        *cols.values(),
    )


def _populate(tg):
    album = 7_000_000_000_000_000_123
    tg.messages = [
        make_service(tg, 1, ALICE.id, types.MessageActionChatCreate(title="Test Group", users=[111])),
        make_msg(tg, 2, ALICE.id, "hello *world*",
                 entities=[types.MessageEntityBold(offset=6, length=7)]),
        make_msg(tg, 3, BOB.id, "reply", reply_to=types.MessageReplyHeader(reply_to_msg_id=2), out=True),
        make_msg(tg, 4, ALICE.id, "album caption", media=_photo(1), grouped_id=album),
        make_msg(tg, 5, ALICE.id, "", media=_photo(2), grouped_id=album),
        make_msg(tg, 6, BOB.id, "", media=_doc(3, "audio/ogg", [
            types.DocumentAttributeAudio(duration=7, voice=True)])),
        make_msg(tg, 7, BOB.id, "", media=_doc(4, "application/x-tgsticker", [
            types.DocumentAttributeSticker(alt="😀", stickerset=types.InputStickerSetEmpty())])),
        make_msg(tg, 8, ALICE.id, "fwd", fwd_from=types.MessageFwdHeader(date=T0, from_name="Carol")),
        make_msg(tg, 9, ALICE.id, "", media=_doc(5, "application/pdf", [
            types.DocumentAttributeFilename(file_name="Report Q1/2024.pdf")])),
        make_msg(tg, 10, ALICE.id, "", media=_doc(6, "video/mp4", [
            types.DocumentAttributeVideo(duration=12.4, w=640, h=360)])),
    ]


async def test_full_sync(pool, tmp_path):
    await _add_chat(pool)
    tg, gp = FakeTG(), FakeGPhotos()
    _populate(tg)
    w = SyncWorker(pool, tg, gp, _settings(tmp_path))
    await w.run_pass()

    chat = await pool.fetchrow("SELECT * FROM chats")
    assert chat["last_msg_id"] == 10
    assert chat["last_synced_at"] is not None
    assert chat["gphotos_album_id"] == "album-1"
    assert w.state.chat_errors == {}

    msgs = {r["message_id"]: r for r in await pool.fetch("SELECT * FROM messages")}
    assert len(msgs) == 10
    assert msgs[2]["text"] == "hello *world*"
    assert msgs[2]["raw"]["entities"][0]["_"] == "MessageEntityBold"
    assert msgs[3]["reply_to"] == 2 and msgs[3]["out"] is True
    assert msgs[4]["grouped_id"] == msgs[5]["grouped_id"] == 7_000_000_000_000_000_123
    assert msgs[8]["fwd_from_name"] == "Carol"
    assert msgs[1]["raw"]["action"]["_"] == "MessageActionChatCreate"

    media = {r["message_id"]: r for r in await pool.fetch("SELECT * FROM media")}
    assert media[4]["kind"] == "photo" and media[4]["gphotos_media_id"] == "gp-tok-0"
    assert media[4]["width"] == 1280 and media[4]["thumb_b64"]
    assert media[5]["gphotos_media_id"] == "gp-tok-1"
    assert media[10]["kind"] == "video" and media[10]["duration"] == 12
    assert media[10]["gphotos_media_id"] == "gp-tok-2"
    assert media[6]["kind"] == "voice" and media[6]["file_path"] == f"{CHAT_ID}/6.ogg"
    assert media[7]["kind"] == "sticker" and media[7]["file_path"].endswith(".tgs")
    assert media[9]["kind"] == "document" and media[9]["file_name"] == "Report Q1/2024.pdf"
    assert media[9]["file_path"] == f"{CHAT_ID}/9_Report Q1_2024.pdf"
    assert (tmp_path / "files" / media[9]["file_path"]).exists()
    # temp files for Google Photos uploads are removed
    assert list((tmp_path / "tmp").iterdir()) == []
    # one batchCreate with all three items, into the album
    assert len(gp.created) == 1 and gp.created[0][1] == "album-1" and len(gp.created[0][0]) == 3

    users = {r["user_id"]: r for r in await pool.fetch("SELECT * FROM users")}
    assert users[111]["name"] == "Alice A" and users[111]["avatar_path"] is None
    assert users[222]["avatar_path"] == "avatars/222.jpg"
    assert all(u["avatar_checked"] for u in users.values())

    # incremental: second pass only fetches new messages
    tg.messages.append(make_msg(tg, 11, BOB.id, "new"))
    n_downloads = len(tg.downloads)
    await w.run_pass()
    assert await pool.fetchval("SELECT last_msg_id FROM chats") == 11
    assert len(tg.downloads) == n_downloads
    assert await pool.fetchval("SELECT count(*) FROM messages") == 11


async def test_flood_wait_resumes(pool, tmp_path):
    await _add_chat(pool)
    tg = FakeTG()
    _populate(tg)
    tg.flood_after = 4
    w = SyncWorker(pool, tg, None, _settings(tmp_path))
    await w.run_pass()
    assert await pool.fetchval("SELECT last_msg_id FROM chats") == 10
    assert await pool.fetchval("SELECT count(*) FROM messages") == 10
    # without Google Photos, photos are stored locally
    assert await pool.fetchval("SELECT file_path FROM media WHERE message_id = 4") == f"{CHAT_ID}/4.jpg"


async def test_media_disabled_then_backfilled(pool, tmp_path):
    await _add_chat(pool, sync_media=False)
    tg, gp = FakeTG(), FakeGPhotos()
    _populate(tg)
    w = SyncWorker(pool, tg, gp, _settings(tmp_path))
    await w.run_pass()
    assert tg.downloads == [] or all("avatars" in d for d in tg.downloads)
    # metadata (thumbs, sizes) is still kept for the viewer
    assert await pool.fetchval("SELECT thumb_b64 FROM media WHERE message_id = 4")

    await pool.execute("UPDATE chats SET sync_media = true")
    await w.run_pass()
    assert await pool.fetchval(
        "SELECT count(*) FROM media WHERE gphotos_media_id IS NULL AND file_path IS NULL") == 0


async def test_failed_upload_is_recorded_and_retried(pool, tmp_path):
    await _add_chat(pool)
    tg, gp = FakeTG(), FakeGPhotos()
    _populate(tg)
    gp.fail_tokens = {"tok-0"}
    w = SyncWorker(pool, tg, gp, _settings(tmp_path))
    await w.run_pass()
    assert await pool.fetchval("SELECT error FROM media WHERE message_id = 4") == "boom"
    gp.fail_tokens = set()
    await w.run_pass()
    row = await pool.fetchrow("SELECT error, gphotos_media_id FROM media WHERE message_id = 4")
    assert row["error"] is None and row["gphotos_media_id"]


async def test_sync_since_and_disabled(pool, tmp_path):
    import datetime as dt
    await _add_chat(pool, sync_since=T0 + dt.timedelta(minutes=5, seconds=30))
    await pool.execute(
        "INSERT INTO chats (chat_id, title, sync_enabled) VALUES (1, 'off', false)")
    tg = FakeTG()
    _populate(tg)
    w = SyncWorker(pool, tg, None, _settings(tmp_path))
    await w.run_pass()
    ids = [r["message_id"] for r in await pool.fetch("SELECT message_id FROM messages ORDER BY 1")]
    assert ids == [6, 7, 8, 9, 10]
