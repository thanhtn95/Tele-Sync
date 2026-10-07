from app import main as main_mod

from .fakes import ALICE, CHAT_ID, _photo, make_msg
from .test_api import api  # noqa: F401  (fixture)
from .test_sync import _add_chat


async def test_retry_failed_covers_chats_with_sync_off(api, pool, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main_mod, "_media_stats_cache", {"at": 0.0, "value": None, "epoch": 0})
    await _add_chat(pool)
    api.tg.messages = [make_msg(api.tg, i, ALICE.id, "", media=_photo(i)) for i in range(1, 6)]
    api.gp.fail_all = True
    await api.worker.run_pass()
    # sync switched off since: a normal pass never retries this chat again
    await pool.execute("UPDATE chats SET sync_enabled = false")
    # never downloaded (media was off): "Retry failed" must not start downloading these
    await pool.execute("INSERT INTO messages (chat_id, message_id) VALUES ($1, 99)", CHAT_ID)
    await pool.execute("INSERT INTO media (chat_id, message_id, kind) VALUES ($1, 99, 'photo')", CHAT_ID)
    h = api.http

    st = (await h.get("/api/sync/status")).json()
    assert st["media"]["failed"] == 5 and st["retrying_failed"] is False
    assert st["media"]["failed_by_chat"] == [{"chat_id": CHAT_ID, "title": "Test Group", "n": 5}]

    api.gp.fail_all = False
    await api.worker.run_pass()
    assert (await h.get("/api/sync/status")).json()["media"]["failed"] == 5  # still stuck

    assert (await h.post("/api/media/retry-failed")).status_code == 202
    assert (await h.get("/api/sync/status")).json()["retrying_failed"] is True
    await api.worker.run_pass(only_requested=True)

    st = (await h.get("/api/sync/status")).json()  # recounted at once, not after the cache TTL
    assert st["retrying_failed"] is False
    assert st["media"]["failed"] == 0 and st["media"]["in_gphotos"] == 5 and st["media"]["not_downloaded"] == 1
    assert st["media"]["failed_by_chat"] == []
    assert await pool.fetchval("SELECT error IS NULL AND file_path IS NULL FROM media WHERE message_id = 99")
