-- Pinned state mirrors Telegram: refreshed from the chat's pinned list on every sync pass.
ALTER TABLE messages ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX messages_pinned_idx ON messages (chat_id, message_id) WHERE pinned;
