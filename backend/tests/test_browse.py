from telethon.tl import types

from .fakes import ALICE, BOB, CHAT_ID, _doc, _photo, make_msg
from .test_api import api  # noqa: F401  (fixture)
from .test_sync import _add_chat


def _chat(tg):
    msgs = []
    for i in range(1, 101):
        if i % 10 == 0:
            msgs.append(make_msg(tg, i, ALICE.id, f"photo {i}", media=_photo(i)))
        elif i % 25 == 3:
            msgs.append(make_msg(tg, i, BOB.id, "", media=_doc(i, "application/pdf", [
                types.DocumentAttributeFilename(file_name=f"f{i}.pdf")])))
        else:
            msgs.append(make_msg(tg, i, BOB.id, f"m{i}"))
    msgs.append(make_msg(tg, 101, BOB.id, "", media=_doc(101, "audio/ogg", [
        types.DocumentAttributeAudio(duration=3, voice=True)])))
    return msgs


async def _setup(api, pool):  # noqa: F811
    await _add_chat(pool)
    api.tg.messages = _chat(api.tg)
    await api.worker.run_pass()


async def test_around_after_before(api, pool):  # noqa: F811
    await _setup(api, pool)
    h = api.http
    r = (await h.get(f"/api/chats/{CHAT_ID}/messages?around=40&limit=10")).json()
    ids = [m["id"] for m in r["messages"]]
    assert ids == [45, 44, 43, 42, 41, 40, 39, 38, 37, 36] and r["has_more"] and r["has_newer"]

    r = (await h.get(f"/api/chats/{CHAT_ID}/messages?after=45&limit=3")).json()
    assert [m["id"] for m in r["messages"]] == [48, 47, 46] and r["has_newer"]
    r = (await h.get(f"/api/chats/{CHAT_ID}/messages?after=98&limit=5")).json()
    assert [m["id"] for m in r["messages"]] == [101, 100, 99] and not r["has_newer"]

    r = (await h.get(f"/api/chats/{CHAT_ID}/messages?around=3&limit=10")).json()
    assert [m["id"] for m in r["messages"]][-1] == 1 and not r["has_more"] and r["has_newer"]

    r = (await h.get(f"/api/chats/{CHAT_ID}/messages?limit=5")).json()
    assert r["has_newer"] is False and r["messages"][0]["id"] == 101


async def test_media_tabs_and_neighbours(api, pool):  # noqa: F811
    await _setup(api, pool)
    h = api.http
    r = (await h.get(f"/api/chats/{CHAT_ID}/media?limit=4")).json()
    assert [i["id"] for i in r["items"]] == [100, 90, 80, 70] and r["has_more"]
    assert r["counts"] == {"media": 10, "files": 4, "voice": 1}
    assert r["items"][0]["text"] == "photo 100" and r["items"][0]["sender_name"] == "Alice A"
    assert r["items"][0]["media"]["kind"] == "photo"

    r = (await h.get(f"/api/chats/{CHAT_ID}/media?before=70&limit=100")).json()
    assert [i["id"] for i in r["items"]] == [60, 50, 40, 30, 20, 10] and r["has_more"] is False
    assert r["counts"] is None

    # viewer navigation: previous (older) and next (newer) of photo 40
    older = (await h.get(f"/api/chats/{CHAT_ID}/media?before=40&limit=1")).json()["items"]
    newer = (await h.get(f"/api/chats/{CHAT_ID}/media?after=40&limit=1")).json()["items"]
    assert older[0]["id"] == 30 and newer[0]["id"] == 50
    assert (await h.get(f"/api/chats/{CHAT_ID}/media?after=100&limit=1")).json()["items"] == []

    files = (await h.get(f"/api/chats/{CHAT_ID}/media?group=files")).json()["items"]
    assert [f["media"]["file_name"] for f in files] == ["f78.pdf", "f53.pdf", "f28.pdf", "f3.pdf"]
    voice = (await h.get(f"/api/chats/{CHAT_ID}/media?group=voice")).json()["items"]
    assert [v["id"] for v in voice] == [101]
    assert (await h.get(f"/api/chats/{CHAT_ID}/media?group=bogus")).status_code == 422
