import asyncio

import httpx
import pytest

from app import archive
from app.archive import (ArchiveError, Page, WebArchiver, fetch_page, file_name, find_categories, html_assets,
                         link_pages, localize_html, prepare_html, read_navbar)
from app.main import app

from .test_api import api  # noqa: F401  (fixture)

SITE = "https://site.test"

HOME = b"""<html><head><title> Site &amp; Co </title><link rel=stylesheet href=/s.css></head><body>
<nav class="main" aria-label="Main"><ul>
  <li><a href="/"><img alt="Home" src="/logo.png"></a>
  <li><a href="/news/">News</a>
    <ul><li><a href="/news/world.htm"><span>World</span></a></li></ul>
  <li><a href="/sport">Sport</a>
  <li><a href="/category/tech/">Tech</a>
</ul></nav>
<p><a href="/news/a">Story A</a> <a href="/news/b">Story B</a> <a href="/sport/x">Match</a>
<a href="/about.html">About</a> <a href="/logo.png">logo</a> <a href="https://other.test/news/z">elsewhere</a>
<a href="mailto:me@site.test">mail</a></p>
</body></html>"""

PAGES = {
    "/": HOME,
    "/robots.txt": b"User-agent: *\nDisallow: /news/secret\n",
    "/news/": b'<title>News</title><header><a href="/sport/">Sport</a></header><a href="/news/a">A</a>'
              b'<a href="/news/page/2/">2</a><a href="/news/secret">s</a><a href="/story-7.html">Story 7</a>'
              b'<footer><a href="/privacy">Privacy</a></footer>',
    "/story-7.html": b'<title>Story 7</title><a href="/news/c">more</a>',
    "/news/page/2/": b'<title>News 2</title><a href="/news/b">B</a>',
    "/news/a": b'<html><head><title>A</title><link rel="stylesheet" href="/s.css"></head><body>'
               b'<a href="/news/b#top">B</a><img src="../img/a.png"><a href="/sport/x">x</a>'
               b'<script>document.write("ad")</script></body></html>',
    "/news/b": b'<title>B</title><a href="a">A</a>',
    "/news/world.htm": b'<title>World</title>',
    "/sport/": b"<title>Sport</title>",
    "/sport/x": b"<title>X</title>",
}


ASSETS = {
    "/s.css": ("text/css", b'@import "more.css"; body { font-family: F; } h1 { background: url(img/bg.png) }'),
    "/more.css": ("text/css", b'@font-face { font-family: F; src: url("/fonts/f.woff2") format("woff2") }'),
    "/fonts/f.woff2": ("font/woff2", b"wOF2"),
    "/img/a.png": ("image/png", b"\x89PNG a"),
    "/img/bg.png": ("image/png", b"\x89PNG bg"),
    "/logo.png": ("image/png", b"\x89PNG logo"),
}


def site_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path in ASSETS:
        ctype, body = ASSETS[req.url.path]
        return httpx.Response(200, content=body, headers={"content-type": ctype})
    if req.url.path in ("/old", "/sport"):
        return httpx.Response(301, headers={"location": {"/old": "/", "/sport": "/sport/"}[req.url.path]})
    body = PAGES.get(req.url.path)
    if body is None:
        return httpx.Response(404)
    ctype = "text/plain" if req.url.path.endswith(".txt") else "text/html; charset=utf-8"
    return httpx.Response(200, content=body, headers={"content-type": ctype})


async def _public(host):
    pass


def _archiver(pool, tmp_path, handler=site_handler):
    return WebArchiver(pool, tmp_path, check_host=_public, delay=0,
                       client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_read_navbar():
    menus = read_navbar(Page(SITE, SITE + "/", 200, "text/html", HOME))
    assert menus == [{"name": "Main", "items": [  # in the menu's order; home and files left out
        {"label": "News", "url": SITE + "/news/", "prefix": "/news/", "depth": 0},
        {"label": "World", "url": SITE + "/news/world.htm", "prefix": "/news/world/", "depth": 1},
        {"label": "Sport", "url": SITE + "/sport", "prefix": "/sport/", "depth": 0},
        {"label": "Tech", "url": SITE + "/category/tech/", "prefix": "/category/tech/", "depth": 0},
    ]}]


def test_read_navbar_prefers_real_nav_and_skips_footer():
    body = (b'<div class="menu-mobile"><a href="/a/">A</a></div><nav><a href="/b.html">B</a>'
            b'<a href="https://other.test/c/">C</a><a href="/b.html">B again</a></nav>'
            b'<footer><nav><a href="/privacy/">Privacy</a></nav></footer>')
    menus = read_navbar(Page(SITE, SITE + "/", 200, "text/html", body))
    assert [[i["label"] for i in m["items"]] for m in menus] == [["B"]]
    assert menus[0]["items"][0]["prefix"] == "/b/" and menus[0]["name"] == "Main menu"


def test_find_categories_fallback():
    title, cats = find_categories(Page(SITE, SITE + "/", 200, "text/html", HOME))
    assert title == "Site & Co"
    assert {c["prefix"] for c in cats} == {"/news/", "/sport/", "/category/tech/"}


def test_prepare_and_localize_html():
    page = SITE + "/news/a"
    text = prepare_html(
        '<html><head><meta charset="windows-1252"><meta http-equiv="refresh" content="0;url=/x">'
        '<base href="/x/"><link rel="preload" as="style" href="p.css" onload="this.rel=\'stylesheet\'">'
        '<link rel="preload" href="/ad.js" as="script"><link rel=modulepreload href=/m.js>'
        '<link rel="dns-prefetch" href="//ads.test">'
        '<style>.h { background: url(\'/bg.png\') }</style></head><body>'
        '<script src="/a.js"></script><SCRIPT>alert(1)</SCRIPT>'
        '<a href="/news/b#top" onclick="track()">b</a> <a href=?q=1&amp;p=2 onmouseover=x()>q</a> <a href="#s">s</a>'
        '<img class="lazy lazy-blur lazyload" src="data:image/gif;base64,R0" data-src="/big.jpg" alt="b">'
        '<img srcset="s.jpg 300w, l.jpg 900w, m.jpg 600w, xl.jpg 2400w">'
        '<img src="/p.jpg" srcset="/p-2x.jpg 2x">'
        '<div style="background-image: url(&quot;/d.png&quot;)">d</div>'
        '<video poster="/v.jpg"><source src="/v.mp4"></video></body></html>', page)
    assert "<script" not in text.lower() and "refresh" not in text and "windows-1252" not in text
    assert 'class="lazy lazyloaded"' in text  # blur placeholder lifted, layout classes kept
    assert "ad.js" not in text and "m.js" not in text and "ads.test" not in text
    assert text.startswith('<html><head><meta charset="utf-8"><link rel="stylesheet" as="style" href="https://site.test/news/p.css"')
    assert '<a href="https://site.test/news/b#top">' in text and '<a href="https://site.test/news/a?q=1&amp;p=2">' in text
    assert '<a href="#s">' in text and "track()" not in text and "x()" not in text

    refs = html_assets(text, page)
    assert refs == [
        ("https://site.test/news/p.css", "css"), ("https://site.test/big.jpg", "file"),
        ("https://site.test/news/l.jpg", "file"),  # one srcset candidate: widest up to 1000px
        ("https://site.test/p.jpg", "file"),  # plain src preferred over its srcset
        ("https://site.test/v.jpg", "file"),  # poster, not the video
        ("https://site.test/bg.png", "file"), ("https://site.test/d.png", "file"),
    ]
    assert [u for u, k in html_assets(text, page, images=False)] == ["https://site.test/news/p.css"]

    local = {u: f"n{i}{u[-4:]}" for i, (u, _) in enumerate(refs) if not u.endswith("bg.png")}
    out = localize_html(text, page, local)
    assert 'href="assets/n0.css"' in out and 'src="assets/n1.jpg"' in out and 'srcset="assets/n2.jpg"' in out
    assert 'src="assets/n3.jpg" srcset="assets/n3.jpg"' in out
    assert 'poster="assets/n4.jpg"' in out
    assert "url(\'/bg.png\')" not in out and 'url("https://site.test/bg.png")' in out  # not saved: live URL
    assert 'style="background-image: url(&quot;assets/n6.png&quot;)"' in out
    assert '<source src="https://site.test/v.mp4">' in out

    linked = link_pages(out, {SITE + "/news/b": "news_b_1.html"})
    assert '<a href="news_b_1.html#top">' in linked and 'href="assets/n0.css"' in linked


def test_file_name_is_stable():
    assert file_name(SITE + "/news/a", "text/html") == file_name(SITE + "/news/a", None)
    assert file_name(SITE + "/", "text/html").startswith("index_")
    assert file_name(SITE + "/doc.pdf", "application/pdf").endswith(".pdf")
    assert file_name(SITE + "/xa-hoi.htm", "text/html").startswith("xa-hoi_")


async def test_fetch_checks_every_redirect_hop():
    checked = []

    async def check(host):
        checked.append(host)
        if host == "internal":
            raise ArchiveError("internal is not a public address")

    handler = lambda r: httpx.Response(302, headers={"location": "http://internal/admin"})  # noqa: E731
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ArchiveError, match="not a public"):
            await fetch_page(c, SITE + "/", check_host=check)
    assert checked == ["site.test", "internal"]


async def test_fetch_limits():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 100))) as c:
        with pytest.raises(ArchiveError, match="larger"):
            await fetch_page(c, SITE, check_host=_public, max_bytes=50)
    loop = lambda r: httpx.Response(302, headers={"location": "/loop"})  # noqa: E731
    async with httpx.AsyncClient(transport=httpx.MockTransport(loop)) as c:
        with pytest.raises(ArchiveError, match="too many redirects"):
            await fetch_page(c, SITE, check_host=_public)


async def test_private_hosts_refused():
    for host in ("127.0.0.1", "::1", "10.0.0.5", "localhost"):
        with pytest.raises(ArchiveError, match="not a public address"):
            await archive.check_public_host(host)


async def test_crawl_picked_categories(pool, tmp_path):
    arch = _archiver(pool, tmp_path)
    found = await arch.discover("site.test/old")  # scheme added, redirect followed
    assert found["url"] == SITE + "/" and found["title"] == "Site & Co"
    news = next(i for i in found["menus"][0]["items"] if i["prefix"] == "/news/")

    job = await arch.start(found["url"], [news], max_pages=20)
    await arch._tasks[job["id"]]
    row = await pool.fetchrow("SELECT * FROM web_archive_jobs WHERE id = $1", job["id"])
    assert row["status"] == "done" and row["title"] == "Site & Co"
    pages = await pool.fetch("SELECT * FROM web_archive_pages WHERE job_id = $1 ORDER BY id", job["id"])
    saved = [p["url"].removeprefix(SITE) for p in pages if p["file_path"]]
    # /story-7.html and /sport/x: linked from News pages, so saved, but not crawled on (no /news/c);
    # not /sport/: only in the menu; not /privacy: footer
    # articles found on a listing page come before more listing pages
    assert saved == ["/", "/news/", "/story-7.html", "/news/world.htm", "/news/a", "/sport/x", "/news/b",
                     "/news/page/2/"]
    assert [(p["url"].removeprefix(SITE), p["error"]) for p in pages if p["error"]] == [
        ("/news/secret", "blocked by robots.txt")]
    assert (row["pages_saved"], row["pages_failed"]) == (8, 1)

    # links between saved pages point to the local copies, the rest to the live site
    files = {p["url"].removeprefix(SITE): p["file_path"] for p in pages if p["file_path"]}
    a = (tmp_path / files["/news/a"]).read_text()
    b_name = files["/news/b"].rsplit("/", 1)[1]
    assert f'href="{b_name}#top"' in a
    # the page's look is kept: stylesheet, @imported CSS, font, background and image saved locally
    assets = tmp_path / f"web/job{job['id']}/assets"
    assert sorted(p.name.rsplit(".", 1)[1] for p in assets.iterdir()) == ["css", "css", "png", "png", "png", "woff2"]
    main_css = next(p for p in assets.iterdir() if b"@import" in p.read_bytes()).read_text()
    assert main_css.startswith('@import "') and 'url("' in main_css and "site.test" not in main_css
    assert (assets / main_css.split('"')[1]).read_text().count("url(") == 1
    assert 'href="assets/' in a and 'src="assets/' in a and "<script" not in a
    assert '<meta charset="utf-8">' in a
    row = await pool.fetchrow("SELECT assets_saved, assets_bytes, pages_bytes FROM web_archive_jobs WHERE id = $1", job["id"])
    assert row["assets_saved"] == 6 and row["assets_bytes"] > 0 and row["pages_bytes"] > 0
    assert (await pool.fetchval("SELECT count(*) FROM web_archive_pages WHERE url LIKE '%/news/c'")) == 0
    s = (tmp_path / files["/story-7.html"]).read_text()
    assert 'href="https://site.test/news/c"' in s  # not archived: points to the live site
    assert files["/news/a"].startswith(f"web/job{job['id']}/")


async def test_max_pages_and_cancel(pool, tmp_path):
    arch = _archiver(pool, tmp_path)
    cats = [{"prefix": "/news/", "url": SITE + "/news/"}]
    job = await arch.start(SITE, cats, max_pages=2)
    await arch._tasks[job["id"]]
    assert await pool.fetchval("SELECT pages_saved FROM web_archive_jobs WHERE id = $1", job["id"]) == 2

    # categories take turns: a small limit still gets some of each
    job = await arch.start(SITE, cats + [{"prefix": "/sport/", "url": SITE + "/sport/"}], max_pages=4)
    await arch._tasks[job["id"]]
    pages = await pool.fetch("SELECT url FROM web_archive_pages WHERE job_id = $1 ORDER BY id", job["id"])
    assert [p["url"].removeprefix(SITE) for p in pages] == ["/", "/news/", "/sport/", "/story-7.html"]

    gate = asyncio.Event()

    async def slow(req):
        await gate.wait()
        return site_handler(req)

    arch = _archiver(pool, tmp_path, slow)
    job = await arch.start(SITE, cats, max_pages=20)
    await asyncio.sleep(0.05)
    assert await arch.cancel(job["id"])
    await asyncio.gather(arch._tasks[job["id"]], return_exceptions=True)
    assert await pool.fetchval("SELECT status FROM web_archive_jobs WHERE id = $1", job["id"]) == "cancelled"

    with pytest.raises(ArchiveError, match="not on site.test"):
        await arch.start(SITE, [{"prefix": "/news/", "url": "https://evil.test/news/"}], 5)


async def test_api(api, pool, tmp_path):  # noqa: F811
    app.state.archiver = _archiver(pool, tmp_path)
    h = api.http
    r = await h.post("/api/archive/discover", json={"url": SITE})
    assert r.status_code == 200 and r.json()["menus"][0]["items"][0]["label"] == "News"
    assert (await h.post("/api/archive/discover", json={"url": "ftp://x"})).status_code == 400

    r = await h.post("/api/archive/jobs", json={"url": SITE, "categories": [{"prefix": "/sport/"}], "max_pages": 5,
                                                "save_images": False})
    assert r.status_code == 202
    job_id = r.json()["id"]
    await app.state.archiver._tasks[job_id]
    assert (await h.post("/api/archive/jobs", json={"url": SITE, "categories": []})).status_code == 422

    jobs = (await h.get("/api/archive/jobs")).json()
    assert [(j["id"], j["status"], j["pages_saved"]) for j in jobs] == [(job_id, "done", 3)]  # home, /sport/ and /sport/x (linked from home)
    detail = (await h.get(f"/api/archive/jobs/{job_id}")).json()
    assert [p["title"] for p in detail["pages"]] == ["Site & Co", "Sport", "X"]
    folder = tmp_path / "web" / f"job{job_id}"
    assert len([p for p in folder.iterdir() if p.is_file()]) == 3
    assert sorted(p.suffix for p in (folder / "assets").iterdir()) == [".css", ".css", ".woff2"]  # no images

    assert (await h.delete(f"/api/archive/jobs/{job_id}")).status_code == 200
    assert not folder.exists() and (await h.get(f"/api/archive/jobs/{job_id}")).status_code == 404


async def test_interrupted_on_restart(pool, tmp_path):
    await pool.execute("INSERT INTO web_archive_jobs (url, categories, max_pages, status) VALUES ($1, '[]', 5, 'running')", SITE)
    await _archiver(pool, tmp_path).mark_interrupted()
    assert await pool.fetchval("SELECT status FROM web_archive_jobs") == "interrupted"
