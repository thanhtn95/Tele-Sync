import asyncio

import httpx
import pytest

from app import archive
from app.archive import (ArchiveError, Page, WebArchiver, _listing_key, fetch_page, file_name, find_categories,
                         html_assets, is_pagination, link_pages, listing_links, localize_html, looks_like_post,
                         parse_html, prepare_html, read_navbar, site_icons)
from app.main import app

from .test_api import api  # noqa: F401  (fixture)

SITE = "https://site.test"

HOME = b"""<html><head><title> Site &amp; Co </title><link rel=stylesheet href=/s.css>
<link rel="icon" href="/favicon-16.png" sizes="16x16"><link rel="apple-touch-icon" href="/touch.png"></head><body>
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

# /news/ and /sport/ are listings: only the posts they list are saved.
PAGES = {
    "/": HOME,
    "/robots.txt": b"User-agent: *\nDisallow: /news/secret\n",
    "/news/": b'<title>News</title><header><a href="/sport/">Sport</a></header>'
              b'<h2><a href="/news/a">A</a></h2>'
              b'<article><a href="/story-7.html">Story 7</a></article>'
              b'<p><a href="/2026/10/09/big-story">Big</a> <a href="/news/secret-12345">s</a>'
              b' <a href="/sport/">Sport</a> <a href="/news/world.htm">World</a></p>'
              b'<aside><h3><a href="/hot-post-99999">Hot</a></h3></aside>'
              b'<a href="/news/page/2/">2</a><footer><a href="/privacy-policy-of-site">Privacy</a></footer>',
    "/news/page/2/": b'<title>News 2</title><h2><a href="/news/b">B</a></h2><h2><a href="/news/a">A</a></h2>'
                     b'<a href="/news/page/3/">3</a>',
    "/news/page/3/": b'<title>News 3</title><h2><a href="/news/a">A</a></h2><a href="/news/page/4/">4</a>',
    "/news/a": b'<html><head><title>A</title><link rel="stylesheet" href="/s.css"></head><body>'
               b'<a href="/news/b#top">B</a><img src="../img/a.png"><a href="/sport/x">x</a>'
               b'<script>document.write("ad")</script></body></html>',
    "/news/b": b'<title>B</title><a href="a">A</a>',
    "/story-7.html": b'<title>Story 7</title><a href="/news/c">more</a>',
    "/2026/10/09/big-story": b'<title>Big</title><a href="/news/a">A</a>',
    "/news/world.htm": b'<title>World</title>',
    "/sport/": b'<title>Sport</title><h3><a href="/sport/x">X</a></h3>',
    "/sport/x": b"<title>X</title>",
}


ASSETS = {
    "/s.css": ("text/css", b'@import "more.css"; body { font-family: F; } h1 { background: url(img/bg.png) }'),
    "/more.css": ("text/css", b'@font-face { font-family: F; src: url("/fonts/f.woff2") format("woff2") }'),
    "/fonts/f.woff2": ("font/woff2", b"wOF2"),
    "/img/a.png": ("image/png", b"\x89PNG a"),
    "/img/bg.png": ("image/png", b"\x89PNG bg"),
    "/logo.png": ("image/png", b"\x89PNG logo"),
    "/touch.png": ("image/png", b"\x89PNG touch"),
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


def test_listing_links():
    page = Page(SITE + "/news/", SITE + "/news/", 200, "text/html", PAGES["/news/"])
    posts, more = listing_links(page, parse_html(page)[1], "/news/", {_listing_key(SITE + "/news/")})
    # headline, <article> and post-style links; not menus, header, footer, sidebar or other sections
    assert posts == [SITE + "/news/a", SITE + "/story-7.html", SITE + "/2026/10/09/big-story",
                     SITE + "/news/secret-12345"]
    assert more == [SITE + "/news/page/2/"]

    # most posts under the item's own path: the few from elsewhere are left out
    body = b"".join(b'<h3><a href="/news/story%d">p</a></h3>' % i for i in range(4)) + b'<h3><a href="/x/y">y</a></h3>'
    page = Page(SITE + "/news.htm", SITE + "/news.htm", 200, "text/html", body)
    assert listing_links(page, parse_html(page)[1], "/news/", set())[0] == [SITE + f"/news/story{i}" for i in range(4)]


def test_listing_links_real_page_shapes():
    # A page-wide wrapper with a nav/sidebar class (Wired: <body class="site-navigation">,
    # CSS-Tricks: <div class="articles-and-sidebar">) doesn't hide every post; a real sidebar
    # inside it still does. WordPress post cards keep their title in <header>.
    cards = b"".join(
        b'<article><header class="entry-header"><h2><a href="/blog/post-%d/">Post %d</a></h2></header>'
        b'<a href="/author/ann/">Ann</a><footer class="entry-footer"><a href="/tag/x/">x</a></footer></article>'
        % (i, i) for i in range(4))
    body = (b'<body class="has-site-navigation"><div class="articles-and-sidebar">'
            b'<header><a href="/about-our-company-and-team/">About</a></header>' + cards +
            b'<p><a href="/blog/why-we-moved-our-servers-to-the-moon">Why we moved our servers to the moon</a></p>'
            b'<div class="sidebar"><h3><a href="/blog/most-read-1/">Most read</a></h3></div>'
            b'</div></body>')
    page = Page(SITE + "/blog/", SITE + "/blog/", 200, "text/html", body)
    posts, _ = listing_links(page, parse_html(page)[1], "/blog/", set())
    assert posts == [SITE + f"/blog/post-{i}/" for i in range(4)] + [SITE + "/blog/why-we-moved-our-servers-to-the-moon"]


def test_site_icons_best_first():
    body = (b'<link rel="icon" href="/f16.png" sizes="16x16"><link rel="icon" href="/f.svg" type="image/svg+xml">'
            b'<link rel="icon" href="/f192.png" sizes="192x192"><link rel="shortcut icon" href="/f.ico">'
            b'<link rel="apple-touch-icon" href="/touch.png"><link rel="stylesheet" href="/s.css">')
    page = Page(SITE + "/", SITE + "/", 200, "text/html", body)
    assert site_icons(page) == [SITE + "/f192.png", SITE + "/touch.png", SITE + "/f.svg", SITE + "/f16.png",
                                SITE + "/f.ico", SITE + "/favicon.ico"]
    assert site_icons(Page(SITE + "/", SITE + "/", 200, "text/html", b"<p>no icons</p>")) == [SITE + "/favicon.ico"]


def test_pagination_and_post_urls():
    for listing, nxt in [("https://vnexpress.net/du-lich", "https://vnexpress.net/du-lich-p2"),
                         ("https://dantri.com.vn/the-gioi.htm", "https://dantri.com.vn/the-gioi/trang-2.htm"),
                         ("https://techcrunch.com/category/startups/", "https://techcrunch.com/category/startups/page/2/"),
                         (SITE + "/blog/", SITE + "/blog/?page=3")]:
        assert is_pagination(nxt, listing), nxt
    assert not is_pagination("https://vnexpress.net/the-thao-p2", "https://vnexpress.net/du-lich")
    assert not is_pagination("https://dantri.com.vn/the-gioi/a-b-nha-trang-20261010075618440.htm",
                             "https://dantri.com.vn/the-gioi.htm")
    assert looks_like_post("https://vnexpress.net/ban-ta-phinh-mua-nuoc-ngap-5124736.html")
    assert looks_like_post("https://techcrunch.com/2026/10/09/some-post/")
    assert looks_like_post(SITE + "/how-to-make-good-coffee/")
    assert not looks_like_post(SITE + "/news/") and not looks_like_post(SITE + "/category/tech/")


async def test_crawl_saves_posts_only(pool, tmp_path):
    requested = []

    def handler(req):
        requested.append(req.url.path)
        return site_handler(req)

    arch = _archiver(pool, tmp_path, handler)
    found = await arch.discover("site.test/old")  # scheme added, redirect followed
    assert found["url"] == SITE + "/" and found["title"] == "Site & Co"
    news = next(i for i in found["menus"][0]["items"] if i["prefix"] == "/news/")
    requested.clear()  # count only what the job fetches

    job = await arch.start(found["url"], [news], max_pages=20, title=found["title"])
    await arch._tasks[job["id"]]
    row = await pool.fetchrow("SELECT * FROM web_archive_jobs WHERE id = $1", job["id"])
    assert row["status"] == "done" and row["title"] == "Site & Co"
    pages = await pool.fetch("SELECT * FROM web_archive_pages WHERE job_id = $1 ORDER BY id", job["id"])
    saved = [p["url"].removeprefix(SITE) for p in pages if p["file_path"]]
    # the posts listed on News and its next page; listing pages and the home page aren't saved
    assert saved == ["/news/a", "/story-7.html", "/2026/10/09/big-story", "/news/b"]
    assert [(p["url"].removeprefix(SITE), p["error"]) for p in pages if p["error"]] == [
        ("/news/secret-12345", "blocked by robots.txt")]
    assert (row["pages_saved"], row["pages_failed"]) == (4, 1)
    assert {p["category"] for p in pages} == {"/news/"}
    # page 3 listed nothing new: no page 4; never the sidebar, footer or other sections
    assert "/news/page/3/" in requested and "/news/page/4/" not in requested
    assert not {"/hot-post-99999", "/privacy-policy-of-site", "/sport/", "/news/world.htm"} & set(requested)
    assert requested.count("/") == 1  # read once, for the site's icon; not saved
    icon = await pool.fetchval("SELECT icon_path FROM web_archive_jobs WHERE id = $1", job["id"])
    assert icon == f"web/job{job['id']}/icon.png" and (tmp_path / icon).read_bytes() == b"\x89PNG touch"

    # links between saved posts point to the local copies, the rest to the live site
    files = {p["url"].removeprefix(SITE): p["file_path"] for p in pages if p["file_path"]}
    a = (tmp_path / files["/news/a"]).read_text()
    b_name = files["/news/b"].rsplit("/", 1)[1]
    assert f'href="{b_name}#top"' in a and 'href="https://site.test/sport/x"' in a
    big = (tmp_path / files["/2026/10/09/big-story"]).read_text()
    assert f'href="{files["/news/a"].rsplit("/", 1)[1]}"' in big
    s7 = (tmp_path / files["/story-7.html"]).read_text()
    assert 'href="https://site.test/news/c"' in s7  # not archived: points to the live site
    assert files["/news/a"].startswith(f"web/job{job['id']}/")
    # the post's look is kept: stylesheet, @imported CSS, font, background and image saved locally
    assets = tmp_path / f"web/job{job['id']}/assets"
    assert sorted(p.name.rsplit(".", 1)[1] for p in assets.iterdir()) == ["css", "css", "png", "png", "woff2"]
    main_css = next(p for p in assets.iterdir() if b"@import" in p.read_bytes()).read_text()
    assert main_css.startswith('@import "') and 'url("' in main_css and "site.test" not in main_css
    assert (assets / main_css.split('"')[1]).read_text().count("url(") == 1
    assert 'href="assets/' in a and 'src="assets/' in a and "<script" not in a
    assert '<meta charset="utf-8">' in a
    row = await pool.fetchrow("SELECT assets_saved, assets_bytes, pages_bytes FROM web_archive_jobs WHERE id = $1", job["id"])
    assert row["assets_saved"] == 5 and row["assets_bytes"] > 0 and row["pages_bytes"] > 0


async def test_max_posts_and_cancel(pool, tmp_path):
    arch = _archiver(pool, tmp_path)
    news = {"prefix": "/news/", "url": SITE + "/news/"}
    job = await arch.start(SITE, [news], max_pages=2)
    await arch._tasks[job["id"]]
    assert await pool.fetchval("SELECT pages_saved FROM web_archive_jobs WHERE id = $1", job["id"]) == 2

    # menu items take turns: a small limit still gets posts from each
    job = await arch.start(SITE, [news, {"prefix": "/sport/", "url": SITE + "/sport/"}], max_pages=3)
    await arch._tasks[job["id"]]
    pages = await pool.fetch("SELECT url FROM web_archive_pages WHERE job_id = $1 ORDER BY id", job["id"])
    assert [p["url"].removeprefix(SITE) for p in pages] == ["/news/a", "/sport/x", "/story-7.html"]

    gate = asyncio.Event()

    async def slow(req):
        await gate.wait()
        return site_handler(req)

    arch = _archiver(pool, tmp_path, slow)
    job = await arch.start(SITE, [news], max_pages=20)
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

    r = await h.post("/api/archive/jobs", json={"url": SITE, "categories": [{"prefix": "/news/"}], "max_pages": 2,
                                                "save_images": False, "title": "Site & Co"})
    assert r.status_code == 202 and r.json()["title"] == "Site & Co"
    job_id = r.json()["id"]
    await app.state.archiver._tasks[job_id]
    assert (await h.post("/api/archive/jobs", json={"url": SITE, "categories": []})).status_code == 422

    jobs = (await h.get("/api/archive/jobs")).json()
    assert [(j["id"], j["status"], j["pages_saved"]) for j in jobs] == [(job_id, "done", 2)]
    detail = (await h.get(f"/api/archive/jobs/{job_id}")).json()
    assert [p["title"] for p in detail["pages"]] == ["A", "Story 7"]
    folder = tmp_path / "web" / f"job{job_id}"
    assert sorted(p.name.split("_")[0] for p in folder.iterdir() if p.is_file()) == ["icon.png", "news", "story-7"]
    assert jobs[0]["icon_path"] == f"web/job{job_id}/icon.png"
    assert sorted(p.suffix for p in (folder / "assets").iterdir()) == [".css", ".css", ".woff2"]  # no images

    assert (await h.delete(f"/api/archive/jobs/{job_id}")).status_code == 200
    assert not folder.exists() and (await h.get(f"/api/archive/jobs/{job_id}")).status_code == 404


async def test_interrupted_on_restart(pool, tmp_path):
    await pool.execute("INSERT INTO web_archive_jobs (url, categories, max_pages, status) VALUES ($1, '[]', 5, 'running')", SITE)
    await _archiver(pool, tmp_path).mark_interrupted()
    assert await pool.fetchval("SELECT status FROM web_archive_jobs") == "interrupted"


async def test_rerun_replaces_pages(api, pool, tmp_path):  # noqa: F811
    app.state.archiver = arch = _archiver(pool, tmp_path)
    h = api.http
    job = await arch.start(SITE, [{"prefix": "/sport/", "url": SITE + "/sport/"}], max_pages=5, save_images=False)
    await arch._tasks[job["id"]]
    folder = tmp_path / "web" / f"job{job['id']}"
    stale = folder / "stale_00000000.html"  # e.g. a page the site no longer has
    stale.write_text("old")
    old_ids = [p["id"] for p in (await h.get(f"/api/archive/jobs/{job['id']}")).json()["pages"]]

    r = await h.post(f"/api/archive/jobs/{job['id']}/rerun")
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["pages_saved"] == 0
    assert r.json()["save_images"] is False and r.json()["categories"][0]["prefix"] == "/sport/"
    assert (await h.post(f"/api/archive/jobs/{job['id']}/rerun")).status_code == 409  # still running
    await arch._tasks[job["id"]]

    detail = (await h.get(f"/api/archive/jobs/{job['id']}")).json()
    assert detail["status"] == "done" and detail["pages_saved"] == 1  # same id, fresh pages
    assert [p["title"] for p in detail["pages"]] == ["X"]
    assert not set(old_ids) & {p["id"] for p in detail["pages"]}
    assert not stale.exists() and all((tmp_path / p["file_path"]).exists() for p in detail["pages"])
    assert len((await h.get("/api/archive/jobs")).json()) == 1
    assert (await h.post("/api/archive/jobs/999/rerun")).status_code == 404


async def test_no_posts_found_is_reported(pool, tmp_path):
    def handler(req):
        if req.url.path == "/empty/":
            return httpx.Response(200, content=b"<title>Empty</title><div id=app></div><script>render()</script>",
                                  headers={"content-type": "text/html"})
        return site_handler(req)

    arch = _archiver(pool, tmp_path, handler)
    job = await arch.start(SITE, [{"prefix": "/empty/", "url": SITE + "/empty/"}], max_pages=5)
    await arch._tasks[job["id"]]
    row = await pool.fetchrow("SELECT status, error FROM web_archive_jobs WHERE id = $1", job["id"])
    assert row["status"] == "failed" and row["error"].startswith("no posts found")


async def test_discover_js_only_site(tmp_path):
    body = b'<html><body><script>document.cookie="x=1";window.location.reload(true);</script></body></html>'
    arch = WebArchiver(None, tmp_path, check_host=_public, client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body, headers={"content-type": "text/html"}))))
    with pytest.raises(ArchiveError, match="JavaScript"):
        await arch.discover(SITE)


async def test_backfill_icons_for_old_archives(pool, tmp_path):
    def handler(req):
        if req.url.path == "/favicon.ico":
            return httpx.Response(200, content=b"\x00\x00\x01\x00ico", headers={"content-type": "image/x-icon"})
        if req.url.path == "/":
            return httpx.Response(200, content=b"<title>Old</title>", headers={"content-type": "text/html"})
        return httpx.Response(404)

    old = await pool.fetchval("INSERT INTO web_archive_jobs (url, categories, max_pages, status) "
                              "VALUES ($1, '[]', 5, 'done') RETURNING id", SITE + "/")
    running = await pool.fetchval("INSERT INTO web_archive_jobs (url, categories, max_pages, status) "
                                  "VALUES ($1, '[]', 5, 'running') RETURNING id", SITE + "/")
    arch = _archiver(pool, tmp_path, handler)
    await arch.backfill_icons()
    rows = {r["id"]: r for r in await pool.fetch("SELECT id, icon_path, icon_checked FROM web_archive_jobs")}
    assert rows[old]["icon_path"] == f"web/job{old}/icon.ico" and rows[old]["icon_checked"]
    assert rows[running]["icon_path"] is None and not rows[running]["icon_checked"]  # its crawl does it
    # looked for once only, even when none was found
    await pool.execute("UPDATE web_archive_jobs SET icon_path = NULL WHERE id = $1", old)
    await arch.backfill_icons()
    assert await pool.fetchval("SELECT icon_path FROM web_archive_jobs WHERE id = $1", old) is None
