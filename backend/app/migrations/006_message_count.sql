-- Store each chat's message count instead of counting on every chat-list/status
-- request (that count got very slow on a slow disk right after a table rewrite).
-- The sync worker adds newly inserted messages to it.
ALTER TABLE chats ADD COLUMN message_count BIGINT NOT NULL DEFAULT 0;
UPDATE chats c SET message_count = s.n
FROM (SELECT chat_id, count(*) AS n FROM messages GROUP BY chat_id) s
WHERE c.chat_id = s.chat_id;
