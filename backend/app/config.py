"""Settings loaded from environment / .env (repo root or backend dir)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent.parent
# Repo-root .env first, then backend/.env; real env vars always win.
load_dotenv(_BACKEND_DIR.parent / ".env")
load_dotenv(_BACKEND_DIR / ".env")


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name) or default).expanduser()
    if not p.is_absolute():
        p = (_BACKEND_DIR.parent / p).resolve()
    return p


@dataclass(frozen=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    tg_session_path: Path
    database_url: str
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    media_dir: Path
    temp_dir: Path
    sync_interval_seconds: int
    max_file_bytes: int
    web_username: str
    web_password_hash: str
    session_secret: str

    @property
    def auth_enabled(self) -> bool:
        return bool(self.web_password_hash)

    @property
    def gphotos_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_refresh_token)


def load_settings() -> Settings:
    max_mb = int(os.environ.get("MAX_FILE_MB") or 2000)
    return Settings(
        tg_api_id=int(os.environ.get("TG_API_ID") or 0),
        tg_api_hash=os.environ.get("TG_API_HASH", ""),
        tg_session_path=_path("TG_SESSION_PATH", "./data/telegram.session"),
        database_url=os.environ.get("DATABASE_URL", "postgresql://telesync@127.0.0.1:5432/telesync"),
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        google_refresh_token=os.environ.get("GOOGLE_REFRESH_TOKEN", ""),
        media_dir=_path("MEDIA_DIR", "./data/files"),
        temp_dir=_path("TEMP_DIR", "./data/tmp"),
        sync_interval_seconds=int(os.environ.get("SYNC_INTERVAL_SECONDS") or 1800),
        max_file_bytes=max_mb * 1024 * 1024,
        web_username=os.environ.get("WEB_USERNAME") or "admin",
        web_password_hash=os.environ.get("WEB_PASSWORD_HASH", ""),
        session_secret=os.environ.get("SESSION_SECRET", ""),
    )


settings = load_settings()
