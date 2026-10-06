import json

import httpx

from app import gphotos
from app.gphotos import GPhotos, clip_description
from app.sync import SyncWorker

from .fakes import ALICE, FakeGPhotos, FakeTG, _photo, make_msg
from .test_sync import _add_chat, _settings


def test_clip_description():
    assert clip_description("short") == "short"
    assert clip_description(None) == ""
    vi = "Tiếng Việt có dấu 😀 " * 100  # ~2000 chars, far more bytes
    out = clip_description(vi)
    assert len(out.encode()) <= 1000 and out.endswith("…")
    assert len(out.encode("utf-16-le")) // 2 <= 1000 and len(out) <= 1000
    assert vi.startswith(out[:-1].rstrip())


def _gp_with(handler):
    gp = GPhotos("id", "secret", "refresh")

    async def fake_request(method, url, **kw):
        return handler(kw["json"])

    gp._request = fake_request
    return gp


def _ok(body):
    return httpx.Response(200, json={"newMediaItemResults": [
        {"uploadToken": it["simpleMediaItem"]["uploadToken"],
         "mediaItem": {"id": "id-" + it["simpleMediaItem"]["uploadToken"]}}
        for it in body["newMediaItems"]]})


async def test_batch_400_falls_back_per_item_and_drops_bad_description():
    calls = []

    def handler(body):
        calls.append(body)
        items = body["newMediaItems"]
        if any(it["description"] == "bad desc" for it in items):
            return httpx.Response(400, text=json.dumps({"error": {
                "code": 400, "message": "Description must not have more than 1000 characters."}}))
        if any(it["simpleMediaItem"]["uploadToken"] == "broken" for it in items):
            return httpx.Response(400, text='{"error": {"message": "Invalid upload token"}}')
        return _ok(body)

    gp = _gp_with(handler)
    res = await gp.batch_create([("a", "x"), ("b", "bad desc"), ("broken", ""), ("c", "")], "alb")
    await gp.aclose()
    assert [r.media_id for r in res] == ["id-a", "id-b", None, "id-c"]
    assert "Invalid upload token" in res[2].error
    # whole batch, then one per item, then "b" again without description
    assert len(calls[0]["newMediaItems"]) == 4 and len(calls) == 6
    assert calls[-1]["albumId"] == "alb"


async def test_long_captions_are_clipped_before_sending():
    seen = []

    def handler(body):
        seen.extend(it["description"] for it in body["newMediaItems"])
        return _ok(body)

    gp = _gp_with(handler)
    await gp.batch_create([("a", "ố" * 900)])
    await gp.aclose()
    assert len(seen[0].encode()) <= gphotos.DESCRIPTION_MAX_BYTES


async def test_retry_clears_whole_backlog_in_one_pass(pool, tmp_path):
    await _add_chat(pool)
    tg, gp = FakeTG(), FakeGPhotos()
    tg.messages = [make_msg(tg, i, ALICE.id, "", media=_photo(i)) for i in range(1, 251)]
    gp.fail_all = True
    w = SyncWorker(pool, tg, gp, _settings(tmp_path))
    await w.run_pass()
    assert await pool.fetchval("SELECT count(*) FROM media WHERE error IS NOT NULL") == 250

    gp.fail_all = False
    await w.run_pass()  # 250 pending > RETRY_LIMIT (100): all retried in this one pass
    assert await pool.fetchval("SELECT count(*) FROM media WHERE gphotos_media_id IS NOT NULL") == 250
    assert await pool.fetchval("SELECT count(*) FROM media WHERE error IS NOT NULL") == 0
