from types import SimpleNamespace

from app import main as main_mod

from .fakes import ALICE, BOB, CHAT_ID, _photo, make_msg
from .test_api import api  # noqa: F401  (fixture)
from .test_sync import _add_chat, _populate


async def test_send_text_and_reply(api, pool):  # noqa: F811
    await _add_chat(pool)
    _populate(api.tg)
    await api.worker.run_pass()
    before = await pool.fetchval("SELECT message_count FROM chats")

    r = await api.http.post(f"/api/chats/{CHAT_ID}/send", json={"text": "  hello *not bold*  ", "reply_to": 2})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    # sent exactly as typed (trimmed), no markdown parsing, as a reply
    assert api.tg.sent == [("hello *not bold*", 2, None)]
    row = await pool.fetchrow("SELECT text, out, reply_to FROM messages WHERE message_id = $1", mid)
    assert dict(row) == {"text": "hello *not bold*", "out": True, "reply_to": 2}
    assert await pool.fetchval("SELECT message_count FROM chats") == before + 1

    # it is returned by the "newer than" query the page polls
    msgs = (await api.http.get(f"/api/chats/{CHAT_ID}/messages?after={mid - 1}")).json()["messages"]
    assert [m["id"] for m in msgs] == [mid] and msgs[0]["reply"]["message_id"] == 2


async def test_send_errors(api, pool):  # noqa: F811
    await _add_chat(pool)
    h = api.http
    assert (await h.post(f"/api/chats/{CHAT_ID}/send", json={"text": ""})).status_code == 422
    assert (await h.post(f"/api/chats/{CHAT_ID}/send", json={"text": "   "})).status_code == 422
    assert (await h.post(f"/api/chats/{CHAT_ID}/send", json={"text": "x" * 4097})).status_code == 422
    assert (await h.post("/api/chats/999/send", json={"text": "hi"})).status_code == 404
    api.tg.forbid_send = True
    r = await h.post(f"/api/chats/{CHAT_ID}/send", json={"text": "hi"})
    assert r.status_code == 400 and "refused" in r.json()["detail"]


async def test_live_updates_saved_for_synced_chats_only(api, pool, monkeypatch):  # noqa: F811
    await _add_chat(pool)
    await pool.execute("INSERT INTO chats (chat_id, title, sync_enabled) VALUES (5, 'off', false)")
    handlers = []
    monkeypatch.setattr(api.tg, "add_event_handler", lambda fn, ev: handlers.append(fn))
    main_mod._register_live_updates(api.tg, api.worker)
    assert len(handlers) == 2  # new + edited

    on_message = handlers[0]
    msg = make_msg(api.tg, 50, BOB.id, "live!", media=_photo(50))
    await on_message(SimpleNamespace(chat_id=CHAT_ID, message=msg))
    row = await pool.fetchrow("SELECT text FROM messages WHERE message_id = 50")
    assert row["text"] == "live!"
    # media metadata now, file later (by the sync pass's retry step)
    media = await pool.fetchrow("SELECT kind, file_path, gphotos_media_id FROM media WHERE message_id = 50")
    assert media["kind"] == "photo" and media["file_path"] is None and media["gphotos_media_id"] is None
    assert await pool.fetchval("SELECT message_count FROM chats WHERE chat_id = $1", CHAT_ID) == 1

    # an edit updates the text without double counting
    edited = make_msg(api.tg, 50, BOB.id, "live! (edited)")
    await handlers[1](SimpleNamespace(chat_id=CHAT_ID, message=edited))
    assert await pool.fetchval("SELECT text FROM messages WHERE message_id = 50") == "live! (edited)"
    assert await pool.fetchval("SELECT message_count FROM chats WHERE chat_id = $1", CHAT_ID) == 1

    # chats that aren't synced are ignored
    await on_message(SimpleNamespace(chat_id=5, message=make_msg(api.tg, 7, ALICE.id, "x")))
    assert await pool.fetchval("SELECT count(*) FROM messages WHERE chat_id = 5") == 0
