import dataclasses

import pytest

from app import auth
from app import main as main_mod

from .fakes import CHAT_ID
from .test_api import api  # noqa: F401  (fixture)
from .test_sync import _add_chat


def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse")
    assert "$" not in h and h.startswith("scrypt:")
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")


def test_token():
    t = auth.make_token("me", "s3cret", "hash1")
    assert auth.check_token(t, "me", "s3cret", "hash1")
    assert not auth.check_token(t, "other", "s3cret", "hash1")
    assert not auth.check_token(t, "me", "s3cret", "hash2")  # password changed
    assert not auth.check_token(t + "x", "me", "s3cret", "hash1")
    assert not auth.check_token(auth.make_token("me", "s3cret", "hash1", ttl=-1), "me", "s3cret", "hash1")
    assert not auth.check_token(None, "me", "s3cret", "hash1")


def test_throttle():
    t = auth.LoginThrottle(free_attempts=2)
    t.failed()
    assert t.retry_after() == 0
    t.failed()
    assert t.retry_after() > 0
    t.succeeded()
    assert t.retry_after() == 0


@pytest.fixture
def login_on(monkeypatch):
    s = dataclasses.replace(main_mod.settings, web_username="me", web_password_hash=auth.hash_password("pw123456"))
    monkeypatch.setattr(main_mod, "settings", s)
    monkeypatch.setattr(main_mod, "_throttle", auth.LoginThrottle())


async def test_auth_flow(api, pool, login_on):  # noqa: F811
    await _add_chat(pool)
    h = api.http
    assert (await h.get("/api/health")).status_code == 200
    assert (await h.get("/api/dialogs")).status_code == 401
    assert (await h.get(f"/api/chats/{CHAT_ID}/messages")).status_code == 401
    assert (await h.get("/files/avatars/1.jpg")).status_code == 401
    assert (await h.get("/api/auth/check")).status_code == 401
    assert (await h.get("/api/auth/me")).json() == {"authenticated": False, "auth_enabled": True, "username": None}

    r = await h.post("/api/auth/login", json={"username": "me", "password": "nope"})
    assert r.status_code == 401 and "ts_session" not in r.cookies

    r = await h.post("/api/auth/login", json={"username": "me", "password": "pw123456"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert (await h.get(f"/api/chats/{CHAT_ID}")).status_code == 200
    assert (await h.get("/api/auth/check")).status_code == 204
    assert (await h.get("/api/auth/me")).json()["username"] == "me"

    await h.post("/api/auth/logout")
    h.cookies.clear()
    assert (await h.get(f"/api/chats/{CHAT_ID}")).status_code == 401


async def test_login_lockout(api, login_on):  # noqa: F811
    for _ in range(5):
        await api.http.post("/api/auth/login", json={"username": "me", "password": "bad"})
    r = await api.http.post("/api/auth/login", json={"username": "me", "password": "pw123456"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


async def test_login_disabled_by_default(api):  # noqa: F811
    assert (await api.http.get("/api/auth/me")).json()["auth_enabled"] is False
    assert (await api.http.get("/api/sync/status")).status_code == 200
