"""Obtain a Google OAuth refresh token for the Photos Library API.

    cd /opt/tele-sync/backend && sudo ../venv/bin/python -m scripts.gphotos_auth

Needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (OAuth client type "Desktop app")
in .env. Works from any device, including a phone:

  1. Open the printed link and allow access.
  2. Google then redirects to http://localhost:8765/?... which fails to load
     ("site can't be reached"). That's expected: copy the whole address from the
     browser's address bar and paste it into the terminal.

The refresh token is saved into .env (needs sudo); otherwise it is printed.

--listen: instead of pasting, receive the redirect on a local listener
(only useful when the browser runs on this machine, or via ssh -L 8765:localhost:8765).

Remember: set the OAuth consent screen's publishing status to "In production",
otherwise Google expires the refresh token after 7 days.
"""
import http.server
import os
import secrets
import sys
import urllib.parse

import httpx

from app.config import settings
from app.gphotos import SCOPES, TOKEN_URL
from scripts.set_password import ENV_PATH, upsert

PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"


def parse_redirect(pasted: str) -> dict[str, str]:
    """Accept the full redirect URL, just its query string, or a bare code."""
    text = pasted.strip().strip('"\'')
    if not text:
        return {}
    if "code=" in text or "error=" in text:
        # With or without "http://" (some phone browsers copy it without the scheme).
        query = text.split("?", 1)[1] if "?" in text else text
        return dict(urllib.parse.parse_qsl(query))
    return {"code": text}


def listen() -> dict[str, str]:
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" not in qs and "error" not in qs:
                self.send_response(404)
                self.end_headers()
                return
            result.update(qs)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done - you can close this tab and return to the terminal.")

        def log_message(self, *a):
            pass

    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as srv:
        while not result:
            srv.handle_request()
    return result


def main() -> None:
    if not settings.google_client_id or not settings.google_client_secret:
        raise SystemExit("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first")
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # always return a refresh token
        "state": state,
    })
    print("1. Open this link in a browser (phone is fine) and allow access:\n\n" + url + "\n")

    if "--listen" in sys.argv:
        result = listen()
    else:
        print("2. Google then opens a page at http://localhost:8765/... that does NOT load.")
        print("   That's expected. Copy the whole address from the address bar and paste it here.\n")
        while True:
            result = parse_redirect(input("Paste address: "))
            if result:
                break
            print("Nothing pasted, try again.")

    if "error" in result:
        raise SystemExit(f"Authorisation failed: {result['error']}")
    if "state" in result and result["state"] != state:
        raise SystemExit("That address belongs to an older attempt (state mismatch). Run the script again.")
    r = httpx.post(TOKEN_URL, data={
        "code": result["code"],
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    })
    if r.status_code != 200:
        raise SystemExit(f"Token exchange failed ({r.status_code}): {r.text[:300]}\n"
                         "Codes expire within minutes and work once: run the script again.")
    token = r.json().get("refresh_token")
    if not token:
        raise SystemExit(f"No refresh_token in response: {r.text[:300]}")

    if ENV_PATH.exists() and os.access(ENV_PATH, os.W_OK):
        ENV_PATH.write_text(upsert(ENV_PATH.read_text(), "GOOGLE_REFRESH_TOKEN", token))
        print(f"\nSaved GOOGLE_REFRESH_TOKEN to {ENV_PATH}.")
        print("Now run: sudo systemctl restart telesync")
    else:
        print("\nCould not write .env (run with sudo to save it automatically). Add this line to it:\n")
        print(f"GOOGLE_REFRESH_TOKEN={token}")


if __name__ == "__main__":
    main()
