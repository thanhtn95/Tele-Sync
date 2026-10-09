-- Snapshots of web pages saved with POST /api/archive (files under MEDIA_DIR/web/).
CREATE TABLE web_archives (
  id           BIGSERIAL PRIMARY KEY,
  url          TEXT NOT NULL,
  final_url    TEXT NOT NULL,
  status       INT NOT NULL,
  content_type TEXT,
  title        TEXT,
  size         BIGINT NOT NULL,
  file_path    TEXT NOT NULL,
  archived_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX web_archives_archived_at ON web_archives (archived_at DESC);
