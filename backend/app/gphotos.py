"""Minimal Google Photos Library API client (upload + read back app-created items).

Scopes: photoslibrary.appendonly + photoslibrary.readonly.appcreateddata.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import httpx

log = logging.getLogger(__name__)

API = "https://photoslibrary.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]
BATCH_LIMIT = 50
PHOTO_MAX_BYTES = 200 * 1024 * 1024
VIDEO_MAX_BYTES = 20 * 1024 * 1024 * 1024
# baseUrls expire after ~60 min; keep a little headroom.
URL_TTL_SECONDS = 45 * 60


class GPhotosError(Exception):
    pass


@dataclass
class CreateResult:
    upload_token: str
    media_id: str | None
    error: str | None


class GPhotos:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self._url_cache: dict[str, tuple[float, str]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _token(self) -> str:
        async with self._lock:
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token
            r = await self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if r.status_code != 200:
                raise GPhotosError(f"token refresh failed: {r.status_code} {r.text[:300]}")
            data = r.json()
            self._access_token = data["access_token"]
            self._expires_at = time.time() + int(data.get("expires_in", 3600))
            return self._access_token

    async def _request(self, method: str, url: str, *, headers=None, body_factory=None, **kw) -> httpx.Response:
        """Authorised request with one token refresh on 401 and backoff on 429/5xx.

        Streaming bodies cannot be replayed, so they are passed as a factory.
        """
        attempts = 5
        for attempt in range(attempts):
            h = {**(headers or {}), "Authorization": f"Bearer {await self._token()}"}
            if body_factory is not None:
                kw["content"] = body_factory()
            r = await self._http.request(method, url, headers=h, **kw)
            if r.status_code == 401 and attempt == 0:
                self._access_token = None
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                delay = 2 ** attempt * 2
                log.warning("gphotos %s %s -> %s, retrying in %ss", method, url, r.status_code, delay)
                await asyncio.sleep(delay)
                continue
            return r
        return r

    # ---- upload ---------------------------------------------------------

    async def upload(self, path: Path, mime: str) -> str:
        """Step 1: POST raw bytes, streamed from disk (RAM is scarce). Returns an upload token."""
        size = path.stat().st_size

        async def body() -> AsyncIterator[bytes]:
            with path.open("rb") as f:
                while chunk := await asyncio.to_thread(f.read, 1024 * 1024):
                    yield chunk

        r = await self._request(
            "POST",
            f"{API}/uploads",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
                "X-Goog-Upload-Content-Type": mime,
                "X-Goog-Upload-Protocol": "raw",
            },
            body_factory=body,
            timeout=httpx.Timeout(None, connect=15.0),
        )
        if r.status_code != 200 or not r.text:
            raise GPhotosError(f"upload failed: {r.status_code} {r.text[:300]}")
        return r.text.strip()

    async def batch_create(
        self, items: list[tuple[str, str]], album_id: str | None = None
    ) -> list[CreateResult]:
        """Step 2: turn upload tokens into media items. items = [(upload_token, description)].

        Handles HTTP 207 (partial success): each item gets its own result.
        """
        results: list[CreateResult] = []
        for i in range(0, len(items), BATCH_LIMIT):
            chunk = items[i : i + BATCH_LIMIT]
            body: dict = {
                "newMediaItems": [
                    {"description": desc[:1000], "simpleMediaItem": {"uploadToken": tok}}
                    for tok, desc in chunk
                ]
            }
            if album_id:
                body["albumId"] = album_id
            r = await self._request("POST", f"{API}/mediaItems:batchCreate", json=body)
            if r.status_code not in (200, 207):
                err = f"batchCreate {r.status_code}: {r.text[:300]}"
                results.extend(CreateResult(tok, None, err) for tok, _ in chunk)
                continue
            by_token = {res.get("uploadToken"): res for res in r.json().get("newMediaItemResults", [])}
            for tok, _ in chunk:
                res = by_token.get(tok)
                if res is None:
                    results.append(CreateResult(tok, None, "missing in batchCreate response"))
                    continue
                status = res.get("status") or {}
                item = res.get("mediaItem")
                if item and item.get("id") and not status.get("code"):
                    results.append(CreateResult(tok, item["id"], None))
                else:
                    results.append(CreateResult(tok, None, status.get("message") or "unknown error"))
        return results

    async def create_album(self, title: str) -> str:
        r = await self._request("POST", f"{API}/albums", json={"album": {"title": title[:500]}})
        if r.status_code != 200:
            raise GPhotosError(f"create album failed: {r.status_code} {r.text[:300]}")
        return r.json()["id"]

    # ---- read back ------------------------------------------------------

    async def base_urls(self, media_ids: list[str], force: bool = False) -> dict[str, str]:
        """Fresh baseUrls via mediaItems.batchGet (<= 50 ids per call).

        Short in-memory cache only; baseUrls are never persisted.
        """
        now = time.time()
        out: dict[str, str] = {}
        missing: list[str] = []
        for mid in dict.fromkeys(media_ids):
            hit = self._url_cache.get(mid)
            if hit and not force and hit[0] > now:
                out[mid] = hit[1]
            else:
                missing.append(mid)
        for i in range(0, len(missing), BATCH_LIMIT):
            chunk = missing[i : i + BATCH_LIMIT]
            r = await self._request(
                "GET", f"{API}/mediaItems:batchGet", params=[("mediaItemIds", m) for m in chunk]
            )
            if r.status_code != 200:
                log.warning("batchGet failed: %s %s", r.status_code, r.text[:300])
                continue
            for res in r.json().get("mediaItemResults", []):
                item = res.get("mediaItem")
                if item and item.get("baseUrl"):
                    out[item["id"]] = item["baseUrl"]
                    self._url_cache[item["id"]] = (now + URL_TTL_SECONDS, item["baseUrl"])
        if len(self._url_cache) > 5000:  # crude bound on memory
            self._url_cache = {k: v for k, v in self._url_cache.items() if v[0] > now}
        return out
