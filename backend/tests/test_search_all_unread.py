import datetime as dt

from .fakes import ALICE, BOB, CHAT_ID, make_msg
from .test_api import api  # noqa: F401  (fixture)
from .test_sync import _add_chat

OTHER = -100555
NOW = dt.datetime.now(dt.timezone.utc)


async def _two_chats(pool):
    await _add_chat(pool)
    await pool.execute("INSERT INTO chats (chat_id, title, type, sync_enabled) VALUES ($1, 'Other', 'group', true)", OTHER)
    rows = [  # (chat, id, minutes ago, text)
        (CHAT_ID, 1, 50, "Tiếng Việt một"), (CHAT_ID, 2, 40, "hello"), (CHAT_ID, 3, 30, "tieng viet hai"),
        (OTHER, 1, 45, "TIẾNG VIỆT ba"), (OTHER, 2, 35, "nothing"), (OTHER, 3, 31, "tiếng việt bốn"),
    ]
    for chat, mid, ago, text in rows:
        await pool.execute(
            "INSERT INTO messages (chat_id, message_id, date, text, sender_id) VALUES ($1, $2, $3, $4, 111)",
            chat, mid, NOW - dt.timedelta(minutes=ago), text)
    await pool.execute("INSERT INTO users (user_id, name) VALUES (111, 'Alice A')")


async def test_search_all_chats(api, pool):  # noqa: F811
    await _two_chats(pool)
    h = api.http
    r = (await h.get("/api/search", params={"q": "tieng viet", "limit": 3})).json()
    got = [(x["chat_id"], x["id"]) for x in r["results"]]
    assert got == [(CHAT_ID, 3), (OTHER, 3), (OTHER, 1)]  # newest first, across chats
    assert r["total"] == 4 and r["has_more"] and r["results"][1]["chat_title"] == "Other"
    assert r["results"][0]["sender_name"] == "Alice A"
    last = r["results"][-1]
    r2 = (await h.get("/api/search", params={
        "q": "tieng viet", "limit": 3, "before_date": last["date"],
        "before_chat": last["chat_id"], "before_id": last["id"]})).json()
    assert [(x["chat_id"], x["id"]) for x in r2["results"]] == [(CHAT_ID, 1)]
    assert not r2["has_more"] and r2["total"] is None
    assert (await h.get("/api/search", params={"q": "zzz"})).json()["total"] == 0
    assert (await h.get("/api/search", params={"q": " "})).status_code == 422


async def test_unread_counts_and_mark_read(api, pool):  # noqa: F811
    await _add_chat(pool)
    h = api.http
    # history imported now is older than last_read_at -> not unread
    api.tg.messages = [make_msg(api.tg, i, BOB.id, f"old {i}") for i in range(1, 4)]
    await api.worker.run_pass()
    unread = lambda d: next(c for c in d if c["chat_id"] == CHAT_ID)["unread_count"]  # noqa: E731
    assert unread((await h.get("/api/dialogs")).json()) == 0

    # new messages arrive (live) after you last looked; your own don't count
    await pool.execute("UPDATE chats SET last_read_at = now() - interval '1 hour'")
    for i, (sender, out) in enumerate([(BOB.id, False), (ALICE.id, False), (ALICE.id, True)], start=10):
        m = make_msg(api.tg, i, sender, f"new {i}", out=out)
        m.date = NOW
        await api.worker.save_live(m, CHAT_ID)
    assert unread((await h.get("/api/dialogs")).json()) == 2
    st = (await h.get("/api/sync/status")).json()
    assert st["chats"][0]["unread_count"] == 2

    r = await h.post(f"/api/chats/{CHAT_ID}/read")
    assert r.status_code == 200
    assert unread((await h.get("/api/dialogs")).json()) == 0
    assert (await h.post("/api/chats/999/read")).status_code == 404

    # chats that aren't synced never show a badge
    await pool.execute("UPDATE chats SET sync_enabled = false, last_read_at = now() - interval '1 day'")
    assert unread((await h.get("/api/dialogs")).json()) == 0
