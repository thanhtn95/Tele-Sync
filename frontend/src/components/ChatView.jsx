import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Virtuoso } from 'react-virtuoso';
import { api } from '../lib/api.js';
import { dayKey, formatDay } from '../lib/format.js';
import MessageBubble from './Message.jsx';
import { Lightbox } from './Media.jsx';
import PinnedBar from './PinnedBar.jsx';

const PAGE = 50;
const START_INDEX = 10_000_000; // Virtuoso firstItemIndex must stay positive while prepending

/** Turn ascending messages into rows: date separators + bubbles (albums merged). */
export function buildRows(messages) {
  const rows = [];
  let prevDay = null;
  for (const m of messages) {
    const day = dayKey(m.date);
    if (day !== prevDay) {
      rows.push({ type: 'date', key: `d${day}`, date: m.date });
      prevDay = day;
    }
    const prev = rows[rows.length - 1];
    if (m.grouped_id && prev.type === 'msg' && prev.msgs[0].grouped_id === m.grouped_id) {
      prev.msgs.push(m);
    } else {
      rows.push({ type: 'msg', key: `m${m.id}`, msgs: [m] });
    }
  }
  // Consecutive bubbles from one sender: name on the first, avatar on the last.
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (r.type !== 'msg') continue;
    const sender = (row) => (row?.type === 'msg' && !row.msgs[0].service ? row.msgs[0].sender_id : undefined);
    r.firstOfRun = sender(rows[i - 1]) !== sender(r);
    r.lastOfRun = sender(rows[i + 1]) !== sender(r);
  }
  return rows;
}

export default function ChatView({ chatId }) {
  const [chat, setChat] = useState(null);
  const [messages, setMessages] = useState([]); // ascending by id
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [firstItemIndex, setFirstItemIndex] = useState(START_INDEX);
  const [highlight, setHighlight] = useState(null);
  const [pins, setPins] = useState([]); // newest first
  const [pinIndex, setPinIndex] = useState(0);
  const [lightbox, setLightbox] = useState(null);
  const virtuoso = useRef(null);
  const state = useRef({ messages: [], hasMore: true, loading: false });
  state.current.messages = messages;
  state.current.hasMore = hasMore;

  const rows = useMemo(() => buildRows(messages), [messages]);

  // Prepend older messages, keeping the scroll position: firstItemIndex shifts by the
  // number of rows added (albums split across pages may merge, so diff the row counts).
  const prepend = useCallback((older, more) => {
    setHasMore(more);
    if (!older.length) return;
    const cur = state.current.messages;
    const merged = [...older, ...cur];
    const delta = buildRows(merged).length - buildRows(cur).length;
    state.current.messages = merged;
    setFirstItemIndex((f) => f - delta);
    setMessages(merged);
  }, []);

  useEffect(() => {
    let alive = true;
    setMessages([]);
    setHasMore(true);
    setFirstItemIndex(START_INDEX);
    setError(null);
    api.chat(chatId).then((c) => alive && setChat(c), (e) => alive && setError(e.message));
    setPins([]);
    setPinIndex(0);
    api.pinned(chatId).then((p) => alive && setPins(p), () => {});
    state.current.loading = true;
    setLoading(true);
    api
      .messages(chatId, null, PAGE)
      .then(({ messages: page, has_more }) => {
        if (!alive) return;
        setMessages([...page].reverse());
        setHasMore(has_more);
      })
      .catch((e) => alive && setError(e.message))
      .finally(() => {
        state.current.loading = false;
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [chatId]);

  const loadOlder = useCallback(async () => {
    const s = state.current;
    if (s.loading || !s.hasMore || !s.messages.length) return;
    s.loading = true;
    setLoading(true);
    try {
      const { messages: page, has_more } = await api.messages(chatId, s.messages[0].id, PAGE);
      prepend([...page].reverse(), has_more);
    } catch (e) {
      setError(e.message);
    } finally {
      s.loading = false;
      setLoading(false);
    }
  }, [chatId, prepend]);

  // Jump to a replied-to message, loading older pages until it is in memory.
  const pendingJump = useRef(null);
  const jumpTo = useCallback(
    async (id) => {
      const s = state.current;
      // A scroll-triggered page load may be in flight; wait for it rather than dropping the jump.
      for (let i = 0; s.loading && i < 200; i++) await new Promise((r) => setTimeout(r, 50));
      if (!s.messages.length || s.loading) return;
      if (s.messages[0].id > id && s.hasMore) {
        s.loading = true;
        setLoading(true);
        try {
          let before = s.messages[0].id;
          let more = true;
          const older = [];
          for (let i = 0; i < 40 && before > id && more; i++) {
            const res = await api.messages(chatId, before, 200);
            const page = res.messages;
            more = res.has_more;
            if (!page.length) break;
            older.unshift(...[...page].reverse());
            before = page[page.length - 1].id;
          }
          pendingJump.current = id;
          prepend(older, more);
        } finally {
          s.loading = false;
          setLoading(false);
        }
        return;
      }
      pendingJump.current = id;
      setHighlight({ id, at: Date.now() });
    },
    [chatId, prepend],
  );

  useEffect(() => {
    const id = pendingJump.current;
    if (id == null) return;
    const idx = rows.findIndex((r) => r.type === 'msg' && r.msgs.some((m) => m.id === id));
    if (idx < 0) return;
    pendingJump.current = null;
    // Defer until Virtuoso has applied a just-prepended page (new firstItemIndex), then
    // repeat once item heights are measured so the target really ends up centered.
    const go = () => virtuoso.current?.scrollToIndex({ index: idx, align: 'center', behavior: 'auto' });
    // Not cancelled on cleanup: setHighlight below re-runs this effect immediately.
    requestAnimationFrame(go);
    setTimeout(go, 250);
    setHighlight({ id, at: Date.now() });
  }, [rows, highlight?.at]);

  useEffect(() => {
    if (!highlight) return undefined;
    const t = setTimeout(() => setHighlight(null), 1800);
    return () => clearTimeout(t);
  }, [highlight]);

  // Like Telegram, the pinned bar shows the newest pin *above* the middle of the viewport;
  // so after jumping to a pin (centered), it moves on to the next older one.
  const view = useRef({ rows, firstItemIndex, pins });
  view.current = { rows, firstItemIndex, pins };
  const onRangeChanged = useCallback(({ startIndex, endIndex }) => {
    const { rows: rs, firstItemIndex: first, pins: ps } = view.current;
    if (!ps.length) return;
    const mid = Math.round((startIndex + endIndex) / 2) - first;
    let row = null;
    for (let i = Math.min(mid, rs.length - 1); i >= 0; i--) {
      if (rs[i]?.type === 'msg') {
        row = rs[i];
        break;
      }
    }
    if (!row) return;
    const midId = row.msgs[0].id;
    const idx = ps.findIndex((p) => p.id < midId);
    setPinIndex(idx >= 0 ? idx : 0);
  }, []);

  const isGroup = chat?.type === 'group';

  const itemContent = useCallback(
    (_, row) => {
      if (row.type === 'date') {
        return (
          <div className="date-sep">
            <span>{formatDay(row.date)}</span>
          </div>
        );
      }
      return (
        <MessageBubble
          msgs={row.msgs}
          isGroup={isGroup}
          showName={isGroup && row.firstOfRun}
          showAvatar={row.lastOfRun}
          highlighted={highlight && row.msgs.some((m) => m.id === highlight.id)}
          onJump={jumpTo}
          onOpen={setLightbox}
        />
      );
    },
    [isGroup, highlight, jumpTo],
  );

  return (
    <div className="chat-view">
      <header className="chat-header">
        <a href="#/" className="back" aria-label="Back to chats">
          ←
        </a>
        <div>
          <div className="chat-title">{chat?.title ?? '…'}</div>
          <div className="muted small">
            {chat ? `${chat.message_count.toLocaleString()} messages synced` : ''}
            {loading ? ' · loading…' : ''}
          </div>
        </div>
      </header>
      <PinnedBar pins={pins} current={pinIndex} onJump={jumpTo} />
      {error && <div className="error-bar">{error}</div>}
      <div className="chat-body">
        {messages.length > 0 ? (
          <Virtuoso
            key={chatId}
            ref={virtuoso}
            className="message-list"
            data={rows}
            firstItemIndex={firstItemIndex}
            initialTopMostItemIndex={rows.length - 1}
            computeItemKey={(_, row) => row.key}
            startReached={loadOlder}
            rangeChanged={onRangeChanged}
            itemContent={itemContent}
            alignToBottom
            increaseViewportBy={{ top: 800, bottom: 400 }}
            components={{
              Header: () => <div className="list-header muted small">{hasMore ? 'Loading…' : 'Beginning of synced history'}</div>,
            }}
          />
        ) : (
          !loading && <div className="empty muted">No synced messages yet.</div>
        )}
      </div>
      <Lightbox item={lightbox} onClose={() => setLightbox(null)} />
    </div>
  );
}
