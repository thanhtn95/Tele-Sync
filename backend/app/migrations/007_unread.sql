-- "Unread since you last looked": messages from others dated after last_read_at.
-- Existing chats start as read now (old history isn't "new"); new chats default to the
-- time they're added, so importing their history doesn't count as unread either.
ALTER TABLE chats ADD COLUMN last_read_at TIMESTAMPTZ NOT NULL DEFAULT now();
