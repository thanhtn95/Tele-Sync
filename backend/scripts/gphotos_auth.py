"""Obtain a Google OAuth refresh token for the Photos Library API.

    cd backend && python -m scripts.gphotos_auth

Needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (OAuth client type "Desktop app")
in .env. Opens a loopback listener on localhost:8765 to receive the code.

Running on the headless VM? Forward the port from your machine first:
    ssh -L 8765:localhost:8765 <vm>
then open the printed URL in your local browser.

Remember: set the OAuth consent screen's publishing status to "In production",
otherwise Google expires the refresh token after 7 days.
"""
import http.server
import secrets
import urllib.parse

import httpx

from app.config import settings
from app.gphotos import SCOPES, TOKEN_URL

PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"


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

    print("Open this URL in your browser and grant access:\n\n" + url + "\n")
    with http.server.HTTPServer(("127.0.0.1", PORT), Handler) as srv:
        while not result:
            srv.handle_request()

    if "error" in result:
        raise SystemExit(f"Authorisation failed: {result['error']}")
    if result.get("state") != state:
        raise SystemExit("State mismatch; aborting")
    r = httpx.post(TOKEN_URL, data={
        "code": result["code"],
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    })
    r.raise_for_status()
    token = r.json().get("refresh_token")
    if not token:
        raise SystemExit(f"No refresh_token in response: {r.text}")
    print("\nAdd this line to your .env:\n")
    print(f"GOOGLE_REFRESH_TOKEN={token}")


if __name__ == "__main__":
    main()
