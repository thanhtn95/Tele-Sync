import dataclasses
import urllib.parse

import pytest

from scripts import gphotos_auth as ga


def test_parse_redirect_variants():
    full = "http://localhost:8765/?state=abc&code=4/0Ab-x_Y&scope=https://www.googleapis.com/auth/photoslibrary.appendonly"
    assert ga.parse_redirect(full) == {
        "state": "abc", "code": "4/0Ab-x_Y",
        "scope": "https://www.googleapis.com/auth/photoslibrary.appendonly",
    }
    assert ga.parse_redirect("  'localhost:8765/?code=4%2F0Ab&state=s'  ")["code"] == "4/0Ab"
    assert ga.parse_redirect("?code=xyz&state=s") == {"code": "xyz", "state": "s"}
    assert ga.parse_redirect("4/0AbCdEf") == {"code": "4/0AbCdEf"}
    assert ga.parse_redirect("http://localhost:8765/?error=access_denied&state=s")["error"] == "access_denied"
    assert ga.parse_redirect("   ") == {}


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"refresh_token": "1//refresh-XYZ", "access_token": "a"}


def _run(monkeypatch, tmp_path, pasted_fn):
    env = tmp_path / ".env"
    env.write_text("GOOGLE_CLIENT_ID=cid\nGOOGLE_REFRESH_TOKEN=\nOTHER=1\n")
    monkeypatch.setattr(ga, "ENV_PATH", env)
    monkeypatch.setattr(ga, "settings", dataclasses.replace(
        ga.settings, google_client_id="cid", google_client_secret="sec"))
    sent = {}

    def fake_post(url, data):
        sent.update(data)
        return _Resp()

    monkeypatch.setattr(ga.httpx, "post", fake_post)
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    inputs = iter([""])  # first an empty paste -> asks again

    def fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            auth_url = next(p for p in printed if "accounts.google.com" in p)
            state = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url.split("\n\n")[1]).query)["state"][0]
            return pasted_fn(state)

    monkeypatch.setattr("builtins.input", fake_input)
    ga.main()
    return env.read_text(), sent


def test_main_saves_token_to_env(monkeypatch, tmp_path):
    text, sent = _run(monkeypatch, tmp_path, lambda st: f"http://localhost:8765/?state={st}&code=4/abc")
    assert sent["code"] == "4/abc" and sent["redirect_uri"] == "http://localhost:8765/"
    assert "GOOGLE_REFRESH_TOKEN=1//refresh-XYZ\n" in text and "OTHER=1" in text


def test_main_rejects_stale_state(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="older attempt"):
        _run(monkeypatch, tmp_path, lambda st: "http://localhost:8765/?state=old&code=4/abc")
