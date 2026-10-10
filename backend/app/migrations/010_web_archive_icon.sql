-- The archived site's icon (favicon), shown next to it in the archive list.
ALTER TABLE web_archive_jobs
  ADD COLUMN icon_path    TEXT,                           -- relative to MEDIA_DIR; NULL = none found
  ADD COLUMN icon_checked BOOLEAN NOT NULL DEFAULT false;  -- looked for it already
