import unicodedata

from telethon.tl import types

from .fakes import ALICE, BOB, CHAT_ID, _doc, make_msg
from .test_api import api  # noqa: F401  (fixture)
from .test_sync import _add_chat


def _chat(tg):
    texts = {
        1: "Tiếng Việt có dấu",
        2: "TIENG VIET khong dau",
        3: "Đường đi khó",
        4: "duong di de",
        5: "giảm 50% hôm nay",
        6: "500 khách",
        7: "a_b underscore",
        8: "nothing here",
    }
    msgs = [make_msg(tg, i, ALICE.id if i % 2 else BOB.id, t) for i, t in texts.items()]
    msgs.append(make_msg(tg, 9, BOB.id, "", media=_doc(9, "application/pdf", [
        types.DocumentAttributeFilename(file_name="Báo cáo tháng.pdf")])))
    msgs += [make_msg(tg, 100 + i, ALICE.id, f"lặp lại {i}") for i in range(60)]
    return msgs


async def _ids(api, q, **params):  # noqa: F811
    r = await api.http.get(f"/api/chats/{CHAT_ID}/search", params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_search(api, pool):  # noqa: F811
    await _add_chat(pool)
    api.tg.messages = _chat(api.tg)
    await api.worker.run_pass()

    ids = lambda r: [m["id"] for m in r["results"]]  # noqa: E731
    r = await _ids(api, "tieng viet")
    assert ids(r) == [2, 1] and r["total"] == 2
    assert ids(await _ids(api, "TIẾNG")) == [2, 1]
    assert ids(await _ids(api, "duong di")) == [4, 3]       # đ folds to d
    assert ids(await _ids(api, "Đường")) == [4, 3]
    assert ids(await _ids(api, "50%")) == [5]                # % is literal, not a wildcard
    assert ids(await _ids(api, "a_b")) == [7]                # _ is literal
    assert ids(await _ids(api, "bao cao")) == [9]            # file names too
    assert (await _ids(api, "zzz"))["total"] == 0

    first = await _ids(api, "lap lai", limit=50)
    assert first["total"] == 60 and not first["total_capped"]
    assert first["has_more"] and ids(first)[0] == 159
    nxt = await _ids(api, "lap lai", limit=50, before=ids(first)[-1])
    assert len(nxt["results"]) == 10 and not nxt["has_more"] and nxt["total"] is None

    assert (await api.http.get(f"/api/chats/{CHAT_ID}/search", params={"q": "  "})).status_code == 422


async def test_fold_text_matches_unicode_folding(pool):
    # Every Vietnamese letter folds the same way in SQL as via Unicode decomposition.
    letters = "aàáảãạăằắẳẵặâầấẩẫậeèéẻẽẹêềếểễệiìíỉĩịoòóỏõọôồốổỗộơờớởỡợuùúủũụưừứửữựyỳýỷỹỵ"
    word = letters + letters.upper() + "đĐ"
    folded = await pool.fetchval("SELECT fold_text($1)", word)
    expect = "".join(
        "d" if c in "đĐ" else "".join(ch for ch in unicodedata.normalize("NFKD", c)
                                      if not unicodedata.combining(ch)).lower()
        for c in word
    )
    assert folded == expect


async def test_total_is_capped(api, pool, monkeypatch):  # noqa: F811
    from app import main as main_mod
    monkeypatch.setattr(main_mod, "TOTAL_CAP", 25)
    await _add_chat(pool)
    api.tg.messages = _chat(api.tg)
    await api.worker.run_pass()
    r = await _ids(api, "lap lai")
    assert r["total"] == 25 and r["total_capped"] is True and len(r["results"]) == 50


async def test_migration_adds_trigram_indexes_when_pg_trgm_exists(pool):
    from app import db
    async with pool.acquire() as c:
        await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE EXTENSION pg_trgm;")
    await db.migrate(pool)
    names = {r["indexname"] for r in await pool.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    assert {"messages_search_trgm", "media_search_trgm"} <= names
    # stored folded column follows edits
    await pool.execute("INSERT INTO chats (chat_id) VALUES (1)")
    await pool.execute("INSERT INTO messages (chat_id, message_id, text) VALUES (1, 1, 'Đường Phố')")
    await pool.execute("UPDATE messages SET text = 'Tiếng Việt' WHERE message_id = 1")
    assert await pool.fetchval("SELECT search_text FROM messages") == "tieng viet"
