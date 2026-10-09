import re

import httpx
import pytest

from app import archive
from app import main as main_mod
from app.archive import ArchiveError, archive_website

from .test_api import api  # noqa: F401  (fixture)

PAGE = b"<html><head><title> Hello &amp; welcome </title></head><body><img src=a.png></body></html>"


async def _public(host):
    pass


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_saves_html_with_base_and_title(tmp_path):
    def handler(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new/page"})
        return httpx.Response(200, content=PAGE, headers={"content-type": "text/html; charset=utf-8"})

    async with _client(handler) as c:
        a = await archive_website("https://example.com/old", tmp_path, client=c, check_host=_public)
    assert a.final_url == "https://example.com/new/page" and a.status == 200
    assert a.title == "Hello & welcome"
    assert re.fullmatch(r"web/\d{8}-\d{6}_example\.com_[0-9a-f]{8}\.html", a.rel_path)
    saved = (tmp_path / a.rel_path).read_bytes()
    assert saved.startswith(b'<html><head><base href="https://example.com/new/page"><title>')
    assert a.size == len(saved)


async def test_non_html_kept_as_is(tmp_path):
    async with _client(lambda r: httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})) as c:
        a = await archive_website("http://example.com/doc", tmp_path, client=c, check_host=_public)
    assert a.rel_path.endswith(".pdf") and a.title is None
    assert (tmp_path / a.rel_path).read_bytes() == b"%PDF-1.4"


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "not a url"])
async def test_rejects_non_http(tmp_path, url):
    with pytest.raises(ArchiveError, match="http"):
        await archive_website(url, tmp_path, check_host=_public)


async def test_rejects_private_hosts(tmp_path):
    for url in ("http://127.0.0.1:5432/", "http://[::1]/", "http://10.0.0.5/", "http://localhost/"):
        with pytest.raises(ArchiveError, match="not a public address"):
            await archive_website(url, tmp_path)


async def test_redirect_to_private_host_refused(tmp_path):
    checked = []

    async def check(host):
        checked.append(host)
        if host == "internal":
            raise ArchiveError("internal is not a public address")

    async with _client(lambda r: httpx.Response(302, headers={"location": "http://internal/admin"})) as c:
        with pytest.raises(ArchiveError, match="not a public"):
            await archive_website("https://example.com/", tmp_path, client=c, check_host=check)
    assert checked == ["example.com", "internal"]


async def test_errors(tmp_path):
    async with _client(lambda r: httpx.Response(404)) as c:
        with pytest.raises(ArchiveError, match="HTTP 404"):
            await archive_website("https://example.com/", tmp_path, client=c, check_host=_public)
    async with _client(lambda r: httpx.Response(200, content=b"x" * 100)) as c:
        with pytest.raises(ArchiveError, match="larger"):
            await archive_website("https://example.com/", tmp_path, client=c, check_host=_public, max_bytes=50)
    async with _client(lambda r: httpx.Response(302, headers={"location": "/loop"})) as c:
        with pytest.raises(ArchiveError, match="too many redirects"):
            await archive_website("https://example.com/", tmp_path, client=c, check_host=_public)
    assert not (tmp_path / "web").exists()


async def test_api(api, pool, tmp_path, monkeypatch):  # noqa: F811
    real = archive.archive_website

    async def fake(url, media_dir):
        async with _client(lambda r: httpx.Response(200, content=PAGE, headers={"content-type": "text/html"})) as c:
            return await real(url, tmp_path, client=c, check_host=_public)

    monkeypatch.setattr(main_mod, "archive_website", fake)
    r = await api.http.post("/api/archive", json={"url": "https://example.com/a"})
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "Hello & welcome" and body["final_url"] == "https://example.com/a"
    assert (tmp_path / body["file_path"]).exists()

    assert (await api.http.post("/api/archive", json={"url": "ftp://x"})).status_code == 400
    listed = (await api.http.get("/api/archive")).json()
    assert [a["id"] for a in listed] == [body["id"]]
