from app import db
from app.sync import SyncWorker

from .fakes import ALICE, CHAT_ID, FakeTG, make_msg
from .test_sync import _add_chat, _populate, _settings


async def test_message_count_is_maintained(pool, tmp_path):
    await _add_chat(pool)
    tg = FakeTG()
    _populate(tg)
    w = SyncWorker(pool, tg, None, _settings(tmp_path))
    await w.run_pass()
    assert await pool.fetchval("SELECT message_count FROM chats") == 10

    # re-processing existing messages (edits) must not double count
    await pool.execute("UPDATE chats SET last_msg_id = 0")
    await w.run_pass()
    assert await pool.fetchval("SELECT message_count FROM chats") == 10

    tg.messages.append(make_msg(tg, 11, ALICE.id, "new"))
    await w.run_pass()
    assert await pool.fetchval("SELECT message_count FROM chats") == 11
    assert await pool.fetchval("SELECT count(*) FROM messages") == 11


async def test_migration_backfills_counts(pool):
    await pool.execute("INSERT INTO chats (chat_id) VALUES (1), (2)")
    await pool.execute("INSERT INTO messages (chat_id, message_id) SELECT 1, g FROM generate_series(1, 7) g")
    await pool.execute("ALTER TABLE chats DROP COLUMN message_count")
    await pool.execute("DELETE FROM schema_migrations WHERE name = '006_message_count.sql'")
    assert await db.migrate(pool) == ["006_message_count.sql"]
    assert dict(await pool.fetch("SELECT chat_id, message_count FROM chats ORDER BY 1")) == {1: 7, 2: 0}


async def test_statement_timeout_and_vacuum(pool):
    assert await pool.fetchval("SHOW statement_timeout") == "30s"
    await db.vacuum_after_migrations(pool)  # must not fail (VACUUM outside a transaction)
    # the lifted timeout does not leak back into the pool
    assert await pool.fetchval("SHOW statement_timeout") == "30s"
