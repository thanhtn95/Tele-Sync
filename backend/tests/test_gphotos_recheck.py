import httpx
import pytest

from app.gphotos import GPhotos
from scripts import gphotos_recheck as rc


def _gp(handler):
    gp = GPhotos("id", "secret", "refresh")

    async def fake_request(method, url, **kw):
        return handler(url, kw.get("params"))

    gp._request = fake_request
    return gp


async def test_missing_ids_uses_explicit_statuses():
    have = {f"id{i}" for i in range(0, 120, 2)}  # even ids live in the linked account

    def handler(url, params):
        ids = [v for _, v in params]
        return httpx.Response(200, json={"mediaItemResults": [
            {"mediaItem": {"id": m}} if m in have else {"status": {"code": 3, "message": "Invalid media item ID."}}
            for m in ids]})

    gp = _gp(handler)
    ids = [f"id{i}" for i in range(120)]
    missing = await rc.missing_ids(gp, ids)
    await gp.aclose()
    assert missing == [f"id{i}" for i in range(1, 120, 2)]


async def test_failed_call_aborts_instead_of_clearing():
    gp = _gp(lambda url, params: httpx.Response(401, text="unauthorized"))
    with pytest.raises(SystemExit, match="Nothing was changed"):
        await rc.missing_ids(gp, ["a", "b"])
    await gp.aclose()


async def test_stale_albums():
    codes = {"good": 200, "old": 404, "other": 403}
    gp = _gp(lambda url, params: httpx.Response(codes[url.rsplit("/", 1)[1]], json={}))
    assert await rc.stale_albums(gp, [(1, "good"), (2, "old"), (3, "other")]) == [2, 3]
    gp2 = _gp(lambda url, params: httpx.Response(503, text="down"))
    with pytest.raises(SystemExit):
        await rc.stale_albums(gp2, [(1, "x")])
    await gp.aclose()
    await gp2.aclose()


async def test_duplicate_ids_are_sent_once():
    sent = []

    def handler(url, params):
        ids = [v for _, v in params]
        assert len(ids) == len(set(ids)), "Request must not contain duplicated ids."
        sent.extend(ids)
        return httpx.Response(200, json={"mediaItemResults": [
            {"status": {"code": 3}} if m == "gone" else {"mediaItem": {"id": m}} for m in ids]})

    gp = _gp(handler)
    assert await rc.missing_ids(gp, ["a", "gone", "a", "b", "gone"]) == ["gone"]
    await gp.aclose()
    assert sent == ["a", "gone", "b"]
