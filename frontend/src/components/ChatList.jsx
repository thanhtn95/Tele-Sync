import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { formatAgo, formatTime } from '../lib/format.js';
import { snippetAround } from '../lib/fold.js';
import RichText from './RichText.jsx';
import { MEDIA_LABEL } from './Message.jsx';
import { Avatar } from './Message.jsx';
import SyncPanel from './SyncPanel.jsx';
import ThemeToggle from './ThemeToggle.jsx';
import PrivacyToggle from './PrivacyToggle.jsx';

const TABS = [
  ['all', 'All'],
  ['user', 'Users'],
  ['group', 'Groups'],
  ['channel', 'Channels'],
];

// <input type=date> works in local dates; the API stores timestamps.
const toDateInput = (iso) => {
  if (!iso) return '';
  const d = new Date(iso);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};
const fromDateInput = (v) => (v ? new Date(`${v}T00:00:00`).toISOString() : null);

function Toggle({ checked, onChange, label }) {
  return (
    <label className="toggle" title={label}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} aria-label={label} />
      <span className="slider" />
    </label>
  );
}

const badge = (n) => (n >= 1000 ? '999+' : n.toLocaleString());

/** Messages from every chat matching the search box (accent/case-insensitive). */
function MessageResults({ query }) {
  const [res, setRes] = useState({ items: [], total: null, more: false, busy: false });
  const req = useRef(0);
  const q = query.trim();
  useEffect(() => {
    if (q.length < 2) {
      setRes({ items: [], total: null, more: false, busy: false });
      return undefined;
    }
    const my = ++req.current;
    setRes((r) => ({ ...r, busy: true }));
    const t = setTimeout(async () => {
      try {
        const r = await api.searchAll(q);
        if (my === req.current) {
          setRes({ items: r.results, total: `${r.total.toLocaleString()}${r.total_capped ? '+' : ''}`, more: r.has_more, busy: false });
        }
      } catch {
        if (my === req.current) setRes({ items: [], total: null, more: false, busy: false });
      }
    }, 400);
    return () => clearTimeout(t);
  }, [q]);
  const loadMore = async () => {
    const last = res.items[res.items.length - 1];
    const r = await api.searchAll(q, { date: last.date, chat_id: last.chat_id, id: last.id });
    setRes((cur) => ({ ...cur, items: [...cur.items, ...r.results], more: r.has_more }));
  };
  if (q.length < 2) return null;
  return (
    <section className="msg-results">
      <h2 className="muted small">
        Messages{res.busy ? ' · searching…' : res.total != null ? ` · ${res.total}` : ''}
      </h2>
      {!res.busy && res.total === '0' && <div className="muted small">No messages found.</div>}
      <ul className="chat-rows">
        {res.items.map((r) => (
          <li key={`${r.chat_id}:${r.id}`} className="chat-row">
            <a className="msg-result" href={`#/chat/${r.chat_id}?m=${r.id}`}>
              <span className="search-row-head">
                <strong>{r.chat_title}</strong>
                <span className="muted small">
                  {new Date(r.date).toLocaleDateString()} {formatTime(r.date)}
                </span>
              </span>
              <span className="search-snippet">
                <span className="muted">{r.out ? 'You' : r.sender_name || 'Unknown'}: </span>
                <RichText text={snippetAround(r.text || r.file_name || MEDIA_LABEL[r.media_kind] || '', q)} highlight={q} />
              </span>
            </a>
          </li>
        ))}
      </ul>
      {res.more && (
        <button className="link-btn search-more" onClick={loadMore}>
          More results
        </button>
      )}
    </section>
  );
}

function ChatRow({ chat, status, onPatch, onSyncNow }) {
  const [open, setOpen] = useState(false);
  const [asking, setAsking] = useState(false);
  const count = status?.message_count ?? chat.message_count;
  const unread = status?.unread_count ?? chat.unread_count ?? 0;
  const lastSynced = status?.last_synced_at ?? chat.last_synced_at;
  const syncing = status?.syncing;
  const queued = status?.queued;
  const syncNow = async () => {
    setAsking(true);
    try {
      await onSyncNow(chat.chat_id);
    } finally {
      setAsking(false);
    }
  };
  // The whole row opens the chat; its own controls (badge, ⚙, toggle) keep their clicks.
  const openChat = (e) => {
    if (count > 0 && !e.target.closest('a, button, input, label')) window.location.hash = `#/chat/${chat.chat_id}`;
  };
  return (
    <li className={`chat-row${chat.sync_enabled ? ' enabled' : ''}`}>
      <div className={`chat-row-main${count > 0 ? ' openable' : ''}`} onClick={openChat}>
        <Avatar name={chat.title} id={chat.chat_id} size={42} />
        <div className="chat-row-info">
          {count > 0 ? (
            <a className="chat-row-title" href={`#/chat/${chat.chat_id}`}>
              {chat.title}
            </a>
          ) : (
            <span className="chat-row-title">{chat.title}</span>
          )}
          <span className="muted small">
            <span className={`type-badge ${chat.type}`}>{chat.type}</span>
            {(chat.sync_enabled || count > 0) && (
              <>
                {' · '}
                {count.toLocaleString()} msgs · {syncing ? 'syncing…' : queued ? 'queued…' : `synced ${formatAgo(lastSynced)}`}
              </>
            )}
            {status?.error && <span className="err"> · {status.error}</span>}
          </span>
        </div>
        {unread > 0 && (
          <a className="unread-badge" href={`#/chat/${chat.chat_id}`} title={`${unread} unread`}>
            {badge(unread)}
          </a>
        )}
        {chat.sync_enabled && (
          <button
            className={`icon-btn${syncing || queued ? ' spin' : ''}`}
            onClick={syncNow}
            disabled={asking || syncing || queued}
            aria-label="Sync this chat now"
            title={syncing ? 'Syncing…' : queued ? 'Queued' : 'Sync this chat now'}
          >
            ↻
          </button>
        )}
        <button className="icon-btn" onClick={() => setOpen((o) => !o)} aria-label="Settings" title="Settings">
          ⚙
        </button>
        <Toggle checked={chat.sync_enabled} onChange={(v) => onPatch(chat.chat_id, { sync_enabled: v })} label="Sync this chat" />
      </div>
      {open && (
        <div className="chat-row-settings">
          <label>
            <input
              type="checkbox"
              checked={chat.sync_media}
              onChange={(e) => onPatch(chat.chat_id, { sync_media: e.target.checked })}
            />{' '}
            Include media
          </label>
          <label>
            Sync since{' '}
            <input
              type="date"
              value={toDateInput(chat.sync_since)}
              onChange={(e) => onPatch(chat.chat_id, { sync_since: fromDateInput(e.target.value) })}
            />
          </label>
          {chat.sync_since && (
            <button className="link-btn" onClick={() => onPatch(chat.chat_id, { sync_since: null })}>
              clear (full history)
            </button>
          )}
          <span className="muted small">Cursor: message #{chat.last_msg_id}</span>
        </div>
      )}
    </li>
  );
}

export default function ChatList({ onLogout, username }) {
  const [chats, setChats] = useState(null);
  const [tab, setTab] = useState('all');
  const [query, setQuery] = useState('');
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [status, setStatus] = useState(null);
  const [statusKey, setStatusKey] = useState(0);

  const load = useCallback(async (refresh) => {
    setError(null);
    if (refresh) setRefreshing(true);
    try {
      setChats(await api.dialogs(refresh));
    } catch (e) {
      setError(e.message);
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load(false);
  }, [load]);

  // Saved immediately; optimistic with rollback on failure.
  const patch = useCallback(async (id, change) => {
    let before;
    setChats((cs) =>
      cs.map((c) => {
        if (c.chat_id !== id) return c;
        before = c;
        return { ...c, ...change };
      }),
    );
    try {
      const saved = await api.patchChat(id, change);
      setChats((cs) => cs.map((c) => (c.chat_id === id ? saved : c)));
    } catch (e) {
      setError(`Saving failed: ${e.message}`);
      setChats((cs) => cs.map((c) => (c.chat_id === id ? before : c)));
    }
  }, []);

  const syncNow = useCallback(async (id) => {
    try {
      await api.syncChat(id);
      setStatusKey((k) => k + 1); // SyncPanel re-polls and shows "queued" / "syncing"
    } catch (e) {
      setError(`Sync failed to start: ${e.message}`);
    }
  }, []);

  const statusById = useMemo(() => {
    const m = new Map();
    const queued = new Set(status?.queued_chat_ids ?? []);
    for (const c of status?.chats ?? [])
      m.set(c.chat_id, { ...c, syncing: status.current_chat_id === c.chat_id, queued: queued.has(c.chat_id) });
    return m;
  }, [status]);

  const visible = useMemo(() => {
    if (!chats) return [];
    const q = query.trim().toLowerCase();
    const unreadOf = (c) => statusById.get(c.chat_id)?.unread_count ?? c.unread_count ?? 0;
    return chats
      .filter((c) => (tab === 'all' || c.type === tab) && (!q || (c.title || '').toLowerCase().includes(q)))
      .map((c, i) => [c, i])
      // synced chats with new messages first, otherwise keep the server's order
      .sort(([a, i], [b, j]) => (unreadOf(b) > 0) - (unreadOf(a) > 0) || i - j)
      .map(([c]) => c);
  }, [chats, tab, query, statusById]);

  // "(5) Telegram Archive" in the browser tab
  const totalUnread = useMemo(
    () => (status?.chats ?? []).reduce((n, c) => n + (c.unread_count || 0), 0),
    [status],
  );
  useEffect(() => {
    document.title = totalUnread ? `(${badge(totalUnread)}) Telegram Archive` : 'Telegram Archive';
  }, [totalUnread]);

  const enabledCount = chats?.filter((c) => c.sync_enabled).length ?? 0;

  return (
    <div className="chat-list-page">
      <header className="top">
        <h1>Telegram Archive</h1>
        <div className="top-actions">
          <PrivacyToggle />
          <ThemeToggle />
          <button onClick={() => load(true)} disabled={refreshing} title="Re-read chat list from Telegram (slow)">
            {refreshing ? 'Refreshing…' : '↻ Refresh chats'}
          </button>
          {onLogout && (
            <button onClick={onLogout} title={username ? `Signed in as ${username}` : undefined}>
              Log out
            </button>
          )}
        </div>
      </header>
      <SyncPanel onStatus={setStatus} refreshKey={statusKey} />
      {error && <div className="error-bar">{error}</div>}
      <div className="filters">
        <input
          type="search"
          placeholder="Search chats and messages"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="search"
        />
        <div className="tabs" role="tablist">
          {TABS.map(([k, label]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>
              {label}
            </button>
          ))}
        </div>
        <div className="muted small">
          {chats ? `${visible.length} shown · ${enabledCount} syncing` : 'Loading chats…'}
        </div>
      </div>
      {query.trim().length >= 2 && visible.length > 0 && <h2 className="muted small">Chats</h2>}
      <ul className="chat-rows">
        {visible.map((c) => (
          <ChatRow key={c.chat_id} chat={c} status={statusById.get(c.chat_id)} onPatch={patch} onSyncNow={syncNow} />
        ))}
      </ul>
      <MessageResults query={query} />
    </div>
  );
}
