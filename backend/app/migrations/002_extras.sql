-- Columns the viewer needs that the base schema lacks.
ALTER TABLE messages ADD COLUMN out BOOLEAN NOT NULL DEFAULT false;  -- sent by me -> right-hand bubble
ALTER TABLE media ADD COLUMN file_name TEXT;     -- original document name for the file card
ALTER TABLE media ADD COLUMN error TEXT;         -- last download/upload error, NULL when fine
ALTER TABLE users ADD COLUMN avatar_checked BOOLEAN NOT NULL DEFAULT false;  -- avatar download attempted

-- Media not yet stored anywhere (failed, or synced while sync_media was off);
-- lets later passes retry / backfill them cheaply.
CREATE INDEX media_pending_idx ON media (chat_id, message_id)
  WHERE gphotos_media_id IS NULL AND file_path IS NULL;
