CREATE TABLE chats (
  chat_id BIGINT PRIMARY KEY,
  title TEXT,
  type TEXT,                    -- user / group / channel
  sync_enabled BOOLEAN DEFAULT false,
  sync_media BOOLEAN DEFAULT true,
  sync_since TIMESTAMPTZ,       -- optional start date
  last_msg_id BIGINT DEFAULT 0, -- incremental sync cursor
  gphotos_album_id TEXT,
  last_synced_at TIMESTAMPTZ
);

CREATE TABLE users (
  user_id BIGINT PRIMARY KEY,
  name TEXT,
  username TEXT,
  avatar_path TEXT
);

CREATE TABLE messages (
  chat_id BIGINT REFERENCES chats,
  message_id BIGINT,            -- only unique per chat -> composite PK
  sender_id BIGINT,
  date TIMESTAMPTZ,
  text TEXT,
  reply_to BIGINT,
  grouped_id BIGINT,            -- Telegram albums: same id = one bubble
  fwd_from_name TEXT,           -- "Forwarded from X"
  edit_date TIMESTAMPTZ,
  raw JSONB,                    -- msg.to_dict() (includes entities for formatting)
  PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX ON messages (chat_id, date DESC);

CREATE TABLE media (
  chat_id BIGINT, message_id BIGINT,
  kind TEXT,                    -- photo / video / voice / sticker / document
  mime TEXT, size BIGINT,
  width INT, height INT, duration INT,
  thumb_b64 TEXT,               -- Telegram stripped thumbnail, inflate client-side
  file_path TEXT,               -- for non-photo/video files
  gphotos_media_id TEXT,        -- for photos/videos uploaded to Google Photos
  PRIMARY KEY (chat_id, message_id),
  FOREIGN KEY (chat_id, message_id) REFERENCES messages
);
