-- Website archives: a job saves the pages of the categories picked for one site
-- (files under MEDIA_DIR/web/job<id>/).
CREATE TABLE web_archive_jobs (
  id           BIGSERIAL PRIMARY KEY,
  url          TEXT NOT NULL,
  title        TEXT,
  categories   JSONB NOT NULL,          -- [{prefix, label, url}]
  max_pages    INT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'queued',  -- queued/running/done/cancelled/failed/interrupted
  pages_saved  INT NOT NULL DEFAULT 0,
  pages_failed INT NOT NULL DEFAULT 0,
  error        TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at  TIMESTAMPTZ
);

CREATE TABLE web_archive_pages (
  id           BIGSERIAL PRIMARY KEY,
  job_id       BIGINT NOT NULL REFERENCES web_archive_jobs ON DELETE CASCADE,
  url          TEXT NOT NULL,
  final_url    TEXT NOT NULL,
  category     TEXT,                    -- prefix of the category it was found in; NULL = home page
  status       INT,
  content_type TEXT,
  title        TEXT,
  size         BIGINT,
  file_path    TEXT,                    -- NULL when it failed (see error)
  error        TEXT,
  archived_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX web_archive_pages_job ON web_archive_pages (job_id, id);
