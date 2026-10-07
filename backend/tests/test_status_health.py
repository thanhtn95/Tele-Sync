import shutil
from collections import namedtuple

from app import main as main_mod
from app import sync as sync_mod

from .fakes import CHAT_ID
from .test_api import api  # noqa: F401  (fixture)
from .test_sync import _add_chat, _populate

Usage = namedtuple("Usage", "total used free")


async def test_status_media_and_disk(api, pool, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main_mod, "_media_stats_cache", {"at": 0.0, "value": None})
    await _add_chat(pool)
    _populate(api.tg)
    api.gp.fail_tokens = {"tok-0"}
    await api.worker.run_pass()
    await pool.execute(
        "UPDATE media SET error = 'skipped: larger than MAX_FILE_MB' WHERE message_id = 9 AND chat_id = $1", CHAT_ID)
    await pool.execute("UPDATE media SET file_path = NULL WHERE message_id = 9")
    await pool.execute("INSERT INTO messages (chat_id, message_id) VALUES ($1, 99)", CHAT_ID)
    await pool.execute("INSERT INTO media (chat_id, message_id, kind) VALUES ($1, 99, 'photo')", CHAT_ID)

    st = (await api.http.get("/api/sync/status")).json()
    m = st["media"]
    # photos 4,5 + video 10 -> Google Photos (4 failed); voice, sticker local; pdf skipped; 99 waiting
    assert m == {"in_gphotos": 2, "on_disk": 2, "failed": 1, "skipped": 1, "not_downloaded": 1,
                 "top_errors": [{"reason": "boom", "n": 1}]}
    d = st["disk"]
    assert d["total"] > 0 and d["used"] + d["free"] <= d["total"] and 0 <= d["percent"] <= 100
    assert st["disk_low"] is False


async def test_downloads_pause_when_disk_nearly_full(api, pool, monkeypatch):  # noqa: F811
    await _add_chat(pool)
    _populate(api.tg)
    real_disk_usage = shutil.disk_usage
    monkeypatch.setattr(sync_mod.shutil, "disk_usage", lambda p: Usage(30 * 2**30, 29 * 2**30, 2**30))
    await api.worker.run_pass()
    errs = [r["error"] for r in await pool.fetch("SELECT error FROM media ORDER BY message_id")]
    assert errs and all(e.startswith("Disk almost full (1.0 GB free)") for e in errs)
    assert api.tg.downloads == [] and api.gp.uploads == []
    assert api.worker.state.disk_low is True

    # space freed -> the retry step picks them all up
    monkeypatch.setattr(sync_mod.shutil, "disk_usage", real_disk_usage)
    await api.worker.run_pass()
    assert await pool.fetchval("SELECT count(*) FROM media WHERE error IS NOT NULL") == 0
    assert api.worker.state.disk_low is False
