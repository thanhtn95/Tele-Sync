"""Re-queue media whose Google Photos item isn't in the currently linked account.

Use after switching GOOGLE_REFRESH_TOKEN to another Google account: items uploaded
to the old account can't be shown any more. This asks Google (mediaItems.batchGet)
about every stored id and clears only the ones it reports as unavailable, so the
next sync re-downloads them from Telegram and uploads them to the linked account.
Items that ARE in the linked account are left alone (no duplicates).

    sudo telesync gphotos-recheck            (then: sudo telesync restart)
    sudo telesync gphotos-recheck --dry-run  (only count)
"""
import asyncio
import sys

from app import db
from app.config import settings
from app.gphotos import API, BATCH_LIMIT, GPhotos


async def missing_ids(gp: GPhotos, ids: list[str]) -> list[str]:
    """Ids Google explicitly reports as unavailable. Aborts on any failed call, so an
    auth/network problem can never be mistaken for 'everything is missing'."""
    missing: list[str] = []
    for i in range(0, len(ids), BATCH_LIMIT):
        chunk = ids[i : i + BATCH_LIMIT]
        r = await gp._request("GET", f"{API}/mediaItems:batchGet", params=[("mediaItemIds", m) for m in chunk])
        if r.status_code != 200:
            raise SystemExit(f"Google Photos check failed ({r.status_code}): {r.text[:300]}\nNothing was changed.")
        results = r.json().get("mediaItemResults", [])
        if len(results) != len(chunk):
            raise SystemExit("Unexpected batchGet response; nothing was changed.")
        # Results come back in request order.
        for mid, res in zip(chunk, results):
            if not res.get("mediaItem") and res.get("status"):
                missing.append(mid)
        print(f"  checked {min(i + BATCH_LIMIT, len(ids))}/{len(ids)}", end="\r")
    print()
    return missing


async def stale_albums(gp: GPhotos, albums: list[tuple[int, str]]) -> list[int]:
    stale = []
    for chat_id, album_id in albums:
        r = await gp._request("GET", f"{API}/albums/{album_id}")
        if r.status_code == 200:
            continue
        if r.status_code in (400, 403, 404):
            stale.append(chat_id)
        else:
            raise SystemExit(f"Album check failed ({r.status_code}): {r.text[:300]}")
    return stale


async def main() -> None:
    if not settings.gphotos_enabled:
        raise SystemExit("Google Photos is not configured (.env)")
    dry = "--dry-run" in sys.argv
    pool = await db.create_pool(settings.database_url)
    gp = GPhotos(settings.google_client_id, settings.google_client_secret, settings.google_refresh_token)
    try:
        ids = [r["gphotos_media_id"] for r in await pool.fetch(
            "SELECT gphotos_media_id FROM media WHERE gphotos_media_id IS NOT NULL ORDER BY chat_id, message_id")]
        print(f"Checking {len(ids)} stored Google Photos items against the linked account…")
        missing = await missing_ids(gp, ids)
        print(f"{len(ids) - len(missing)} found, {len(missing)} not in the linked account.")
        if missing and not dry:
            await pool.execute(
                "UPDATE media SET gphotos_media_id = NULL, error = NULL WHERE gphotos_media_id = ANY($1::text[])",
                missing,
            )
            print(f"Re-queued {len(missing)} items; they upload on the next sync.")
        # Albums from another account would reject uploads; a fresh one is made when needed.
        stale = await stale_albums(gp, [(r["chat_id"], r["gphotos_album_id"]) for r in await pool.fetch(
            "SELECT chat_id, gphotos_album_id FROM chats WHERE gphotos_album_id IS NOT NULL")])
        if stale:
            print(f"{len(stale)} chat album(s) are not in the linked account" + ("" if dry else "; cleared."))
            if not dry:
                await pool.execute("UPDATE chats SET gphotos_album_id = NULL WHERE chat_id = ANY($1::bigint[])", stale)
    finally:
        await gp.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
