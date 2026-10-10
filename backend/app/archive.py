"""Archive a website: read its navbar, then save the posts listed under the menu items picked.

Pages land in MEDIA_DIR/web/job<id>/ (served by nginx under /files/web/), with the
stylesheets, fonts and images they use in job<id>/assets/, so a saved page looks like the
original even after the site changes. Scripts are dropped (the archive never runs them).
When a job ends, links between saved pages are pointed at the local copies.
"""
from __future__ import annotations

import asyncio
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
ASSET_MAX_BYTES = 15 * 1024 * 1024
ASSET_CONCURRENCY = 6
MAX_ASSETS_PER_PAGE = 400
CSS_DEPTH = 3  # @import chains followed this deep
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
_FOOTER_HINT = re.compile(r"footer", re.I)
_ASIDE_HINT = re.compile(r"sidebar", re.I)
# Category pages often end in .htm(l)/.php (Dân Trí: /xa-hoi.htm, articles under /xa-hoi/…).
PAGE_EXT = {".htm", ".html", ".php", ".asp", ".aspx", ".shtml"}

# What may be stored as an asset, by Content-Type (anything else, e.g. an HTML error page, is not).
ASSET_TYPES = {
    "text/css": ".css", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif", "image/svg+xml": ".svg",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico", "image/bmp": ".bmp",
    "font/woff2": ".woff2", "font/woff": ".woff", "font/ttf": ".ttf", "font/otf": ".otf",
    "font/sfnt": ".ttf", "application/font-woff2": ".woff2", "application/font-woff": ".woff",
    "application/x-font-woff": ".woff", "application/x-font-ttf": ".ttf", "application/x-font-otf": ".otf",
    "application/font-sfnt": ".ttf", "application/vnd.ms-fontobject": ".eot",
}
ASSET_EXTS = set(ASSET_TYPES.values()) | {".jpeg"}
FONT_EXTS = {".woff2", ".woff", ".ttf", ".otf", ".eot"}


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
    in_heading: bool = False  # inside <h1>-<h4>: how listing pages link their posts
    in_article: bool = False  # inside <article>
    in_aside: bool = False  # sidebar (<aside>, *sidebar*): "most read" boxes and the like
    in_header: bool = False  # <header>: the site's header, or a post card's (inside <article>)
    image: str | None = None  # the picture inside the link (a list page's post thumbnail)


_HEADINGS = {"h1", "h2", "h3", "h4"}
_VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "col", "embed"}


# A class/id hint (nav, menu, sidebar, footer) only marks an element as page chrome when it
# holds at most this share of the page's links: wrappers like <body class="site-navigation">
# or <div class="articles-and-sidebar"> hold nearly all of them and are not chrome.
HINT_MAX_SHARE = 0.5


def _img_src(a: dict) -> str | None:
    """The real picture of an <img>/<source>: lazy data-src first, then src, then a srcset candidate."""
    for key in ("data-src", "data-original", "data-lazy-src", "data-lazy", "src"):
        v = (a.get(key) or "").strip()
        if _is_fetchable(v):
            return v
    for key in ("data-srcset", "srcset"):
        best = _srcset_best(a.get(key) or "")
        if _is_fetchable(best):
            return best
    return None


class _LinkParser(HTMLParser):
    """Title, links (with the context they sit in) and <link rel=next> of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[list] = []  # [href, text parts, contexts, hint ids, image]
        self.title: list[str] = []
        self.next: str | None = None
        self._in_title = False
        # open elements: (tag, contexts opened by its tag, index of its class/id hint or None)
        self._stack: list[tuple[str, dict, int | None]] = []
        self._depth = {"nav": 0, "header": 0, "footer": 0, "heading": 0, "article": 0, "aside": 0}
        self.hints: list[dict] = []  # {"kinds": set, "links": n}
        self._open_hints: list[int] = []
        self._a: list | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "link" and "next" in a.get("rel", "").lower().split() and a.get("href"):
            self.next = self.next or a["href"]
        if tag == "a" and a.get("href"):
            for i in self._open_hints:
                self.hints[i]["links"] += 1
            self._a = [a["href"], [], {k: v > 0 for k, v in self._depth.items()}, tuple(self._open_hints), None]
            return
        if self._a is not None and self._a[4] is None and tag in ("img", "source"):
            self._a[4] = _img_src(a)
        if tag in _VOID:
            return
        opened = {
            "nav": tag == "nav" or a.get("role") == "navigation",
            "header": tag == "header",
            "footer": tag == "footer",
            "heading": tag in _HEADINGS,
            "article": tag == "article",
            "aside": tag == "aside",
        }
        hint = f"{a.get('class', '')} {a.get('id', '')}".lower()
        kinds = {k for k, words in (("nav", NAV_HINT), ("footer", _FOOTER_HINT), ("aside", _ASIDE_HINT))
                 if words.search(hint)} if tag not in ("html", "body", "main", "article") else set()
        index = None
        if kinds:
            self.hints.append({"kinds": kinds, "links": 0})
            index = len(self.hints) - 1
            self._open_hints.append(index)
        self._stack.append((tag, opened, index))
        for k, v in opened.items():
            self._depth[k] += v

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
                t, opened, index = self._stack.pop()
                for k, v in opened.items():
                    self._depth[k] -= v
                if index is not None and index in self._open_hints:
                    self._open_hints.remove(index)
                if t == tag:
                    break

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
        if self._a:
            self._a[1].append(data)

    def contexts(self, ctx: dict, hint_ids: tuple[int, ...]) -> dict:
        """A link's contexts, with class/id hints of page-wide wrappers left out."""
        limit = HINT_MAX_SHARE * max(len(self.links), 1)
        ctx = dict(ctx)
        for i in hint_ids:
            if self.hints[i]["links"] <= limit:
                for k in self.hints[i]["kinds"]:
                    ctx[k] = True
        return ctx


def _parse(page: Page) -> _LinkParser:
    p = _LinkParser()
    try:
        p.feed(page.body.decode(page_charset(page), errors="replace"))
        p.close()
    except Exception:  # broken markup: keep what was parsed
        pass
    return p


def parse_html(page: Page) -> tuple[str | None, list[Link]]:
    """Title and absolute links (fragments dropped) of an HTML page."""
    p = _parse(page)
    links = []
    for href, text, raw_ctx, hint_ids, image in p.links:
        ctx = p.contexts(raw_ctx, hint_ids)
        href = href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(page.final_url, href))[0]
        if urlsplit(url).scheme in ("http", "https"):
            links.append(Link(url, " ".join(" ".join(text).split())[:120], ctx["nav"], ctx["footer"],
                              ctx["heading"], ctx["article"], ctx["aside"], ctx["header"],
                              urljoin(page.final_url, image) if image else None))
    title = " ".join(" ".join(p.title).split())[:300] or None
    return title, links


class _NavParser(HTMLParser):
    """Links inside the page's menus (<nav>, role=navigation, or class/id *nav*/*menu*),
    with how deep each sits in nested lists (sub-menus) and which menu it belongs to."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.menus: list[dict] = []  # {name, strong, items: [(href, text, depth)]}
        self._stack: list[tuple[str, int | None, bool, bool]] = []  # (tag, menu opened here, list?, footer?)
        self._menu: int | None = None
        self._lists = 0  # open <ul>/<ol> inside the current menu
        self._footer = 0
        self._a: list | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if self._a is not None:
            if tag == "img" and a.get("alt"):
                self._a[1].append(a["alt"])
            return
        if tag == "a":
            if self._menu is not None and not self._footer and a.get("href"):
                label = a.get("title") or a.get("aria-label") or ""
                self._a = [a["href"], [], max(self._lists - 1, 0), label]
            return
        if tag in ("br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "col", "embed"):
            return
        hint = f"{a.get('class', '')} {a.get('id', '')}"
        opened = None
        if self._menu is None and not self._footer and (
                tag == "nav" or a.get("role") == "navigation" or NAV_HINT.search(hint)):
            name = a.get("aria-label") or ""
            strong = tag == "nav" or a.get("role") == "navigation"
            self.menus.append({"name": name, "strong": strong, "items": []})
            self._menu = opened = len(self.menus) - 1
            self._lists = 0
        is_list = self._menu is not None and tag in ("ul", "ol")
        self._lists += is_list
        footer = tag == "footer" or "footer" in hint
        self._footer += footer
        self._stack.append((tag, opened, is_list, footer))

    def handle_endtag(self, tag):
        if self._a is not None and tag != "a":
            return  # inside a link: its own <span>/<div> were never pushed
        if tag == "a":
            if self._a is not None and self._menu is not None:
                href, text, depth, label = self._a
                text = " ".join(" ".join(text).split()) or " ".join(label.split())
                self.menus[self._menu]["items"].append((href, text[:120], depth))
            self._a = None
            return
        if any(t[0] == tag for t in self._stack):  # close up to the matching tag (<li> left open…)
            while self._stack:
                t, opened, is_list, footer = self._stack.pop()
                self._lists -= is_list
                self._footer -= footer
                if opened is not None:
                    self._menu, self._lists = None, 0
                if t == tag:
                    break

    def handle_data(self, data):
        if self._a is not None:
            self._a[1].append(data)


def section_prefix(url: str) -> str | None:
    """The path a menu item's pages live under: /the-thao → /the-thao/, /xa-hoi.htm → /xa-hoi/."""
    path = urlsplit(url).path
    ext = Path(path).suffix.lower()
    if ext in SKIP_EXT:
        return None
    if ext in PAGE_EXT:
        path = path[: -len(ext)]
    path = "/" + path.strip("/")
    return None if path == "/" else path + "/"


def read_navbar(page: Page) -> list[dict]:
    """The site's menus as shown on the page: [{name, items: [{label, url, prefix, depth}]}].
    Real <nav>/role=navigation menus win over class-name guesses; each URL is listed once."""
    p = _NavParser()
    try:
        p.feed(page.body.decode(page_charset(page), errors="replace"))
        p.close()
    except Exception:
        pass
    menus = [m for m in p.menus if m["items"]]
    if any(m["strong"] for m in menus):
        menus = [m for m in menus if m["strong"]]
    seen: set[str] = set()
    out = []
    for m in menus:
        items = []
        for href, text, depth in m["items"]:
            href = href.strip()
            if not text or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            url = urldefrag(urljoin(page.final_url, href))[0]
            prefix = section_prefix(url)
            if prefix is None or not same_site(url, page.final_url) or url in seen:
                continue
            seen.add(url)
            items.append({"label": text, "url": url, "prefix": prefix, "depth": depth})
        if items:
            # Depths relative to the menu's top level, without gaps (a child is parent + 1).
            base = min(i["depth"] for i in items)
            prev = -1
            for i in items:
                i["depth"] = min(i["depth"] - base, prev + 1)
                prev = i["depth"]
            out.append({"name": _menu_name(m["name"], len(out)), "items": items[:300]})
    return out


def _menu_name(raw: str, index: int) -> str:
    raw = " ".join(re.sub(r"[-_]+", " ", raw).split())
    return raw[:60] if raw else ("Main menu" if index == 0 else f"Menu {index + 1}")


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


_PAGINATION = re.compile(
    r"(?:[?&](?:page|paged|p|pg|trang)=(\d+))"            # ?page=2
    r"|(?:/(?:page|trang|p)[/-]?(\d+)/?(?:\.\w+)?$)"     # /page/2/, /trang-2.htm, /p2
    r"|(?:-(?:p|page|trang)-?(\d+)(?:\.\w+)?$)",           # /du-lich-p2
    re.I,
)
_POST_PATH = re.compile(r"/\d{4}/\d{1,2}/")  # /2026/10/09/slug
_LONG_NUMBER = re.compile(r"\d{5,}")         # -5124736.html, -20261010075618440.htm
MAX_LISTING_PAGES = 100  # next pages followed per menu item at most
TITLE_WORDS = 6  # a link whose text is this long reads like a post title
IN_PREFIX_SHARE = 0.6  # when most of a listing's posts live under its path, keep only those


def _listing_key(url: str) -> str:
    """A listing's identity without pagination or page extension: /du-lich-p2 → /du-lich."""
    parts = urlsplit(url)
    path = parts.path
    m = _PAGINATION.search(path)
    if m:
        path = path[: m.start()]
    if Path(path).suffix.lower() in PAGE_EXT:
        path = path[: -len(Path(path).suffix)]
    return f"{_site(parts.hostname)}{path.rstrip('/')}"


def is_pagination(url: str, listing_url: str) -> bool:
    """Another page (2, 3…) of the same listing."""
    parts = urlsplit(url)
    return bool(_PAGINATION.search(parts.path + ("?" + parts.query if parts.query else ""))) and (
        _listing_key(url) == _listing_key(listing_url))


# Links in post cards that aren't posts: author, tag and topic pages, search, accounts.
NOT_POST_SEGMENTS = {"author", "authors", "tag", "tags", "category", "categories", "topic", "topics", "user",
                     "users", "profile", "search", "login", "register", "subscribe", "tac-gia", "tim-kiem"}


def looks_like_post(url: str) -> bool:
    """Post-style address: a date path, a long id, or a long slug."""
    path = urlsplit(url).path
    last = Path(path.rstrip("/")).stem
    return bool(_POST_PATH.search(path) or _LONG_NUMBER.search(last) or last.count("-") >= 3)


def listing_links(page: Page, links: list[Link], prefix: str, listings: set[str]) -> tuple[list[str], list[str]]:
    """(posts, next pages) linked from a listing page. Posts are content links (not menus,
    header, footer or sidebar) that sit in a headline or <article>, have a post-style address,
    or read like a title."""
    me = _listing_key(page.final_url)
    posts, pages = [], []
    for link in links:
        url, path = link.url, urlsplit(link.url).path
        if not same_site(url, page.final_url) or Path(path).suffix.lower() in SKIP_EXT or not path.strip("/"):
            continue
        if is_pagination(url, page.final_url):
            pages.append(url)
            continue
        # menus and sidebars; the site's header/footer (a post card's own header/footer is fine)
        if link.in_nav or link.in_aside or ((link.in_header or link.in_footer) and not link.in_article):
            continue
        key = _listing_key(url)
        if key == me or key in listings:
            continue  # the listing itself, or another picked menu item
        if {seg.lower() for seg in path.split("/")} & NOT_POST_SEGMENTS:
            continue
        if link.in_heading or link.in_article or looks_like_post(url) or len(link.text.split()) >= TITLE_WORDS:
            posts.append(url)
    posts = list(dict.fromkeys(posts))
    inside = [u for u in posts if in_category(u, prefix)]
    if len(inside) >= 3 and len(inside) >= IN_PREFIX_SHARE * len(posts):
        posts = inside  # e.g. Dân Trí: /the-gioi/…htm posts, plus a few from elsewhere
    return posts, list(dict.fromkeys(pages))


def post_thumbnails(links: list[Link]) -> dict[str, str]:
    """Post URL → the thumbnail a list page shows for it (the picture inside a link to it)."""
    thumbs: dict[str, str] = {}
    for link in links:
        if link.image and link.url not in thumbs and _is_fetchable(link.image):
            thumbs[link.url] = link.image
    return thumbs


_SHARE_IMAGE = re.compile(r"""<meta\b[^>]*(?:property|name)\s*=\s*["'](?:og:image|twitter:image)(?::src)?["'][^>]*>""", re.I)


def share_image(page: Page) -> str | None:
    """The page's own share picture (og:image / twitter:image), used when a list had no thumbnail."""
    text = page.body[:300_000].decode(page_charset(page), errors="replace")
    for m in _SHARE_IMAGE.finditer(text):
        v = _attr_value(_attr(m.group(0), "content"))
        if _is_fetchable(v):
            return urljoin(page.final_url, v)
    return None


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
        link.in_nav = link.in_nav or link.in_header
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
    path = unquote(parts.path).strip("/")
    if Path(path).suffix.lower() in PAGE_EXT:
        path = path[: -len(Path(path).suffix)]
    stem = _UNSAFE.sub("_", path)[:80].strip("._") or "index"
    if stem.lower().endswith(ext):
        stem = stem[: -len(ext)]
    return f"{stem}_{hashlib.sha1(url.encode()).hexdigest()[:8]}{ext}"


_ATTR = re.compile(r"""(\s(?:href|src|action|poster|data)\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I)
_BASE_TAG = re.compile(r"<base\s[^>]*>", re.I)
_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
_META_DROP = re.compile(r"""<meta\b[^>]*(?:charset\s*=|http-equiv\s*=\s*["']?(?:content-type|refresh|content-security-policy))[^>]*>""", re.I)
_HEAD = re.compile(r"<head\b[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[a-zA-Z][^<>]*>")
_ON_ATTR = re.compile(r"""\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_TAG = re.compile(r"<(img|source|link|video|body|table|td|th|input)\b[^>]*>", re.I)
_LAZY = re.compile(r"""\sdata-(?:src|original|lazy-src|lazy)\s*=\s*("[^"]*"|'[^']*')""", re.I)
_LAZY_SET = re.compile(r"""\sdata-(?:srcset|lazy-srcset)\s*=\s*("[^"]*"|'[^']*')""", re.I)
_SRCSET = re.compile(r"""\ssrcset\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_SRC = re.compile(r"""\ssrc\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_REL = re.compile(r"""\srel\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_AS_STYLE = re.compile(r"""\sas\s*=\s*["']?style\b""", re.I)
_STYLE_BLOCK = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.I | re.S)
_STYLE_ATTR = re.compile(r"""(\sstyle\s*=\s*)(?:"([^"]*)"|'([^']*)')""", re.I)
_CSS_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"\s]*))\s*\)""", re.I)
_CSS_IMPORT = re.compile(r"""@import\s+(?:"([^"]*)"|'([^']*)')""", re.I)
_SRCSET_ITEM = re.compile(r"\s*(\S+?)(?:\s+([\d.]+)([wx]))?\s*(?:,|$)")
_ATTR_VALUE = r"""\s{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))"""
ASSETS_DIR = "assets"


_CLASS = re.compile(r"""(\sclass\s*=\s*)(?:"([^"]*)"|'([^']*)')""", re.I)
_LOADED = {"lazyload": "lazyloaded", "lazyloading": "lazyloaded"}  # lazysizes' finished state


def _loaded_classes(m: re.Match) -> str:
    """Placeholder styles (blur, lazysizes' hidden state) are lifted by JS once an image loads."""
    classes = (m.group(2) if m.group(2) is not None else m.group(3)).split()
    kept = [_LOADED.get(c, c) for c in classes if "blur" not in c.lower()]
    return f'{m.group(1)}"{" ".join(kept)}"'


def _unlazy(m: re.Match) -> str:
    """Lazy-loaded images keep the real URL in data-src(set) and need JS, which the archive can't run."""
    tag = m.group(0)
    name_end = len(m.group(1)) + 1
    lazy, lazy_set = _LAZY.search(tag), _LAZY_SET.search(tag)
    if lazy and m.group(1).lower() == "img":
        tag = _SRC.sub("", tag)
        tag = tag[:name_end] + f" src={lazy.group(1)}" + tag[name_end:]
    if lazy or lazy_set:
        tag = _CLASS.sub(_loaded_classes, tag)
    if lazy_set:
        tag = _SRCSET.sub("", tag)
        tag = tag[:name_end] + f" srcset={lazy_set.group(1)}" + tag[name_end:]
    if m.group(1).lower() == "link":
        rel = _REL.search(tag)
        rels = (rel and (rel.group(1) or rel.group(2) or rel.group(3)) or "").lower().split()
        if "preload" in rels and _AS_STYLE.search(tag):
            # <link rel=preload as=style onload="this.rel='stylesheet'">: the switch needs JS
            tag = tag[: rel.start()] + ' rel="stylesheet"' + tag[rel.end():]
        elif {"preload", "modulepreload", "prefetch", "preconnect", "dns-prefetch", "prerender"} & set(rels):
            return ""  # hints for scripts/requests the archive never makes
    return tag


def _attr(tag: str, name: str) -> re.Match | None:
    return re.search(_ATTR_VALUE.format(name=name), tag, re.I)


def _attr_value(m: re.Match | None) -> str | None:
    if m is None:
        return None
    return html.unescape(next(g for g in m.groups() if g is not None)).strip()


SRCSET_MAX_W = 1000  # widest image taken from a srcset: enough for a page, not a 4K original


def _srcset_best(value: str) -> str | None:
    """One candidate of a srcset: the widest up to SRCSET_MAX_W (or 1.5x), else the smallest."""
    cands = []
    for m in _SRCSET_ITEM.finditer(value):
        if m.group(1):
            size = float(m.group(2)) if m.group(2) else 1.0
            limit = SRCSET_MAX_W if m.group(3) == "w" else 1.5
            cands.append((size <= limit, size if size <= limit else -size, m.group(1)))
    return max(cands)[2] if cands else None


def _is_fetchable(value: str | None) -> bool:
    return bool(value) and not value.startswith(("data:", "#", "javascript:", "mailto:", "tel:", "about:"))


def _css_refs(css: str, base: str) -> list[tuple[str, str]]:
    """(absolute url, kind) of everything a stylesheet loads: @import → css, url() → file."""
    refs = []
    for m in _CSS_IMPORT.finditer(css):
        v = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if _is_fetchable(v):
            refs.append((urljoin(base, v), "css"))
    for m in _CSS_URL.finditer(css):
        v = next(g for g in m.groups() if g is not None).strip()
        if _is_fetchable(v):
            kind = "css" if css[max(0, m.start() - 12): m.start()].lower().rstrip().endswith("@import") else "file"
            refs.append((urljoin(base, v), kind))
    return refs


def _css_localize(css: str, base: str, local: dict[str, str], prefix: str) -> str:
    """Point a stylesheet's url()/@import at saved copies (prefix + name); others made absolute."""
    def target(v: str) -> str:
        if not _is_fetchable(v):
            return v
        absolute = urljoin(base, v)
        name = local.get(urldefrag(absolute)[0])
        return prefix + name if name else absolute

    css = _CSS_IMPORT.sub(lambda m: f'@import "{target((m.group(1) if m.group(1) is not None else m.group(2)).strip())}"', css)
    return _CSS_URL.sub(lambda m: f'url("{target(next(g for g in m.groups() if g is not None).strip())}")', css)


def prepare_html(text: str, page_url: str) -> str:
    """Make a fetched page work without JavaScript and without the live site's relative paths:
    drop scripts, event handlers, <base>, refresh/CSP/charset metas and script/prefetch hints;
    un-lazy images; resolve every href/src."""
    text = _SCRIPT.sub("", _BASE_TAG.sub("", text))
    text = _META_DROP.sub("", text)
    text = _TAG.sub(_unlazy, text)
    text = _ANY_TAG.sub(lambda m: _ON_ATTR.sub("", m.group(0)), text)  # onclick=… can't run either

    def absolute(m: re.Match) -> str:
        raw = next(g for g in m.groups()[1:] if g is not None)
        value = html.unescape(raw).strip()
        if not _is_fetchable(value):
            return m.group(0)
        return f'{m.group(1)}"{html.escape(urljoin(page_url, value), quote=True)}"'

    text = _ATTR.sub(absolute, text)
    m = _HEAD.search(text)
    meta = '<meta charset="utf-8">'
    return text[: m.end()] + meta + text[m.end():] if m else meta + text


def html_assets(text: str, page_url: str, images: bool = True) -> list[tuple[str, str]]:
    """(url, kind) of the stylesheets, icons and (optionally) images a prepared page uses."""
    refs: list[tuple[str, str]] = []
    for m in _TAG.finditer(text):
        tag, name = m.group(0), m.group(1).lower()
        if name == "link":
            rel = (_attr_value(_attr(tag, "rel")) or "").lower().split()
            href = _attr_value(_attr(tag, "href"))
            if "stylesheet" in rel and _is_fetchable(href):
                refs.append((urljoin(page_url, href), "css"))
            elif images and "icon" in " ".join(rel) and _is_fetchable(href):
                refs.append((urljoin(page_url, href), "file"))
            continue
        if not images:
            continue
        # src only on <img>: <source>/<video> src would be whole videos
        for attr in {"img": ("src",), "video": ("poster",)}.get(name, ("background",)):
            v = _attr_value(_attr(tag, attr))
            if _is_fetchable(v) and name != "input":
                refs.append((urljoin(page_url, v), "file"))
        if name == "img" and _is_fetchable(_attr_value(_attr(tag, "src"))):
            continue  # the plain src is enough; its srcset is pointed at the same copy
        best = _srcset_best(_attr_value(_attr(tag, "srcset")) or "")
        if _is_fetchable(best):
            refs.append((urljoin(page_url, best), "file"))
    css_parts = [m.group(2) for m in _STYLE_BLOCK.finditer(text)]
    css_parts += [html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
                  for m in _STYLE_ATTR.finditer(text) if "url(" in (m.group(2) or m.group(3) or "")]
    for css in css_parts:
        refs += [r for r in _css_refs(css, page_url) if images or r[1] == "css"]
    out, seen = [], set()
    for url, kind in refs:
        url = urldefrag(url)[0]
        if url not in seen and urlsplit(url).scheme in ("http", "https"):
            seen.add(url)
            out.append((url, kind))
    return out[:MAX_ASSETS_PER_PAGE]


def localize_html(text: str, page_url: str, local: dict[str, str]) -> str:
    """Point a prepared page's stylesheets/images at their saved copies (assets/<name>)."""
    prefix = ASSETS_DIR + "/"

    def tag_sub(m: re.Match) -> str:
        tag, name = m.group(0), m.group(1).lower()
        for attr in ("href", "src", "poster", "background"):
            if attr == "href" and name != "link":
                continue
            am = _attr(tag, attr)
            v = _attr_value(am)
            saved = local.get(urldefrag(urljoin(page_url, v))[0]) if _is_fetchable(v) else None
            if saved:
                tag = tag[: am.start()] + f' {attr}="{html.escape(prefix + saved, quote=True)}"' + tag[am.end():]
        am = _attr(tag, "srcset")
        if am:
            best = _srcset_best(_attr_value(am) or "")
            saved = local.get(urldefrag(urljoin(page_url, best))[0]) if _is_fetchable(best) else None
            src = _attr_value(_attr(tag, "src")) if name == "img" else None
            if src and src.startswith(prefix):
                saved = src[len(prefix):]  # src saved: show that copy whatever the screen size
            if saved:  # one saved candidate instead of every size
                tag = tag[: am.start()] + f' srcset="{html.escape(prefix + saved, quote=True)}"' + tag[am.end():]
            else:  # not saved: at least make the candidates absolute
                value = ", ".join(
                    urljoin(page_url, i.group(1)) + (f" {i.group(2)}{i.group(3)}" if i.group(2) else "")
                    for i in _SRCSET_ITEM.finditer(_attr_value(am) or "") if i.group(1))
                tag = tag[: am.start()] + f' srcset="{html.escape(value, quote=True)}"' + tag[am.end():]
        return tag

    text = _TAG.sub(tag_sub, text)
    text = _STYLE_BLOCK.sub(lambda m: m.group(1) + _css_localize(m.group(2), page_url, local, prefix) + m.group(3), text)

    def style_attr(m: re.Match) -> str:
        raw = m.group(2) if m.group(2) is not None else m.group(3)
        if "url(" not in raw:
            return m.group(0)
        css = _css_localize(html.unescape(raw), page_url, local, prefix)
        return f'{m.group(1)}"{html.escape(css, quote=True)}"'

    return _STYLE_ATTR.sub(style_attr, text)


def link_pages(text: str, local: dict[str, str]) -> str:
    """Links (href) to other saved pages → their local file (all in the same folder)."""
    def sub(m: re.Match) -> str:
        raw = next(g for g in m.groups()[1:] if g is not None)
        target, frag = urldefrag(html.unescape(raw).strip())
        name = local.get(target)
        if not name or not m.group(1).strip().lower().startswith("href"):
            return m.group(0)
        return f'{m.group(1)}"{html.escape(name + (f"#{frag}" if frag else ""), quote=True)}"'

    return _ATTR.sub(sub, text)


def asset_name(url: str, content_type: str | None) -> str | None:
    """File name for a saved asset, or None if its type isn't one we keep."""
    mime = (content_type or "").split(";")[0].strip().lower()
    ext = ASSET_TYPES.get(mime)
    if ext is None:
        guess = Path(urlsplit(url).path).suffix.lower()
        if guess in ASSET_EXTS and mime in ("", "application/octet-stream", "binary/octet-stream", "text/plain"):
            ext = ".jpg" if guess == ".jpeg" else guess
    if ext is None:
        return None
    return hashlib.sha1(url.encode()).hexdigest()[:20] + ext


class AssetStore:
    """Downloads a job's stylesheets/fonts/images once each into job<id>/assets/."""

    def __init__(self, folder: Path, client: httpx.AsyncClient, check_host, disk_low, images: bool = True) -> None:
        self.folder, self.client, self.check_host, self.disk_low = folder, client, check_host, disk_low
        self.images = images
        self.saved: dict[str, str] = {}  # url → file name
        self._tasks: dict[str, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(ASSET_CONCURRENCY)
        self.count = self.bytes = 0

    async def get_all(self, refs: list[tuple[str, str]], depth: int = 0) -> None:
        await asyncio.gather(*(self._get(url, kind, depth) for url, kind in refs))

    async def _get(self, url: str, kind: str, depth: int) -> None:
        task = self._tasks.get(url)
        if task is None:
            task = self._tasks[url] = asyncio.ensure_future(self._fetch(url, kind, depth))
        try:
            await asyncio.shield(task)
        except Exception:
            pass

    async def _fetch(self, url: str, kind: str, depth: int) -> None:
        if self.disk_low():
            return
        async with self._sem:
            try:
                page = await fetch_page(self.client, url, max_bytes=ASSET_MAX_BYTES, check_host=self.check_host)
            except ArchiveError as e:
                log.debug("asset %s not saved: %s", url, e)
                return
        name = asset_name(url, page.content_type)
        if name is None and kind == "css":
            name = hashlib.sha1(url.encode()).hexdigest()[:20] + ".css"  # some servers say text/plain
        if name is None:
            return
        body = page.body
        if name.endswith(".css"):
            css = body.decode(page_charset(page), errors="replace")
            if depth < CSS_DEPTH:
                refs = _css_refs(css, page.final_url)
                if not self.images:  # stylesheets and fonts only
                    refs = [r for r in refs if r[1] == "css" or Path(urlsplit(r[0]).path).suffix.lower() in FONT_EXTS]
                await self.get_all(refs, depth + 1)
            # Saved next to this file: a bare name works.
            body = _css_localize(css, page.final_url, self.saved, "").encode()
        self.folder.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread((self.folder / name).write_bytes, body)
        self.saved[url] = self.saved[page.final_url] = name
        self.count += 1
        self.bytes += len(body)


# ---- site icon --------------------------------------------------------------------

_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
ICON_TYPES = {".png", ".ico", ".svg", ".jpg", ".gif", ".webp", ".avif", ".bmp"}
ICON_MAX_BYTES = 1024 * 1024


def site_icons(page: Page) -> list[str]:
    """The site's icon URLs, best first: large apple-touch/PNG icons, then SVG, then any
    rel=icon, then /favicon.ico (where browsers look when a page names none)."""
    text = page.body[:300_000].decode(page_charset(page), errors="replace")
    ranked = []
    for i, m in enumerate(_LINK_TAG.finditer(text)):
        tag = m.group(0)
        rel = (_attr_value(_attr(tag, "rel")) or "").lower().split()
        href = _attr_value(_attr(tag, "href"))
        if not _is_fetchable(href) or not ({"icon", "apple-touch-icon", "apple-touch-icon-precomposed"} & set(rel)):
            continue
        sizes = [int(n) for n in re.findall(r"(\d+)x\d+", _attr_value(_attr(tag, "sizes")) or "")]
        size = max(sizes, default=180 if "apple-touch-icon" in " ".join(rel) else 0)
        svg = href.lower().split("?")[0].endswith(".svg") or "svg" in (_attr_value(_attr(tag, "type")) or "")
        # 64-256 px is plenty for a list icon; svg scales; unknown sizes come after
        score = (2 if 64 <= size <= 256 else 1 if size > 256 or svg else 0, min(size, 256), -i)
        ranked.append((score, urljoin(page.final_url, href)))
    urls = [u for _, u in sorted(ranked, reverse=True)]
    urls.append(urljoin(page.final_url, "/favicon.ico"))
    return list(dict.fromkeys(urls))


# ---- jobs -------------------------------------------------------------------------

JOB_COLS = ("id, url, title, categories, max_pages, save_images, status, pages_saved, pages_failed, "
            "assets_saved, assets_bytes, pages_bytes, error, created_at, finished_at, icon_path")


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
        title, links = parse_html(page)
        if not links and b"<script" in page.body.lower():
            raise ArchiveError("this site only shows its pages to browsers running JavaScript, "
                               "which the archiver can't do")
        menus = read_navbar(page)
        if not menus:  # no menu found: offer the sections linked from the page instead
            _, cats = find_categories(page)
            menus = [{"name": "Sections linked on the home page",
                      "items": [{"label": c["label"], "url": c["url"], "prefix": c["prefix"], "depth": 0}
                                for c in cats]}] if cats else []
        return {"url": page.final_url, "title": title, "menus": menus}

    async def start(self, url: str, categories: list[dict], max_pages: int, save_images: bool = True,
                    title: str | None = None) -> dict:
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
            f"INSERT INTO web_archive_jobs (url, categories, max_pages, save_images, title) "
            f"VALUES ($1, $2, $3, $4, $5) RETURNING {JOB_COLS}",
            url, cats, max_pages, save_images, (title or "")[:300] or None,
        )
        self._tasks[row["id"]] = asyncio.create_task(self._run(row["id"]), name=f"web-archive-{row['id']}")
        return dict(row)

    async def rerun(self, job_id: int) -> dict | None:
        """Archive a finished job's site again with the same menu items and settings, replacing
        its saved pages and files (same id, so links to it keep working). None if no such job."""
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            raise ArchiveError("this archive is still running")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE web_archive_jobs SET status = 'queued', pages_saved = 0, pages_failed = 0, "
                "assets_saved = 0, assets_bytes = 0, pages_bytes = 0, error = NULL, created_at = now(), "
                "icon_path = NULL, icon_checked = false, "
                f"finished_at = NULL WHERE id = $1 AND status NOT IN ('queued', 'running') RETURNING {JOB_COLS}",
                job_id,
            )
            if row is None:
                if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM web_archive_jobs WHERE id = $1)", job_id):
                    raise ArchiveError("this archive is still running")
                return None
            await conn.execute("DELETE FROM web_archive_pages WHERE job_id = $1", job_id)
        await asyncio.to_thread(shutil.rmtree, self.media_dir / "web" / f"job{job_id}", True)
        self._tasks[job_id] = asyncio.create_task(self._run(job_id), name=f"web-archive-{job_id}")
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
        job = await self.pool.fetchrow(
            "SELECT url, categories, max_pages, save_images FROM web_archive_jobs WHERE id = $1", job_id)
        cats = job["categories"]
        folder = self.media_dir / "web" / f"job{job_id}"
        folder.mkdir(parents=True, exist_ok=True)
        # Each picked menu item's listing pages (its page, then page 2, 3…) are read only to
        # find posts; only posts are saved and counted. Posts found go first and items take
        # turns, so a small limit still gets posts from every picked item.
        posts = {c["prefix"]: deque() for c in cats}
        listings = {c["prefix"]: deque([c["url"]]) for c in cats}
        listing_reads = dict.fromkeys(posts, 0)
        posts_found = dict.fromkeys(posts, 0)
        thumbs: dict[str, str] = {}  # post URL → its thumbnail on the list page
        listing_keys = {_listing_key(c["url"]) for c in cats}
        order = deque(c["prefix"] for c in cats)
        seen = {c["url"] for c in cats}
        fetched: set[str] = set()
        saved = requests = 0

        def next_url():
            for _ in range(len(order)):
                prefix = order[0]
                order.rotate(-1)
                if posts[prefix]:
                    return posts[prefix].popleft(), prefix, False
                if listings[prefix] and listing_reads[prefix] < MAX_LISTING_PAGES:
                    listing_reads[prefix] += 1
                    return listings[prefix].popleft(), prefix, True
            return None

        async with self.client_factory() as client:
            assets = AssetStore(folder / ASSETS_DIR, client, self.check_host, self._disk_low, job["save_images"])
            robots = await self._robots(client, job["url"])
            await self._save_icon(client, job_id, job["url"])
            while saved < job["max_pages"] and (item := next_url()):
                url, prefix, is_listing = item
                if robots and not robots.can_fetch(ROBOTS_AGENT, url):
                    if not is_listing:
                        await self._record(job_id, url, prefix, error="blocked by robots.txt")
                    continue
                if self._disk_low():
                    raise ArchiveError("disk almost full: stopped")
                if requests:
                    await asyncio.sleep(self.delay)
                requests += 1
                try:
                    page = await fetch_page(client, url, check_host=self.check_host)
                except ArchiveError as e:
                    if not is_listing:
                        await self._record(job_id, url, prefix, error=str(e))
                    continue
                if page.final_url in fetched:  # e.g. redirected to a page already read
                    continue
                fetched.add(page.final_url)

                if is_listing:
                    if page.is_html:
                        _, links = parse_html(page)
                        found, more = listing_links(page, links, prefix, listing_keys)
                        for u, img in post_thumbnails(links).items():
                            thumbs.setdefault(u, img)
                        new = [u for u in found if u not in seen]
                        seen.update(new)
                        posts[prefix].extend(new)
                        posts_found[prefix] += len(new)
                        nxt = _parse(page).next
                        if nxt:
                            more.append(urldefrag(urljoin(page.final_url, nxt))[0])
                        if new:  # stop paging once a page brings nothing new
                            for u in more:
                                if u not in seen and is_pagination(u, page.final_url):
                                    seen.add(u)
                                    listings[prefix].append(u)
                    continue

                title = parse_html(page)[0] if page.is_html else None
                name = file_name(page.final_url, page.content_type)
                body = page.body
                if page.is_html:
                    # Saved as UTF-8, with its stylesheets/fonts/images next to it.
                    text = prepare_html(body.decode(page_charset(page), errors="replace"), page.final_url)
                    before = (assets.count, assets.bytes)
                    await assets.get_all(html_assets(text, page.final_url, images=job["save_images"]))
                    body = localize_html(text, page.final_url, assets.saved).encode()
                    await self.pool.execute(
                        "UPDATE web_archive_jobs SET assets_saved = assets_saved + $2, "
                        "assets_bytes = assets_bytes + $3 WHERE id = $1",
                        job_id, assets.count - before[0], assets.bytes - before[1])
                await asyncio.to_thread((folder / name).write_bytes, body)
                thumb = await self._save_thumb(assets, job_id, thumbs.get(url) or (
                    share_image(page) if page.is_html else None))
                await self._record(job_id, url, prefix, page=page, title=title, size=len(body),
                                   file_path=f"web/job{job_id}/{name}", thumb_path=thumb)
                saved += 1
        if not saved and not any(posts_found.values()):
            raise ArchiveError("no posts found on the picked menu items' pages (the site may build its "
                               "lists with JavaScript, which the archiver can't run)")

    async def _save_icon(self, client: httpx.AsyncClient, job_id: int, url: str) -> None:
        """Save the site's icon as job<id>/icon.<ext> for the archive list (best effort)."""
        try:
            home = await fetch_page(client, url, check_host=self.check_host)
            candidates = site_icons(home) if home.is_html else [urljoin(url, "/favicon.ico")]
        except ArchiveError:
            candidates = [urljoin(url, "/favicon.ico")]
        path = None
        for icon_url in candidates[:4]:
            try:
                icon = await fetch_page(client, icon_url, max_bytes=ICON_MAX_BYTES, check_host=self.check_host)
            except ArchiveError:
                continue
            name = asset_name(icon.final_url, icon.content_type)
            ext = Path(name or "").suffix
            if ext not in ICON_TYPES or not icon.body:
                continue
            if not await self.pool.fetchval("SELECT EXISTS (SELECT 1 FROM web_archive_jobs WHERE id = $1)", job_id):
                return  # deleted meanwhile: don't leave a folder behind
            folder = self.media_dir / "web" / f"job{job_id}"
            folder.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread((folder / f"icon{ext}").write_bytes, icon.body)
            path = f"web/job{job_id}/icon{ext}"
            break
        await self.pool.execute(
            "UPDATE web_archive_jobs SET icon_path = $2, icon_checked = true WHERE id = $1", job_id, path)

    async def backfill_icons(self) -> None:
        """Archives made before icons were kept get theirs once (in the background at startup)."""
        rows = await self.pool.fetch(
            "SELECT id, url FROM web_archive_jobs WHERE NOT icon_checked AND status NOT IN ('queued', 'running') "
            "ORDER BY id")
        if not rows:
            return
        async with self.client_factory() as client:
            for r in rows:
                try:
                    await self._save_icon(client, r["id"], r["url"])
                except Exception:
                    log.exception("icon for web archive job %s failed", r["id"])

    async def _save_thumb(self, assets: AssetStore, job_id: int, image_url: str | None) -> str | None:
        """Save a post's thumbnail with the job's assets (also when page images are off: it's
        one small picture per post, for the archive's list). Returns its path under MEDIA_DIR."""
        if not image_url:
            return None
        before = (assets.count, assets.bytes)
        await assets.get_all([(image_url, "file")])
        name = assets.saved.get(image_url)
        if assets.count != before[0]:
            await self.pool.execute(
                "UPDATE web_archive_jobs SET assets_saved = assets_saved + $2, assets_bytes = assets_bytes + $3 "
                "WHERE id = $1", job_id, assets.count - before[0], assets.bytes - before[1])
        if not name or Path(name).suffix not in ICON_TYPES:
            return None
        return f"web/job{job_id}/{ASSETS_DIR}/{name}"

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
                      title: str | None = None, size: int | None = None, file_path: str | None = None,
                      thumb_path: str | None = None, error: str | None = None) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO web_archive_pages (job_id, url, final_url, category, status, content_type,
                                               title, size, file_path, error, thumb_path)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                job_id, url, page.final_url if page else url, prefix, page.status if page else None,
                page.content_type if page else None, title, size, file_path, error, thumb_path,
            )
            col = "pages_failed" if error else "pages_saved"
            await conn.execute(f"UPDATE web_archive_jobs SET {col} = {col} + 1, "
                               "pages_bytes = pages_bytes + $2 WHERE id = $1", job_id, size or 0)

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
            if not Page(r["url"], r["final_url"], 200, r["content_type"], b"").is_html:
                continue
            path = self.media_dir / r["file_path"]

            def work(path=path):
                try:
                    text = path.read_text("utf-8", errors="replace")  # saved as UTF-8
                except FileNotFoundError:
                    return
                path.write_text(link_pages(text, local), "utf-8")

            await asyncio.to_thread(work)
