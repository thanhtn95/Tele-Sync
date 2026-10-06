"""Set the web login (single account). Writes WEB_USERNAME / WEB_PASSWORD_HASH /
SESSION_SECRET into the repo-root .env, then restart the service.

    cd /opt/tele-sync/backend && sudo ../venv/bin/python -m scripts.set_password
    sudo systemctl restart telesync

Changing the password logs out all existing sessions.
"""
import getpass
import os
import re
import secrets
import sys
from pathlib import Path

from app.auth import hash_password

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def upsert(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pat.search(text):
        return pat.sub(lambda _: line, text)
    return text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"


def main() -> None:
    if not ENV_PATH.exists():
        sys.exit(f"{ENV_PATH} not found (copy .env.example first)")
    if not os.access(ENV_PATH, os.W_OK):
        sys.exit(f"cannot write {ENV_PATH}; run with sudo")
    text = ENV_PATH.read_text()
    current = re.search(r"^WEB_USERNAME=(.*)$", text, re.M)
    default_user = (current.group(1).strip() if current else "") or "admin"
    username = input(f"Username [{default_user}]: ").strip() or default_user
    while True:
        pw = getpass.getpass("Password (min 8 chars): ")
        if len(pw) < 8:
            print("Too short.")
            continue
        if getpass.getpass("Repeat password: ") != pw:
            print("Passwords differ.")
            continue
        break
    text = upsert(text, "WEB_USERNAME", username)
    text = upsert(text, "WEB_PASSWORD_HASH", hash_password(pw))
    if not re.search(r"^SESSION_SECRET=\S+", text, re.M):
        text = upsert(text, "SESSION_SECRET", secrets.token_urlsafe(32))
    ENV_PATH.write_text(text)  # in place: keeps owner/mode (root:telesync 640)
    print(f"Saved login for '{username}' to {ENV_PATH}.")
    print("Now run: sudo systemctl restart telesync")


if __name__ == "__main__":
    main()
