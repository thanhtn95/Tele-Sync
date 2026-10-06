from scripts import configure


def _run(monkeypatch, tmp_path, answers, argv=()):
    env = tmp_path / ".env"
    env.write_text(
        "# comment kept\nTG_API_ID=123456\nTG_API_HASH=0123456789abcdef0123456789abcdef\n"
        "DATABASE_URL=postgresql://x\nGOOGLE_CLIENT_ID=\nWEB_PASSWORD_HASH=scrypt:keep\n"
    )
    monkeypatch.setattr(configure, "ENV_PATH", env)
    monkeypatch.setattr(configure.sys, "argv", ["configure", *argv])
    it = iter(answers)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(it)

    out = []
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    configure.main()
    return env.read_text(), prompts, "\n".join(out)


def test_fill_validate_keep_clear(monkeypatch, tmp_path):
    answers = [
        "abc", " 13851320 ",                      # TG_API_ID: invalid, then valid (trimmed)
        "-",                                       # TG_API_HASH: clear
        "123.apps.googleusercontent.com",          # GOOGLE_CLIENT_ID
        "'GOCSPX-secret value'", "GOCSPX-secret",  # secret: spaces rejected, then ok (quotes stripped)
        "my-bucket", "gs://my-bucket",             # GCS_BUCKET: must start gs://
        "",                                        # SYNC_INTERVAL_SECONDS: keep (absent)
        "",                                        # MAX_FILE_MB: keep
    ]
    text, prompts, out = _run(monkeypatch, tmp_path, answers)
    assert "TG_API_ID=13851320\n" in text
    assert "TG_API_HASH=\n" in text
    assert "GOOGLE_CLIENT_ID=123.apps.googleusercontent.com\n" in text
    assert "GOOGLE_CLIENT_SECRET=GOCSPX-secret\n" in text
    assert "GCS_BUCKET=gs://my-bucket\n" in text
    assert "SYNC_INTERVAL_SECONDS" not in text and "MAX_FILE_MB" not in text
    # untouched lines survive
    assert text.startswith("# comment kept\n")
    assert "DATABASE_URL=postgresql://x\n" in text and "WEB_PASSWORD_HASH=scrypt:keep\n" in text
    # secrets are masked when shown
    assert "0123…cdef" in prompts[2] and "0123456789abcdef0123456789abcdef" not in out
    assert "must be a number" in out and "no spaces" in out


def test_only_selected_keys(monkeypatch, tmp_path):
    text, prompts, _ = _run(monkeypatch, tmp_path, ["999"], argv=["tg_api_id"])
    assert len(prompts) == 1 and "TG_API_ID=999\n" in text


def test_nothing_changed(monkeypatch, tmp_path):
    text, _, out = _run(monkeypatch, tmp_path, [""] * 7)
    assert "Nothing changed" in out
