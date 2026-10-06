import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Virtuoso } from 'react-virtuoso';
import { api } from '../lib/api.js';
import { dayKey, formatDay } from '../lib/format.js';
import ChatSearch from './ChatSearch.jsx';
import MediaPanel from './MediaPanel.jsx';
import MediaViewer from './MediaViewer.jsx';
import MessageBubble from './Message.jsx';
import PinnedBar from './PinnedBar.jsx';
import ThemeToggle from './ThemeToggle.jsx';

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

const rowIndexOf = (rows, id) => rows.findIndex((r) => r.type === 'msg' && r.msgs.some((m) => m.id === id));

const TABS = [
  ['chat', 'Chat'],
  ['media', 'Media'],
  ['files', 'Files'],
  ['voice', 'Voice'],
];

/**
 * The loaded messages are a window into the chat that can sit anywhere in history:
 * opening shows the newest page; jumping to an old reply/pin loads a window around it.
 * Scrolling up loads older pages, scrolling down loads newer ones until the latest.
 */
export default function ChatView({ chatId, tab = 'chat' }) {
  const [chat, setChat] = useState(null);
  const [messages, setMessages] = useState([]); // ascending by id
  const [hasMore, setHasMore] = useState(true); // older messages exist
  const [hasNewer, setHasNewer] = useState(false); // window is not at the latest message
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [firstItemIndex, setFirstItemIndex] = useState(START_INDEX);
  const [listKey, setListKey] = useState(0); // bump to remount the list on a new window
  const [initialIndex, setInitialIndex] = useState(null);
  const [atBottom, setAtBottom] = useState(true);
  const [highlight, setHighlight] = useState(null);
  const [pins, setPins] = useState([]); // newest first
  const [pinIndex, setPinIndex] = useState(0);
  const [counts, setCounts] = useState(null);
  const [viewer, setViewer] = useState(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const virtuoso = useRef(null);
  const state = useRef({ messages: [], hasMore: true, hasNewer: false, loading: false });
  state.current.messages = messages;
  state.current.hasMore = hasMore;
  state.current.hasNewer = hasNewer;

  const rows = useMemo(() => buildRows(messages), [messages]);

  const withLoading = useCallback(async (fn) => {
    const s = state.current;
    // A scroll-triggered page load may be in flight; wait for it rather than dropping the action.
    for (let i = 0; s.loading && i < 200; i++) await new Promise((r) => setTimeout(r, 50));
    if (s.loading) return;
    s.loading = true;
    setLoading(true);
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      s.loading = false;
      setLoading(false);
    }
  }, []);

  /** Replace the window: newest page (target = null) or a window around message `target`. */
  const openWindow = useCallback(
    (target) =>
      withLoading(async () => {
        const res = target == null ? await api.messages(chatId, null, PAGE) : await api.messagesAround(chatId, target, PAGE * 2);
        const asc = [...res.messages].reverse();
        const newRows = buildRows(asc);
        let idx = newRows.length - 1;
        if (target != null) {
          // The exact message may be missing (e.g. deleted); fall back to the nearest one.
          const near = asc.find((m) => m.id >= target) ?? asc[asc.length - 1];
          idx = Math.max(0, rowIndexOf(newRows, near?.id));
          setHighlight({ id: near?.id, at: Date.now() });
        }
        state.current.messages = asc;
        setMessages(asc);
        setHasMore(res.has_more);
        setHasNewer(Boolean(res.has_newer));
        setFirstItemIndex(START_INDEX);
        setInitialIndex(target == null ? { index: idx, align: 'end' } : { index: idx, align: 'center' });
        setListKey((k) => k + 1);
      }),
    [chatId, withLoading],
  );

  useEffect(() => {
    let alive = true;
    setMessages([]);
    setError(null);
    setChat(null);
    setPins([]);
    setPinIndex(0);
    setCounts(null);
    setSearchOpen(false);
    setQuery('');
    api.chat(chatId).then((c) => alive && setChat(c), (e) => alive && setError(e.message));
    api.pinned(chatId).then((p) => alive && setPins(p), () => {});
    api.chatMedia(chatId, { limit: 1 }).then((r) => alive && setCounts(r.counts), () => {});
    openWindow(null);
    return () => {
      alive = false;
    };
  }, [chatId, openWindow]);

  // Prepend older messages, keeping the scroll position: firstItemIndex shifts by the
  // number of rows added (albums split across pages may merge, so diff the row counts).
  const loadOlder = useCallback(() => {
    const s = state.current;
    if (s.loading || !s.hasMore || !s.messages.length) return;
    withLoading(async () => {
      const res = await api.messages(chatId, s.messages[0].id, PAGE);
      const older = [...res.messages].reverse();
      setHasMore(res.has_more);
      if (!older.length) return;
      const cur = state.current.messages;
      const merged = [...older, ...cur];
      const delta = buildRows(merged).length - buildRows(cur).length;
      state.current.messages = merged;
      setFirstItemIndex((f) => f - delta);
      setMessages(merged);
    });
  }, [chatId, withLoading]);

  // Append newer messages (only when the window was opened somewhere in the past).
  const loadNewer = useCallback(() => {
    const s = state.current;
    if (s.loading || !s.hasNewer || !s.messages.length) return;
    withLoading(async () => {
      const res = await api.messagesAfter(chatId, s.messages[s.messages.length - 1].id, PAGE);
      const newer = [...res.messages].reverse();
      setHasNewer(Boolean(res.has_newer));
      if (!newer.length) return;
      const merged = [...state.current.messages, ...newer];
      state.current.messages = merged;
      setMessages(merged);
    });
  }, [chatId, withLoading]);

  // Jump to any message: scroll if loaded, otherwise load a window around it.
  const pendingJump = useRef(null);
  const jumpTo = useCallback(
    (id) => {
      if (rowIndexOf(buildRows(state.current.messages), id) >= 0) {
        pendingJump.current = id;
        setHighlight({ id, at: Date.now() });
      } else {
        openWindow(id);
      }
    },
    [openWindow],
  );

  useEffect(() => {
    const id = pendingJump.current;
    if (id == null) return;
    const idx = rowIndexOf(rows, id);
    if (idx < 0) return;
    pendingJump.current = null;
    // rAF + a later repeat: the second pass corrects for rows measured on the way.
    const go = () => virtuoso.current?.scrollToIndex({ index: idx, align: 'center', behavior: 'auto' });
    requestAnimationFrame(go);
    setTimeout(go, 250);
  }, [rows, highlight?.at]);

  useEffect(() => {
    if (!highlight) return undefined;
    const t = setTimeout(() => setHighlight(null), 1800);
    return () => clearTimeout(t);
  }, [highlight]);

  const toLatest = useCallback(() => {
    if (state.current.hasNewer) openWindow(null);
    else virtuoso.current?.scrollToIndex({ index: rows.length - 1, align: 'end', behavior: 'smooth' });
  }, [openWindow, rows.length]);

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

  const showInChat = useCallback(
    (id) => {
      setViewer(null);
      if (tab !== 'chat') window.location.hash = `#/chat/${chatId}`;
      jumpTo(id);
    },
    [chatId, tab, jumpTo],
  );

  const closeSearch = useCallback(() => {
    setSearchOpen(false);
    setQuery('');
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
          onOpen={setViewer}
          query={query}
        />
      );
    },
    [isGroup, highlight, jumpTo, query],
  );

  return (
    <div className="chat-view">
      {searchOpen && tab === 'chat' ? (
        <header className="chat-header search-mode">
          <ChatSearch chatId={chatId} onClose={closeSearch} onJump={jumpTo} onQuery={setQuery} />
        </header>
      ) : (
        <header className="chat-header">
          <a href="#/" className="back" aria-label="Back to chats">
            ←
          </a>
          <div className="chat-header-title">
            <div className="chat-title">{chat?.title ?? '…'}</div>
            <div className="muted small">
              {chat ? `${chat.message_count.toLocaleString()} messages synced` : ''}
              {loading ? ' · loading…' : ''}
            </div>
          </div>
          {tab === 'chat' && (
            <button className="icon-btn search-btn" onClick={() => setSearchOpen(true)} aria-label="Search in chat" title="Search">
              🔍
            </button>
          )}
          <ThemeToggle />
        </header>
      )}
      <nav className="chat-tabs" role="tablist">
        {TABS.map(([k, label]) => (
          <a
            key={k}
            role="tab"
            aria-selected={tab === k}
            className={tab === k ? 'active' : ''}
            href={k === 'chat' ? `#/chat/${chatId}` : `#/chat/${chatId}/${k}`}
          >
            {label}
            {k !== 'chat' && counts?.[k] ? <span className="tab-count">{counts[k].toLocaleString()}</span> : null}
          </a>
        ))}
      </nav>
      {tab === 'chat' && !searchOpen && <PinnedBar pins={pins} current={pinIndex} onJump={jumpTo} />}
      {error && <div className="error-bar">{error}</div>}
      <div className="chat-body">
        {messages.length > 0 ? (
          <Virtuoso
            key={`${chatId}-${listKey}`}
            ref={virtuoso}
            className="message-list"
            data={rows}
            firstItemIndex={firstItemIndex}
            initialTopMostItemIndex={initialIndex ?? rows.length - 1}
            computeItemKey={(_, row) => row.key}
            startReached={loadOlder}
            endReached={loadNewer}
            atBottomStateChange={setAtBottom}
            rangeChanged={onRangeChanged}
            itemContent={itemContent}
            alignToBottom
            increaseViewportBy={{ top: 800, bottom: 400 }}
            components={{
              Header: () => (
                <div className="list-header muted small">{hasMore ? 'Loading…' : 'Beginning of synced history'}</div>
              ),
              Footer: () => (hasNewer ? <div className="list-header muted small">Loading…</div> : null),
            }}
          />
        ) : (
          !loading && <div className="empty muted">No synced messages yet.</div>
        )}
        {tab === 'chat' && (hasNewer || !atBottom) && messages.length > 0 && (
          <button className="to-latest" onClick={toLatest} aria-label="Go to latest message" title="Go to latest">
            ↓
          </button>
        )}
        {tab !== 'chat' && (
          // Overlay, so the message list keeps its place underneath.
          <div className="tab-overlay">
            <MediaPanel key={`${chatId}-${tab}`} chatId={chatId} group={tab} onOpen={setViewer} onShowInChat={showInChat} />
          </div>
        )}
      </div>
      {viewer && (
        <MediaViewer chatId={chatId} start={viewer} onClose={() => setViewer(null)} onShowInChat={showInChat} />
      )}
    </div>
  );
}
