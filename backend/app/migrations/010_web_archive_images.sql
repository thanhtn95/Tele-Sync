-- Pictures for the archive pages: the site's icon (favicon), shown next to it in the archive list.
ALTER TABLE web_archive_jobs
  ADD COLUMN icon_path    TEXT,                           -- relative to MEDIA_DIR; NULL = none found
  ADD COLUMN icon_checked BOOLEAN NOT NULL DEFAULT false;  -- looked for it already

-- Each post's thumbnail as its site's list page shows it (else its og:image).
ALTER TABLE web_archive_pages ADD COLUMN thumb_path TEXT;  -- relative to MEDIA_DIR
