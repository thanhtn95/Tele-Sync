"""Fill in .env interactively: asks for each setting, Enter keeps the current value.

    sudo telesync config          (installed by setup.sh)
    cd backend && sudo ../venv/bin/python -m scripts.configure

Web login, Telegram login and the Google refresh token have their own commands
(telesync password / tg-login / gphotos-auth) because they are not plain values.
"""
import os
import re
import sys

from scripts.set_password import ENV_PATH, upsert

# (key, question, validator or None, secret?)
FIELDS = [
    ("TG_API_ID", "Telegram api_id (my.telegram.org -> API development tools)",
     lambda v: v.isdigit() or "must be a number, e.g. 13851320", False),
    ("TG_API_HASH", "Telegram api_hash",
     lambda v: bool(re.fullmatch(r"[0-9a-f]{32}", v)) or "must be 32 characters of 0-9 / a-f", True),
    ("GOOGLE_CLIENT_ID", "Google OAuth client ID (Desktop app)",
     lambda v: v.endswith(".apps.googleusercontent.com") or "should end with .apps.googleusercontent.com", False),
    ("GOOGLE_CLIENT_SECRET", "Google OAuth client secret", None, True),
    ("GCS_BUCKET", "Backup bucket, e.g. gs://my-telesync-backups (optional)",
     lambda v: v.startswith("gs://") or "must start with gs://", False),
    ("SYNC_INTERVAL_SECONDS", "Seconds between automatic syncs (0 = only manual)",
     lambda v: v.isdigit() or "must be a number", False),
    ("MAX_FILE_MB", "Skip files larger than this many MB (0 = no limit)",
     lambda v: v.isdigit() or "must be a number", False),
]


def current(text: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def shown(value: str, secret: bool) -> str:
    if not value:
        return "not set"
    if secret and len(value) > 8:
        return value[:4] + "…" + value[-4:]
    return value


def ask(key: str, question: str, check, secret: bool, now: str) -> str | None:
    """Returns the new value, or None to keep the current one."""
    while True:
        raw = input(f"\n{question}\n  {key} [{shown(now, secret)}]  (Enter = keep, '-' = clear): ").strip()
        raw = raw.strip("'\"")
        if not raw:
            return None
        if raw == "-":
            return ""
        if any(c.isspace() for c in raw):
            print("  ! no spaces allowed, try again")
            continue
        ok = check(raw) if check else True
        if ok is True:
            return raw
        print(f"  ! {ok}, try again")


def main() -> None:
    if not ENV_PATH.exists():
        sys.exit(f"{ENV_PATH} not found (run deploy/setup.sh first)")
    if not os.access(ENV_PATH, os.W_OK):
        sys.exit(f"cannot write {ENV_PATH}; run with sudo")
    only = [a.upper() for a in sys.argv[1:]]
    fields = [f for f in FIELDS if not only or f[0] in only]
    print(f"Editing {ENV_PATH}. Paste values and press Enter.")

    text = ENV_PATH.read_text()
    changed = []
    for key, question, check, secret in fields:
        value = ask(key, question, check, secret, current(text, key))
        if value is not None and value != current(text, key):
            text = upsert(text, key, value)
            changed.append(key)

    if not changed:
        print("\nNothing changed.")
        return
    ENV_PATH.write_text(text)  # in place: keeps owner/mode (root:telesync 640)
    print("\nSaved: " + ", ".join(changed))
    print("\nSummary:")
    for key, _, _, secret in FIELDS:
        print(f"  {key:22} {shown(current(text, key), secret)}")


if __name__ == "__main__":
    main()
