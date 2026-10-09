"""Save a snapshot of a web page to MEDIA_DIR/web/ (served by nginx under /files/)."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import ipaddress
import mimetypes
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
USER_AGENT = "Mozilla/5.0 (compatible; Tele-Sync archiver)"


class ArchiveError(Exception):
    """The page could not be archived; the message is safe to show to the user."""


@dataclass
class Archived:
    url: str  # as requested
    final_url: str  # after redirects
    status: int
    content_type: str | None
    title: str | None
    size: int
    rel_path: str  # relative to MEDIA_DIR
    archived_at: dt.datetime


async def check_public_host(host: str) -> None:
    """Refuse hosts that resolve to loopback/private addresses: the VM runs Postgres on
    localhost and sits on a tailnet, and a link must not reach those."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ArchiveError(f"cannot resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ArchiveError(f"{host} is not a public address")


def _validate(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ArchiveError("only http(s) URLs can be archived")
    return parts.hostname


_UNSAFE = re.compile(r"[^\w.\-]+", re.ASCII)
_TITLE = re.compile(rb"<title[^>]*>(.*?)</title", re.I | re.S)
_HEAD = re.compile(rb"<head(\s[^>]*)?>", re.I)
_BASE = re.compile(rb"<base\s", re.I)
_CHARSET = re.compile(r"charset=([\w-]+)", re.I)


def _file_name(url: str, host: str, content_type: str | None, now: dt.datetime) -> str:
    mime = (content_type or "").split(";")[0].strip().lower()
    ext = ".html" if mime in ("", "text/html") else mimetypes.guess_extension(mime) or ".bin"
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"web/{now:%Y%m%d-%H%M%S}_{_UNSAFE.sub('_', host)[:60]}_{digest}{ext}"


def _title(body: bytes, content_type: str | None) -> str | None:
    m = _TITLE.search(body[:200_000])
    if not m:
        return None
    cs = _CHARSET.search(content_type or "")
    text = m.group(1).decode(cs.group(1) if cs else "utf-8", errors="replace")
    return " ".join(html.unescape(text).split())[:300] or None


def _with_base(body: bytes, final_url: str) -> bytes:
    """Point relative links/images/CSS at the live site so the snapshot still renders."""
    if _BASE.search(body[:200_000]):
        return body
    tag = f'<base href="{html.escape(final_url, quote=True)}">'.encode()
    m = _HEAD.search(body[:200_000])
    if m:
        return body[: m.end()] + tag + body[m.end():]
    return tag + body


async def archive_website(
    url: str,
    media_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
    max_bytes: int = MAX_BYTES,
    check_host=check_public_host,
) -> Archived:
    """Download `url` (following up to MAX_REDIRECTS redirects, each host checked) and
    save it under media_dir/web/. HTML pages get a <base> tag so relative assets load."""
    url = url.strip()
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = _validate(current)
            await check_host(host)
            async with client.stream("GET", current, follow_redirects=False) as r:
                if r.is_redirect and "location" in r.headers:
                    current = urljoin(current, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise ArchiveError(f"{current} answered HTTP {r.status_code}")
                length = r.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise ArchiveError(f"page is larger than {max_bytes // (1024 * 1024)} MB")
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ArchiveError(f"page is larger than {max_bytes // (1024 * 1024)} MB")
                    chunks.append(chunk)
                body = b"".join(chunks)
                status, content_type = r.status_code, r.headers.get("content-type")
                break
        else:
            raise ArchiveError("too many redirects")
    except httpx.HTTPError as e:
        raise ArchiveError(f"download failed: {type(e).__name__}") from e
    finally:
        if own_client:
            await client.aclose()

    now = dt.datetime.now(dt.timezone.utc)
    is_html = (content_type or "text/html").split(";")[0].strip().lower() == "text/html"
    title = _title(body, content_type) if is_html else None
    if is_html:
        body = _with_base(body, current)
    rel = _file_name(url, urlsplit(current).hostname or "site", content_type, now)
    dest = media_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(dest.write_bytes, body)
    return Archived(url=url, final_url=current, status=status, content_type=content_type,
                    title=title, size=len(body), rel_path=rel, archived_at=now)
