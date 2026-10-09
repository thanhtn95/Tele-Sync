-- Website archives also keep each page's stylesheets, fonts and images (job<id>/assets/).
ALTER TABLE web_archive_jobs
  ADD COLUMN save_images  BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN assets_saved INT NOT NULL DEFAULT 0,
  ADD COLUMN assets_bytes BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN pages_bytes  BIGINT NOT NULL DEFAULT 0;
