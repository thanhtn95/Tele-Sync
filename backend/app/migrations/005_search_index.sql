-- Fast search: keep the folded text in stored columns (computed once on write instead of
-- on every search) and, when the pg_trgm extension is available, index them so
-- "contains" searches use the index instead of scanning the whole chat.
-- deploy/setup.sh creates pg_trgm (needs a superuser) before the app runs migrations.
ALTER TABLE messages ADD COLUMN search_text TEXT GENERATED ALWAYS AS (fold_text(text)) STORED;
ALTER TABLE media ADD COLUMN search_name TEXT GENERATED ALWAYS AS (fold_text(file_name)) STORED;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
    CREATE INDEX messages_search_trgm ON messages USING gin (search_text gin_trgm_ops);
    CREATE INDEX media_search_trgm ON media USING gin (search_name gin_trgm_ops);
  END IF;
END
$$;
