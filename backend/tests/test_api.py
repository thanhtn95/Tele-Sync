import datetime as dt
from types import SimpleNamespace

import httpx
import pytest

from app.main import app
from app.sync import SyncWorker
from app.config import load_settings

from .fakes import CHAT_ID, FakeGPhotos, FakeTG
from .test_sync import _add_chat, _populate, _settings


@pytest.fixture
async def api(pool, tmp_path):
    tg, gp = FakeTG(), FakeGPhotos()
    import asyncio
    app.state.pool, app.state.client, app.state.gphotos = pool, tg, gp
    app.state.worker = SyncWorker(pool, tg, gp, _settings(tmp_path))
    app.state.dialogs_lock = asyncio.Lock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield SimpleNamespace(http=c, tg=tg, gp=gp, worker=app.state.worker)


def _dialog(id_, name, kind):
    return SimpleNamespace(id=id_, name=name, entity=None, is_user=kind == "user",
                           is_group=kind == "group", is_channel=kind != "user")


async def test_dialogs_refresh_preserves_flags(api, pool):
    api.tg.dialogs = [_dialog(CHAT_ID, "Group", "group"), _dialog(5, "Bob", "user"),
                      _dialog(-1009, "News", "channel")]
    r = await api.http.get("/api/dialogs")
    assert r.status_code == 200
    assert {d["chat_id"]: d["type"] for d in r.json()} == {CHAT_ID: "group", 5: "user", -1009: "channel"}

    r = await api.http.patch(f"/api/chats/{CHAT_ID}", json={"sync_enabled": True, "sync_since": "2024-01-01T00:00:00Z"})
    assert r.status_code == 200 and r.json()["sync_enabled"] is True

    api.tg.dialogs[0].name = "Renamed"
    r = await api.http.get("/api/dialogs?refresh=true")
    g = next(d for d in r.json() if d["chat_id"] == CHAT_ID)
    assert g["title"] == "Renamed" and g["sync_enabled"] is True
    assert g["sync_since"].startswith("2024-01-01")

    # not refreshed without ?refresh
    api.tg.dialogs = []
    assert len((await api.http.get("/api/dialogs")).json()) == 3


async def test_patch(api, pool):
    await _add_chat(pool, sync_enabled=False)
    r = await api.http.patch(f"/api/chats/{CHAT_ID}", json={"sync_media": False})
    assert r.json()["sync_media"] is False and r.json()["sync_enabled"] is False
    r = await api.http.patch(f"/api/chats/{CHAT_ID}", json={"sync_since": None})
    assert r.status_code == 200 and r.json()["sync_since"] is None
    assert (await api.http.patch(f"/api/chats/{CHAT_ID}", json={"sync_enabled": None})).status_code == 422
    assert (await api.http.patch("/api/chats/999", json={"sync_enabled": True})).status_code == 404


async def test_messages_pagination(api, pool):
    await _add_chat(pool)
    _populate(api.tg)
    await api.worker.run_pass()

    r = (await api.http.get(f"/api/chats/{CHAT_ID}/messages?limit=4")).json()
    assert [m["id"] for m in r["messages"]] == [10, 9, 8, 7] and r["has_more"]
    r = (await api.http.get(f"/api/chats/{CHAT_ID}/messages?limit=4&before=7")).json()
    assert [m["id"] for m in r["messages"]] == [6, 5, 4, 3]
    by_id = {m["id"]: m for m in r["messages"]}
    assert by_id[3]["reply"] == {"message_id": 2, "found": True, "sender_name": "Alice A",
                                 "text": "hello *world*", "media_kind": None}
    assert by_id[4]["grouped_id"] == "7000000000000000123"
    assert by_id[4]["media"]["gphotos_media_id"] == "gp-tok-0"
    assert by_id[6]["media"]["kind"] == "voice"
    r = (await api.http.get(f"/api/chats/{CHAT_ID}/messages?limit=4&before=3")).json()
    assert [m["id"] for m in r["messages"]] == [2, 1] and not r["has_more"]
    assert r["messages"][0]["entities"][0]["_"] == "MessageEntityBold"
    assert r["messages"][1]["service"] == "Alice A created the group"

    status = (await api.http.get("/api/sync/status")).json()
    assert status["chats"][0]["message_count"] == 10 and status["running"] is False

    urls = (await api.http.post("/api/gphotos/urls", json={"media_ids": ["a", "b"]})).json()
    assert urls == {"urls": {"a": "https://lh3.example/a", "b": "https://lh3.example/b"}}
