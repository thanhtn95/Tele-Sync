# Tele-Sync — personal Telegram archiver

Syncs messages and media from **your own** Telegram account (only the chats you pick)
into PostgreSQL, uploads photos/videos to Google Photos, and shows the archive in a
Telegram-style web viewer. Single user: reachable only over Tailscale, behind a single-account web login.

```
backend/    FastAPI + Telethon (MTProto user API) + asyncpg; sync worker runs in-process
frontend/   React (Vite) + react-virtuoso viewer, built to static files served by nginx
deploy/     e2-micro provisioning, systemd unit, nginx site, Postgres tuning, backups
```

## How it works

* **Chat list** (`#/`): every dialog from `GET /api/dialogs` (cached in the `chats` table;
  "Refresh chats" re-reads Telegram). Each row's toggle is `sync_enabled`, saved instantly
  via `PATCH /api/chats/{id}`. The gear opens per-chat settings: *include media* and
  *sync since* date. The status panel polls `GET /api/sync/status`.
* **Sync worker**: a single asyncio task. Every `SYNC_INTERVAL_SECONDS` (or on
  "Sync now" / `POST /api/sync/run`, or when a chat is enabled) it walks chats with
  `sync_enabled`, calling `iter_messages(min_id=last_msg_id, reverse=True, offset_date=sync_since)`.
  Messages are upserted (`ON CONFLICT … DO UPDATE`, so re-fetches capture edits), senders
  go to `users` (avatar downloaded once), and the cursor `last_msg_id` is persisted every
  50 messages, so a crash or restart resumes where it stopped. `FloodWaitError` →
  checkpoint, sleep `e.seconds`, continue.
* **Media**: metadata (size, dimensions, duration, stripped thumbnail) is always stored.
  When *include media* is on:
  * photos / videos / GIFs → downloaded to `TEMP_DIR`, uploaded to Google Photos
    (raw upload + `mediaItems:batchCreate` in batches of ≤50, per-item handling of HTTP 207),
    into one album per chat, then the temp file is deleted. Only `gphotos_media_id` is stored.
  * voice, audio, stickers (`.webp`/`.webm`/`.tgs`), documents → `MEDIA_DIR/<chat>/<msg>…`,
    served by nginx at `/files/`.
  * Without Google Photos credentials, photos/videos are stored on disk as well.
  * Failed downloads/uploads are recorded in `media.error` and retried on later passes
    (≤100 per chat per pass). Turning *include media* on later backfills older messages.
    Files over `MAX_FILE_MB` are skipped.
* **Viewer** (`#/chat/<id>`): virtualized reverse-scrolling list that loads older pages
  as you scroll up. Date separators, left/right bubbles, sender names + avatars in groups,
  formatted text (bold/italic/code/pre/links/mentions/spoilers…), reply previews (click to
  jump, loading older pages if needed), "Forwarded from", albums as one grid bubble,
  blurred stripped-thumbnail placeholders, click-to-open full image (`=d`) / video (`=dv`).
  baseUrls are fetched in batches from `POST /api/gphotos/urls`, never persisted, and
  re-fetched when an image fails to load (expired).
* **Browsing**: the loaded messages are a window that can sit anywhere in history, so
  jumping to a reply or pin (even years back) loads just the messages around it; scrolling
  loads older/newer pages and ↓ returns to the latest. Each chat has **Media / Files /
  Voice** tabs (`#/chat/<id>/media` …), and the photo/video viewer steps through the whole
  chat with ‹ ›, ←/→ or a swipe, with "Show in chat".
* **Theme**: the button in the header switches System → Light → Dark (Telegram's
  night colours); the choice is remembered per browser, and "System" follows the OS live.
* **Pinned messages** mirror Telegram: every pass re-reads the chat's pinned list, so
  pins/unpins of old messages are picked up, and pinned messages older than *sync since*
  are archived too. The viewer has Telegram's pinned bar (shows the pin above your scroll
  position; click to jump, then it steps to the next older pin), a list of all pins, a 📌
  on pinned bubbles, and clickable "X pinned «…»" service messages.

## API

| Method | Path | |
|---|---|---|
| POST | `/api/auth/login` `{username, password}` / `/api/auth/logout` | session cookie (30 days) |
| GET | `/api/auth/me` | login state |
| GET | `/api/dialogs[?refresh=true]` | chats (from DB; `refresh` re-reads Telegram, preserves sync flags) |
| GET / PATCH | `/api/chats/{chat_id}` | one chat / update `sync_enabled`, `sync_media`, `sync_since` |
| GET | `/api/chats/{chat_id}/messages?before=<id>&limit=50` | newest first, with media, sender, reply preview |
| GET | `/api/chats/{chat_id}/messages?after=<id>` / `?around=<id>` | newer page / window around a message |
| GET | `/api/chats/{chat_id}/media?group=media\|files\|voice&before=&after=` | shared media tabs, viewer neighbours |
| GET | `/api/chats/{chat_id}/pinned` | currently pinned messages, newest first |
| POST | `/api/gphotos/urls` `{media_ids, force?}` | fresh baseUrls (batchGet ≤50/call) |
| GET | `/api/sync/status` | worker state + per-chat last sync / message count / error |
| POST | `/api/sync/run` | trigger a pass now |

Chat ids are Telethon "marked" ids (`-100…` for channels/supergroups). `grouped_id` is
returned as a string because it does not fit in a JS number.

## Setup

### 1. Credentials (manual, once)

* **Telegram**: create an app at <https://my.telegram.org> → *API development tools* →
  `TG_API_ID`, `TG_API_HASH`.
* **Google**: in a Google Cloud project enable the *Photos Library API*; configure the
  OAuth consent screen (External, add yourself as a user) with scopes
  `photoslibrary.appendonly` and `photoslibrary.readonly.appcreateddata`; **set publishing
  status to "In production"** (unverified is fine for personal use; in "Testing" refresh
  tokens die after 7 days). Create an OAuth client of type *Desktop app* →
  `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
  Note: since 2025‑03‑31 the Library API only reads items the app itself created,
  which is exactly what this app needs. Uploads count toward your 15 GB Google storage
  and the API cannot delete items.

### 2. VM (GCP Always Free)

```bash
PROJECT=my-project ./deploy/gcp-create-vm.sh          # e2-micro, us-central1, 30 GB pd-standard
gcloud compute ssh telesync --zone us-central1-a
git clone <this repo> /tmp/tele-sync && sudo bash /tmp/tele-sync/deploy/setup.sh
```

`setup.sh` adds a 2 GB swap file, installs Postgres (tuned for 1 GB RAM), nginx, Tailscale,
creates the DB + a random password, the `telesync` system user, `/opt/tele-sync` (code,
venv, `.env`) and `/var/lib/tele-sync` (session, files, temp, backups), builds the
frontend, installs the systemd unit, the nginx site (bound to `127.0.0.1:8080`) and a
nightly `pg_dump` cron (03:17 UTC, uploads to `GCS_BUCKET` if set). Re-run it to deploy
updates. If `npm` struggles on the VM, run `npm ci && npm run build` in `frontend/`
locally first; `setup.sh` uses an existing `frontend/dist`.

Then, on the VM (`setup.sh` installs the `telesync` admin command):

```bash
sudo telesync config        # asks for TG api_id/api_hash, Google client id/secret, backup bucket...
                            #   Enter keeps a value, '-' clears it; offers to restart the app
sudo telesync tg-login      # one-time Telegram login: phone, code from the Telegram app, 2FA
sudo telesync gphotos-auth  # link Google Photos: open the link (phone OK), allow, then paste the
                            #   address of the page that fails to load (http://localhost:8765/?code=...)
sudo telesync password      # change the web login
sudo tailscale up --ssh
sudo tailscale serve --bg 8080                        # https://<vm>.<tailnet>.ts.net
```

`sudo telesync restart | status | logs` for day-to-day checks.


### Security notes

* **Web login**: one account. `scripts/set_password` stores a scrypt hash
  (`WEB_PASSWORD_HASH`) and a random `SESSION_SECRET` in `.env`; restart the service after
  changing it (that also logs out every session). Every `/api/*` route and `/files/*`
  (via nginx `auth_request`) needs the HttpOnly, SameSite=Lax session cookie. After 5
  wrong passwords, further attempts are refused with exponential back-off (max 5 min).
  With `WEB_PASSWORD_HASH` empty, login is off and Tailscale is the only gate.

* The Telethon `.session` file is **full access to your Telegram account**. It lives in
  `/var/lib/tele-sync/` (mode 600), is git-ignored, and should never leave the VM.
* All secrets are in `/opt/tele-sync/.env` (mode 640, git-ignored).
* No public ports: nginx listens on localhost, Tailscale serves it to your tailnet. GCP
  firewall keeps only SSH (delete `default-allow-ssh` once Tailscale SSH works).
* `/files/` responses carry `Content-Security-Policy: sandbox`, so a received HTML/SVG
  file cannot run scripts in the app's origin.
* Set a GCP billing budget alert (command printed by `gcp-create-vm.sh`).

## Development

```bash
# Postgres somewhere, then:
cp .env.example .env                      # set DATABASE_URL (+ TG_* to actually sync)
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload             # migrations run on startup; also serves /files in dev
cd ../frontend && npm install && npm run dev   # proxies /api and /files to :8000

# tests
TEST_DATABASE_URL=postgresql://…/telesync_test pytest     # backend (wipes that DB's public schema)
npm test                                                   # frontend (entities, thumbnails)
```

The backend starts without Telegram credentials (viewer-only); the status panel says so.
Schema changes go in `backend/app/migrations/NNN_name.sql` and are applied on startup
(or `python -m scripts.migrate`).
