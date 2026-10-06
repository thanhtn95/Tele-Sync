"""Single-account web login: scrypt password hash + HMAC-signed session cookie.

Configured via .env (written by `python -m scripts.set_password`):
  WEB_USERNAME, WEB_PASSWORD_HASH, SESSION_SECRET
When WEB_PASSWORD_HASH is empty, login is disabled (Tailscale is the only gate).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

COOKIE_NAME = "ts_session"
SESSION_TTL = 30 * 24 * 3600

# scrypt cost: ~16 MB RAM, ~50 ms per hash; fine on an e2-micro.
_N, _R, _P = 2**14, 8, 1


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str) -> str:
    """Encoded as scrypt:N:r:p:salt:hash (no '$', so .env/systemd never expand it)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt:{_N}:{_R}:{_P}:{_b64(salt)}:{_b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, want = encoded.split(":")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                            dklen=len(_unb64(want)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, _unb64(want))


def _key(secret: str, password_hash: str) -> bytes:
    # Mixing in the password hash means changing the password logs out every session.
    return hashlib.sha256(f"{secret}\0{password_hash}".encode()).digest()


def make_token(username: str, secret: str, password_hash: str, ttl: int = SESSION_TTL) -> str:
    body = _b64(json.dumps({"u": username, "exp": int(time.time()) + ttl}).encode())
    sig = _b64(hmac.new(_key(secret, password_hash), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def check_token(token: str | None, username: str, secret: str, password_hash: str) -> bool:
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    good = _b64(hmac.new(_key(secret, password_hash), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        return False
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return False
    return data.get("u") == username and int(data.get("exp", 0)) > time.time()


class LoginThrottle:
    """Global back-off after repeated failures (single account, so global is right)."""

    def __init__(self, free_attempts: int = 5, max_lock: int = 300):
        self.free = free_attempts
        self.max_lock = max_lock
        self.failures = 0
        self.locked_until = 0.0

    def retry_after(self) -> int:
        return max(0, int(self.locked_until - time.time() + 0.999))

    def failed(self) -> None:
        self.failures += 1
        if self.failures >= self.free:
            self.locked_until = time.time() + min(self.max_lock, 2 ** (self.failures - self.free + 1))

    def succeeded(self) -> None:
        self.failures = 0
        self.locked_until = 0.0
