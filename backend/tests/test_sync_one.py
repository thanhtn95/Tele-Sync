import asyncio
import dataclasses

from .fakes import CHAT_ID
from .test_api import api  # noqa: F401  (fixture)
from .test_sync import _add_chat

OTHER = -100555
OFF = -100777


async def _chats(pool):
    await _add_chat(pool)
    await pool.execute(
        "INSERT INTO chats (chat_id, title, type, sync_enabled) VALUES ($1, 'Other', 'group', true), "
        "($2, 'Off', 'group', false)", OTHER, OFF)


def _record(worker, gate=None):
    """Replace the real per-chat sync with one that just logs the order."""
    order = []

    async def fake(chat):
        order.append(chat["chat_id"])
        if gate is not None:
            await gate.wait()
    worker.sync_chat = fake
    return order


async def test_sync_one_endpoint(api, pool):  # noqa: F811
    await _chats(pool)
    order = _record(api.worker)
    h = api.http
    assert (await h.post(f"/api/chats/{OTHER}/sync")).status_code == 202
    assert (await h.post(f"/api/chats/{OTHER}/sync")).status_code == 202  # no duplicates
    assert (await h.get("/api/sync/status")).json()["queued_chat_ids"] == [OTHER]
    assert (await h.post(f"/api/chats/{OFF}/sync")).status_code == 409
    assert (await h.post("/api/chats/123/sync")).status_code == 404

    await api.worker.run_pass(only_requested=True)
    assert order == [OTHER]  # just that chat, not a full pass
    st = (await h.get("/api/sync/status")).json()
    assert st["queued_chat_ids"] == [] and st["last_pass_finished_at"] is None


async def test_requested_chat_jumps_a_running_pass(api, pool):  # noqa: F811
    await _chats(pool)
    await pool.execute("UPDATE chats SET last_synced_at = now() WHERE chat_id = $1", OTHER)  # CHAT_ID goes first
    gate = asyncio.Event()
    order = _record(api.worker, gate)
    task = asyncio.create_task(api.worker.run_pass())
    while not order:
        await asyncio.sleep(0.01)
    api.worker.request_chat(OTHER)
    gate.set()
    await task
    assert order == [CHAT_ID, OTHER]  # OTHER synced once, not again at its normal turn

    order.clear()
    await pool.execute("UPDATE chats SET last_synced_at = NULL")
    api.worker.request_chat(OTHER)  # queued before a pass starts -> goes first
    await api.worker.run_pass()
    assert order == [OTHER, CHAT_ID]


async def test_run_forever_serves_requests_between_passes(api, pool):  # noqa: F811
    await _chats(pool)
    w = api.worker
    w.s = dataclasses.replace(w.s, sync_interval_seconds=3600)
    order = _record(w)
    task = asyncio.create_task(w.run_forever())
    try:
        while w.state.last_pass_finished_at is None:
            await asyncio.sleep(0.01)
        finished = w.state.last_pass_finished_at
        assert sorted(order) == sorted([CHAT_ID, OTHER])
        order.clear()
        w.request_chat(CHAT_ID)
        for _ in range(200):
            if order:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert order == [CHAT_ID]  # only the requested chat, without waiting an hour
        assert w.state.last_pass_finished_at == finished and w.state.next_pass_at is not None
    finally:
        task.cancel()
