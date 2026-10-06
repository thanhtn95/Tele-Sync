"""One-time interactive Telegram login: creates the .session file.

    cd backend && python -m scripts.tg_login

Prompts for phone number, the login code Telegram sends you, and your 2FA
password if one is set. The resulting session file grants FULL access to your
account: keep it on the VM only (chmod 600), never commit it.
"""
import asyncio
import os

from app.config import settings
from app.tg import make_client


async def main() -> None:
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise SystemExit("Set TG_API_ID and TG_API_HASH in .env first (from https://my.telegram.org)")
    client = make_client(settings)
    await client.start()  # interactive: phone, code, 2FA password
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username}, id {me.id})")
    await client.disconnect()
    session_file = str(settings.tg_session_path)
    if not session_file.endswith(".session"):
        session_file += ".session"
    os.chmod(session_file, 0o600)
    print(f"Session saved to {session_file} (mode 600)")


if __name__ == "__main__":
    asyncio.run(main())
