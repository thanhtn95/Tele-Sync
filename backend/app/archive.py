"""Archive a website: find its categories, then save every page of the ones picked.

Pages land in MEDIA_DIR/web/job<id>/ (served by nginx under /files/). When a job ends,
links between saved pages are rewritten to the local copies and every other relative
link/image/stylesheet to the live site, so the archive can be browsed page to page.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import ipaddress
import logging
import mimetypes
import re
import shutil
import socket
from collections import Counter, deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import asyncpg
import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_PAGES_LIMIT = 2000
CRAWL_DELAY = 1.0  # seconds between requests: be polite to the site
DISK_RESERVE_BYTES = 1536 * 1024 * 1024  # same reserve as the sync worker
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
USER_AGENT = "Mozilla/5.0 (compatible; Tele-Sync archiver)"
ROBOTS_AGENT = "Tele-Sync"

# Paths like /category/news/ or /tag/python/: the second segment names the category.
CATEGORY_WORDS = {"category", "categories", "cat", "c", "tag", "tags", "topic", "topics", "section",
                  "sections", "collections", "chuyen-muc", "danh-muc", "the"}
# Links to these are files, not pages of a category.
SKIP_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js", ".json", ".xml",
            ".mp4", ".mp3", ".zip", ".woff", ".woff2", ".ttf", ".rss", ".atom"}
NAV_HINT = re.compile(r"nav|menu", re.I)


class ArchiveError(Exception):
    """The page could not be archived; the message is safe to show to the user."""


@dataclass
class Page:
    url: str  # as requested
    final_url: str  # after redirects
    status: int
    content_type: str | None
    body: bytes

    @property
    def is_html(self) -> bool:
        return (self.content_type or "text/html").split(";")[0].strip().lower() in ("text/html", "application/xhtml+xml")


# ---- fetching -------------------------------------------------------------------

async def check_public_host(host: str) -> None:
    """Refuse hosts that resolve to loopback/private addresses: the VM runs Postgres on
    localhost and sits on a tailnet, and a website must not make us reach those."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ArchiveError(f"cannot resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise ArchiveError(f"{host} is not a public address")


def normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ArchiveError("only http(s) websites can be archived")
    if not parts.path:
        url = parts._replace(path="/").geturl()
    return urldefrag(url)[0]


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})


async def fetch_page(client: httpx.AsyncClient, url: str, *, max_bytes: int = MAX_BYTES,
                     check_host=check_public_host) -> Page:
    """GET url, following redirects by hand so every hop's host is checked."""
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ArchiveError("only http(s) websites can be archived")
            await check_host(parts.hostname)
            async with client.stream("GET", current, follow_redirects=False) as r:
                if r.is_redirect and "location" in r.headers:
                    current = urldefrag(urljoin(current, r.headers["location"]))[0]
                    continue
                if r.status_code >= 400:
                    raise ArchiveError(f"HTTP {r.status_code}")
                too_big = ArchiveError(f"larger than {max_bytes // (1024 * 1024)} MB")
                length = r.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise too_big
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise too_big
                    chunks.append(chunk)
                return Page(url, current, r.status_code, r.headers.get("content-type"), b"".join(chunks))
        raise ArchiveError("too many redirects")
    except httpx.HTTPError as e:
        raise ArchiveError(f"download failed: {type(e).__name__}") from e


# ---- parsing --------------------------------------------------------------------

_CHARSET_HDR = re.compile(r"charset=([\w-]+)", re.I)
_CHARSET_META = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.I)


def page_charset(page: Page) -> str:
    m = _CHARSET_HDR.search(page.content_type or "")
    if m:
        cs = m.group(1)
    else:
        m = _CHARSET_META.search(page.body[:4096])
        cs = m.group(1).decode() if m else "utf-8"
    try:
        "".encode(cs)
    except LookupError:
        cs = "utf-8"
    return cs


@dataclass
class Link:
    url: str
    text: str
    in_nav: bool  # inside a menu / header
    in_footer: bool = False


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, list[str], bool]] = []
        self.title: list[str] = []
        self._in_title = False
        self._stack: list[tuple[str, bool, bool]] = []  # open elements: (tag, nav/menu?, footer?)
        self._nav_depth = self._footer_depth = 0
        self._a: tuple[str, list[str], bool, bool] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "a" and a.get("href"):
            self._a = (a["href"], [], self._nav_depth > 0, self._footer_depth > 0)
            return
        if tag in ("br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "col", "embed"):
            return
        is_nav = tag in ("nav", "header") or a.get("role") == "navigation" or bool(
            NAV_HINT.search((a.get("class") or "") + " " + (a.get("id") or "")))
        is_footer = tag == "footer" or "footer" in (a.get("class") or "") + " " + (a.get("id") or "")
        self._stack.append((tag, is_nav, is_footer))
        self._nav_depth += is_nav
        self._footer_depth += is_footer

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "a":
            if self._a:
                self.links.append(self._a)
            self._a = None
            return
        # Close up to the matching tag: <li>/<p> are often left open in real pages.
        if any(t == tag for t, _, _ in self._stack):
            while self._stack:
                t, is_nav, is_footer = self._stack.pop()
                self._nav_depth -= is_nav
                self._footer_depth -= is_footer
                if t == tag:
                    break

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
        if self._a:
            self._a[1].append(data)


def parse_html(page: Page) -> tuple[str | None, list[Link]]:
    """Title and absolute links (fragments dropped) of an HTML page."""
    p = _LinkParser()
    try:
        p.feed(page.body.decode(page_charset(page), errors="replace"))
        p.close()
    except Exception:  # broken markup: keep what was parsed
        pass
    links = []
    for href, text, in_nav, in_footer in p.links:
        href = href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(page.final_url, href))[0]
        if urlsplit(url).scheme in ("http", "https"):
            links.append(Link(url, " ".join(" ".join(text).split())[:120], in_nav, in_footer))
    title = " ".join(" ".join(p.title).split())[:300] or None
    return title, links


def _site(host: str | None) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(a: str, b: str) -> bool:
    return _site(urlsplit(a).hostname) == _site(urlsplit(b).hostname)


def category_prefix(url: str) -> str | None:
    path = urlsplit(url).path
    segs = [s for s in path.split("/") if s]
    if not segs or Path(segs[-1]).suffix.lower() in SKIP_EXT:
        return None
    if segs[0].lower() in CATEGORY_WORDS and len(segs) > 1:
        return f"/{segs[0]}/{segs[1]}/"
    if len(segs) == 1 and "." in segs[0]:
        return None  # a single page such as /about.html, not a section
    return f"/{segs[0]}/"


def in_category(url: str, prefix: str) -> bool:
    path = urlsplit(url).path
    if not path.endswith("/"):
        path += "/"
    return path.startswith(prefix)


def is_article(link: Link) -> bool:
    """A content link (not menu, header or footer; not the home page or a file)."""
    path = urlsplit(link.url).path
    return (not link.in_nav and not link.in_footer and path.strip("/") != ""
            and Path(path).suffix.lower() not in SKIP_EXT)


def find_categories(page: Page) -> tuple[str | None, list[dict]]:
    """Group the home page's same-site links by section (/news/…, /category/sport/…)."""
    title, links = parse_html(page)
    count: Counter[str] = Counter()
    nav: set[str] = set()
    label: dict[str, str] = {}
    start: dict[str, str] = {}
    for link in links:
        if not same_site(link.url, page.final_url) or link.in_footer:
            continue
        prefix = category_prefix(link.url)
        if prefix is None:
            continue
        count[prefix] += 1
        if link.in_nav:
            nav.add(prefix)
        path = urlsplit(link.url).path.rstrip("/") + "/"
        if path == prefix:  # the link to the section itself: best label and start page
            start.setdefault(prefix, link.url)
            if link.text and (prefix not in label or link.in_nav):
                label.setdefault(prefix, link.text)
        start.setdefault(prefix, urljoin(page.final_url, prefix))
    cats = [
        {"prefix": p, "label": label.get(p) or unquote(p.strip("/").split("/")[-1]).replace("-", " ").capitalize(),
         "url": start[p], "links": n, "in_nav": p in nav}
        for p, n in count.items()
    ]
    cats.sort(key=lambda c: (not c["in_nav"], -c["links"], c["prefix"]))
    return title, cats[:150]


# ---- saving -----------------------------------------------------------------------

_UNSAFE = re.compile(r"[^\w.\-]+", re.ASCII)


def file_name(url: str, content_type: str | None) -> str:
    """Stable name for a URL inside a job's folder (same URL → same file)."""
    parts = urlsplit(url)
    mime = (content_type or "").split(";")[0].strip().lower()
    ext = ".html" if mime in ("", "text/html", "application/xhtml+xml") else mimetypes.guess_extension(mime) or ".bin"
    stem = _UNSAFE.sub("_", unquote(parts.path).strip("/"))[:80].strip("._") or "index"
    if stem.lower().endswith(ext):
        stem = stem[: -len(ext)]
    return f"{stem}_{hashlib.sha1(url.encode()).hexdigest()[:8]}{ext}"


_ATTR = re.compile(r"""(\s(?:href|src|action|poster)\s*=\s*)(?:"([^"]*)"|'([^']*)')""", re.I)
_BASE_TAG = re.compile(r"<base\s[^>]*>", re.I)
_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_LAZY = re.compile(r"""\sdata-(?:src|original|lazy-src|lazy)\s*=\s*("[^"]*"|'[^']*')""", re.I)
_LAZY_SET = re.compile(r"""\sdata-(?:srcset|lazy-srcset)\s*=\s*("[^"]*"|'[^']*')""", re.I)
_SRCSET = re.compile(r"""\ssrcset\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_SRC = re.compile(r"""\ssrc\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)


def _unlazy(m: re.Match) -> str:
    """Lazy-loaded images keep the real URL in data-src(set) and need JS, which the archive can't run."""
    tag = m.group(0)
    lazy, lazy_set = _LAZY.search(tag), _LAZY_SET.search(tag)
    if lazy:
        tag = _SRC.sub("", tag)
        tag = tag[:4] + f" src={lazy.group(1)}" + tag[4:]
    if lazy_set:
        tag = _SRCSET.sub("", tag)
        tag = tag[:4] + f" srcset={lazy_set.group(1)}" + tag[4:]
    return tag


def rewrite_links(text: str, page_url: str, local: dict[str, str]) -> str:
    """Links to archived pages → their local file; other relative URLs → absolute live URLs.
    Also drops scripts and un-lazies images so pages render without JavaScript."""
    # Scripts can't run in the archive (served with CSP sandbox) and would only load ads/trackers.
    text = _SCRIPT.sub("", _BASE_TAG.sub("", text))
    text = _IMG.sub(_unlazy, text)

    def sub(m: re.Match) -> str:
        raw = m.group(2) if m.group(2) is not None else m.group(3)
        value = html.unescape(raw).strip()
        if not value or value.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
            return m.group(0)
        absolute = urljoin(page_url, value)
        target, frag = urldefrag(absolute)
        new = local.get(target)
        new = (new + (f"#{frag}" if frag else "")) if new else absolute
        return f'{m.group(1)}"{html.escape(new, quote=True)}"'

    return _ATTR.sub(sub, text)


# ---- jobs -------------------------------------------------------------------------

JOB_COLS = ("id, url, title, categories, max_pages, status, pages_saved, pages_failed, error, "
            "created_at, finished_at")


class WebArchiver:
    """Runs archive jobs in the background, one at a time (the VM is small)."""

    def __init__(self, pool: asyncpg.Pool, media_dir: Path, *, client_factory=make_client,
                 check_host=check_public_host, delay: float = CRAWL_DELAY) -> None:
        self.pool, self.media_dir = pool, media_dir
        self.client_factory, self.check_host, self.delay = client_factory, check_host, delay
        self._lock = asyncio.Lock()
        self._tasks: dict[int, asyncio.Task] = {}

    async def discover(self, url: str) -> dict:
        url = normalize_url(url)
        async with self.client_factory() as client:
            page = await fetch_page(client, url, check_host=self.check_host)
        if not page.is_html:
            raise ArchiveError("that address is not a web page")
        title, cats = find_categories(page)
        return {"url": page.final_url, "title": title, "categories": cats}

    async def start(self, url: str, categories: list[dict], max_pages: int) -> dict:
        url = normalize_url(url)
        cats = []
        for c in categories:
            prefix, start = c["prefix"], normalize_url(c.get("url") or urljoin(url, c["prefix"]))
            if not (prefix.startswith("/") and prefix.endswith("/")) or not same_site(start, url):
                raise ArchiveError(f"category {prefix} is not on {urlsplit(url).hostname}")
            cats.append({"prefix": prefix, "label": c.get("label") or prefix, "url": start})
        if not cats:
            raise ArchiveError("pick at least one category")
        row = await self.pool.fetchrow(
            f"INSERT INTO web_archive_jobs (url, categories, max_pages) VALUES ($1, $2, $3) RETURNING {JOB_COLS}",
            url, cats, max_pages,
        )
        self._tasks[row["id"]] = asyncio.create_task(self._run(row["id"]), name=f"web-archive-{row['id']}")
        return dict(row)

    async def cancel(self, job_id: int) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def delete(self, job_id: int) -> bool:
        await self.cancel(job_id)
        task = self._tasks.pop(job_id, None)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        deleted = await self.pool.fetchval("DELETE FROM web_archive_jobs WHERE id = $1 RETURNING id", job_id)
        await asyncio.to_thread(shutil.rmtree, self.media_dir / "web" / f"job{job_id}", True)
        return deleted is not None

    async def mark_interrupted(self) -> None:
        """Jobs that were running when the app stopped can't be resumed."""
        await self.pool.execute(
            "UPDATE web_archive_jobs SET status = 'interrupted', finished_at = now() "
            "WHERE status IN ('queued', 'running')"
        )

    async def aclose(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def _disk_low(self) -> bool:
        try:
            self.media_dir.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.media_dir).free < DISK_RESERVE_BYTES
        except OSError:
            return False

    async def _run(self, job_id: int) -> None:
        status, error = "done", None
        try:
            async with self._lock:
                await self.pool.execute("UPDATE web_archive_jobs SET status = 'running' WHERE id = $1", job_id)
                await self._crawl(job_id)
        except asyncio.CancelledError:
            status = "cancelled"
        except ArchiveError as e:
            status, error = "failed", str(e)
        except Exception as e:
            log.exception("web archive job %s failed", job_id)
            status, error = "failed", f"{type(e).__name__}: {e}"[:500]
        finally:
            try:
                await asyncio.shield(self._finish(job_id, status, error))
            except Exception:
                log.exception("finishing web archive job %s failed", job_id)

    async def _finish(self, job_id: int, status: str, error: str | None) -> None:
        await self._rewrite(job_id)
        await self.pool.execute(
            "UPDATE web_archive_jobs SET status = $2, error = coalesce($3, error), finished_at = now() WHERE id = $1",
            job_id, status, error,
        )

    async def _crawl(self, job_id: int) -> None:
        job = await self.pool.fetchrow("SELECT url, categories, max_pages FROM web_archive_jobs WHERE id = $1", job_id)
        cats = job["categories"]
        folder = self.media_dir / "web" / f"job{job_id}"
        folder.mkdir(parents=True, exist_ok=True)
        # Per category: articles waiting to be saved, and its listing pages (followed on).
        # Articles go first and categories take turns, so a small max_pages still gets
        # stories from every picked category rather than only sub-section pages.
        articles = {c["prefix"]: deque() for c in cats}
        listings = {c["prefix"]: deque([c["url"]]) for c in cats}
        order = deque(c["prefix"] for c in cats)
        pending: deque[tuple[str, str | None, bool]] = deque([(job["url"], None, True)])  # home page first
        seen = {job["url"], *(c["url"] for c in cats)}
        fetched: set[str] = set()
        saved = 0

        def next_url():
            if pending:
                return pending.popleft()
            for _ in range(len(order)):
                prefix = order[0]
                order.rotate(-1)
                if articles[prefix]:
                    return articles[prefix].popleft(), prefix, False
                if listings[prefix]:
                    return listings[prefix].popleft(), prefix, True
            return None

        async with self.client_factory() as client:
            robots = await self._robots(client, job["url"])
            while saved < job["max_pages"] and (item := next_url()):
                url, prefix, expand = item
                if robots and not robots.can_fetch(ROBOTS_AGENT, url):
                    await self._record(job_id, url, prefix, error="blocked by robots.txt")
                    continue
                if self._disk_low():
                    raise ArchiveError("disk almost full: stopped")
                if saved:
                    await asyncio.sleep(self.delay)
                try:
                    page = await fetch_page(client, url, check_host=self.check_host)
                except ArchiveError as e:
                    await self._record(job_id, url, prefix, error=str(e))
                    continue
                if page.final_url in fetched:  # e.g. /sport redirected to /sport/, already saved
                    continue
                fetched.add(page.final_url)
                title = None
                if page.is_html:
                    title, links = parse_html(page)
                    for link in links if expand else ():
                        if link.url in seen or not same_site(link.url, job["url"]):
                            continue
                        hit = next((c["prefix"] for c in cats if in_category(link.url, c["prefix"])), None)
                        if hit and category_prefix(link.url) is not None:
                            # more of a picked category (sub-pages, page 2…): follow its links too
                            seen.add(link.url)
                            listings[hit].append(link.url)
                        elif prefix and is_article(link):
                            # listed on a category page but stored elsewhere (news sites often keep
                            # articles at /story-123.html): save it, don't crawl on from it
                            seen.add(link.url)
                            articles[prefix].append(link.url)
                name = file_name(page.final_url, page.content_type)
                await asyncio.to_thread((folder / name).write_bytes, page.body)
                await self._record(job_id, url, prefix, page=page, title=title,
                                   file_path=f"web/job{job_id}/{name}")
                if prefix is None and title:
                    await self.pool.execute("UPDATE web_archive_jobs SET title = $2 WHERE id = $1", job_id, title)
                saved += 1

    async def _robots(self, client: httpx.AsyncClient, url: str) -> RobotFileParser | None:
        try:
            page = await fetch_page(client, urljoin(url, "/robots.txt"), max_bytes=512 * 1024,
                                    check_host=self.check_host)
        except ArchiveError:
            return None  # no robots.txt: everything allowed
        rp = RobotFileParser()
        rp.parse(page.body.decode("utf-8", errors="replace").splitlines())
        return rp

    async def _record(self, job_id: int, url: str, prefix: str | None, *, page: Page | None = None,
                      title: str | None = None, file_path: str | None = None, error: str | None = None) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO web_archive_pages (job_id, url, final_url, category, status, content_type,
                                               title, size, file_path, error)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                job_id, url, page.final_url if page else url, prefix, page.status if page else None,
                page.content_type if page else None, title, len(page.body) if page else None, file_path, error,
            )
            col = "pages_failed" if error else "pages_saved"
            await conn.execute(f"UPDATE web_archive_jobs SET {col} = {col} + 1 WHERE id = $1", job_id)

    async def _rewrite(self, job_id: int) -> None:
        rows = await self.pool.fetch(
            "SELECT url, final_url, content_type, file_path FROM web_archive_pages "
            "WHERE job_id = $1 AND file_path IS NOT NULL", job_id,
        )
        local: dict[str, str] = {}
        for r in rows:
            name = r["file_path"].rsplit("/", 1)[-1]
            local[r["url"]] = local[r["final_url"]] = name  # same folder: a bare file name works
        for r in rows:
            page = Page(r["url"], r["final_url"], 200, r["content_type"], b"")
            if not page.is_html:
                continue
            path = self.media_dir / r["file_path"]

            def work(path=path, page=page):
                try:
                    body = path.read_bytes()
                except FileNotFoundError:
                    return
                cs = page_charset(Page(page.url, page.final_url, 200, page.content_type, body))
                text = rewrite_links(body.decode(cs, errors="replace"), page.final_url, local)
                path.write_bytes(text.encode(cs, errors="xmlcharrefreplace"))

            await asyncio.to_thread(work)
