import asyncio

import httpx
import pytest

from app import archive
from app.archive import ArchiveError, Page, WebArchiver, fetch_page, file_name, find_categories, rewrite_links
from app.main import app

from .test_api import api  # noqa: F401  (fixture)

SITE = "https://site.test"

HOME = b"""<html><head><title> Site &amp; Co </title></head><body>
<nav class="main"><ul>
  <li><a href="/news/">News</a>
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
    "/news/a": b'<html><head><title>A</title></head><body><a href="/news/b#top">B</a>'
               b'<img src="../img/a.png"><a href="/sport/x">x</a></body></html>',
    "/news/b": b'<title>B</title><a href="a">A</a>',
    "/sport/": b"<title>Sport</title>",
    "/sport/x": b"<title>X</title>",
}


def site_handler(req: httpx.Request) -> httpx.Response:
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


def test_find_categories():
    title, cats = find_categories(Page(SITE, SITE + "/", 200, "text/html", HOME))
    assert title == "Site & Co"
    by = {c["prefix"]: c for c in cats}
    assert set(by) == {"/news/", "/sport/", "/category/tech/"}  # no files, other sites or single pages
    assert [c["prefix"] for c in cats[:3]] == ["/news/", "/sport/", "/category/tech/"]  # menu first, by links
    assert by["/news/"] == {"prefix": "/news/", "label": "News", "url": SITE + "/news/", "links": 3, "in_nav": True}
    assert by["/sport/"]["url"] == SITE + "/sport" and by["/sport/"]["label"] == "Sport"


def test_rewrite_links():
    local = {SITE + "/news/b": "news_b_1.html"}
    out = rewrite_links('<base href="/x/"><a href="/news/b#top">b</a> <img src=\'i.png\'> '
                        '<a href="?q=1&amp;p=2">q</a> <a href="#s">s</a>', SITE + "/news/a", local)
    assert out == ('<a href="news_b_1.html#top">b</a> <img src="https://site.test/news/i.png"> '
                   '<a href="https://site.test/news/a?q=1&amp;p=2">q</a> <a href="#s">s</a>')


def test_rewrite_drops_scripts_and_unlazies_images():
    out = rewrite_links('<script src="/a.js"></script><p>x</p><SCRIPT>alert(1)</SCRIPT>'
                        '<img src="data:image/gif;base64,R0" data-src="/big.jpg" alt="b"><img src="/c.png">',
                        SITE + "/news/a", {})
    assert out == ('<p>x</p><img src="https://site.test/big.jpg" data-src="/big.jpg" alt="b">'
                   '<img src="https://site.test/c.png">')
    out = rewrite_links('<img srcset="tiny.jpg 1x" data-srcset="https://cdn.test/a.jpg 1x, https://cdn.test/b.jpg 2x">',
                        SITE, {})
    assert out == '<img srcset="https://cdn.test/a.jpg 1x, https://cdn.test/b.jpg 2x" data-srcset="https://cdn.test/a.jpg 1x, https://cdn.test/b.jpg 2x">'


def test_file_name_is_stable():
    assert file_name(SITE + "/news/a", "text/html") == file_name(SITE + "/news/a", None)
    assert file_name(SITE + "/", "text/html").startswith("index_")
    assert file_name(SITE + "/doc.pdf", "application/pdf").endswith(".pdf")


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
    news = next(c for c in found["categories"] if c["prefix"] == "/news/")

    job = await arch.start(found["url"], [news], max_pages=20)
    await arch._tasks[job["id"]]
    row = await pool.fetchrow("SELECT * FROM web_archive_jobs WHERE id = $1", job["id"])
    assert row["status"] == "done" and row["title"] == "Site & Co"
    pages = await pool.fetch("SELECT * FROM web_archive_pages WHERE job_id = $1 ORDER BY id", job["id"])
    saved = [p["url"].removeprefix(SITE) for p in pages if p["file_path"]]
    # /story-7.html and /sport/x: linked from News pages, so saved, but not crawled on (no /news/c);
    # not /sport/: only in the menu; not /privacy: footer
    # articles found on a listing page come before more listing pages
    assert saved == ["/", "/news/", "/story-7.html", "/news/a", "/sport/x", "/news/b", "/news/page/2/"]
    assert [(p["url"].removeprefix(SITE), p["error"]) for p in pages if p["error"]] == [
        ("/news/secret", "blocked by robots.txt")]
    assert (row["pages_saved"], row["pages_failed"]) == (7, 1)

    # links between saved pages point to the local copies, the rest to the live site
    files = {p["url"].removeprefix(SITE): p["file_path"] for p in pages if p["file_path"]}
    a = (tmp_path / files["/news/a"]).read_text()
    b_name = files["/news/b"].rsplit("/", 1)[1]
    assert f'href="{b_name}#top"' in a
    assert 'src="https://site.test/img/a.png"' in a
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
    assert r.status_code == 200 and r.json()["categories"][0]["label"] == "News"
    assert (await h.post("/api/archive/discover", json={"url": "ftp://x"})).status_code == 400

    r = await h.post("/api/archive/jobs", json={"url": SITE, "categories": [{"prefix": "/sport/"}], "max_pages": 5})
    assert r.status_code == 202
    job_id = r.json()["id"]
    await app.state.archiver._tasks[job_id]
    assert (await h.post("/api/archive/jobs", json={"url": SITE, "categories": []})).status_code == 422

    jobs = (await h.get("/api/archive/jobs")).json()
    assert [(j["id"], j["status"], j["pages_saved"]) for j in jobs] == [(job_id, "done", 3)]  # home, /sport/ and /sport/x (linked from home)
    detail = (await h.get(f"/api/archive/jobs/{job_id}")).json()
    assert [p["title"] for p in detail["pages"]] == ["Site & Co", "Sport", "X"]
    folder = tmp_path / "web" / f"job{job_id}"
    assert len(list(folder.iterdir())) == 3

    assert (await h.delete(f"/api/archive/jobs/{job_id}")).status_code == 200
    assert not folder.exists() and (await h.get(f"/api/archive/jobs/{job_id}")).status_code == 404


async def test_interrupted_on_restart(pool, tmp_path):
    await pool.execute("INSERT INTO web_archive_jobs (url, categories, max_pages, status) VALUES ($1, '[]', 5, 'running')", SITE)
    await _archiver(pool, tmp_path).mark_interrupted()
    assert await pool.fetchval("SELECT status FROM web_archive_jobs") == "interrupted"
