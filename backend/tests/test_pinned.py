import datetime as dt

from telethon.tl import types

from app.sync import SyncWorker

from .fakes import ALICE, BOB, CHAT_ID, T0, FakeTG, make_msg, make_service
from .test_api import api  # noqa: F401  (fixture)
from .test_sync import _add_chat, _settings


def _chat(tg):
    return [
        make_msg(tg, 1, ALICE.id, "old rules", pinned=True),
        make_msg(tg, 2, BOB.id, "hello"),
        make_msg(tg, 3, ALICE.id, "meeting at 5", pinned=True),
        make_service(tg, 4, ALICE.id, types.MessageActionPinMessage()),
        make_msg(tg, 5, BOB.id, "ok"),
    ]


async def test_pins_mirror_telegram(pool, tmp_path):
    await _add_chat(pool, sync_since=T0 + dt.timedelta(minutes=2, seconds=30))  # msg 1/2 excluded
    tg = FakeTG()
    tg.messages = _chat(tg)
    tg.messages[3].reply_to = types.MessageReplyHeader(reply_to_msg_id=3)
    w = SyncWorker(pool, tg, None, _settings(tmp_path))
    await w.run_pass()

    pinned = [r["message_id"] for r in await pool.fetch(
        "SELECT message_id FROM messages WHERE pinned ORDER BY 1")]
    # msg 1 is older than sync_since but pinned -> archived anyway
    assert pinned == [1, 3]
    assert await pool.fetchval("SELECT count(*) FROM messages WHERE message_id = 2") == 0

    # unpin 1, pin 5 in Telegram; the next pass mirrors it even though 1 is below the cursor
    tg.messages[0].pinned = False
    tg.messages[4].pinned = True
    await w.run_pass()
    pinned = [r["message_id"] for r in await pool.fetch(
        "SELECT message_id FROM messages WHERE pinned ORDER BY 1")]
    assert pinned == [3, 5]


async def test_pinned_api(api, pool):  # noqa: F811
    await _add_chat(pool)
    api.tg.messages = _chat(api.tg)
    api.tg.messages[3].reply_to = types.MessageReplyHeader(reply_to_msg_id=3)
    await api.worker.run_pass()

    r = (await api.http.get(f"/api/chats/{CHAT_ID}/pinned")).json()
    assert [p["id"] for p in r] == [3, 1]
    assert r[0]["text"] == "meeting at 5" and r[0]["sender_name"] == "Alice A"

    msgs = {m["id"]: m for m in (await api.http.get(f"/api/chats/{CHAT_ID}/messages")).json()["messages"]}
    assert msgs[3]["pinned"] is True and msgs[2]["pinned"] is False
    assert msgs[4]["service"] == "Alice A pinned"
    assert msgs[4]["reply"]["text"] == "meeting at 5"
