import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api.js';
import { formatAgo } from '../lib/format.js';
import { Avatar } from './Message.jsx';
import SyncPanel from './SyncPanel.jsx';

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

function ChatRow({ chat, status, onPatch }) {
  const [open, setOpen] = useState(false);
  const count = status?.message_count ?? chat.message_count;
  const lastSynced = status?.last_synced_at ?? chat.last_synced_at;
  const syncing = status?.syncing;
  return (
    <li className={`chat-row${chat.sync_enabled ? ' enabled' : ''}`}>
      <div className="chat-row-main">
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
                {count.toLocaleString()} msgs · {syncing ? 'syncing…' : `synced ${formatAgo(lastSynced)}`}
              </>
            )}
            {status?.error && <span className="err"> · {status.error}</span>}
          </span>
        </div>
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

export default function ChatList() {
  const [chats, setChats] = useState(null);
  const [tab, setTab] = useState('all');
  const [query, setQuery] = useState('');
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [status, setStatus] = useState(null);

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

  const statusById = useMemo(() => {
    const m = new Map();
    for (const c of status?.chats ?? []) m.set(c.chat_id, { ...c, syncing: status.current_chat_id === c.chat_id });
    return m;
  }, [status]);

  const visible = useMemo(() => {
    if (!chats) return [];
    const q = query.trim().toLowerCase();
    return chats.filter((c) => (tab === 'all' || c.type === tab) && (!q || (c.title || '').toLowerCase().includes(q)));
  }, [chats, tab, query]);

  const enabledCount = chats?.filter((c) => c.sync_enabled).length ?? 0;

  return (
    <div className="chat-list-page">
      <header className="top">
        <h1>Telegram Archive</h1>
        <button onClick={() => load(true)} disabled={refreshing} title="Re-read chat list from Telegram (slow)">
          {refreshing ? 'Refreshing…' : '↻ Refresh chats'}
        </button>
      </header>
      <SyncPanel onStatus={setStatus} />
      {error && <div className="error-bar">{error}</div>}
      <div className="filters">
        <input
          type="search"
          placeholder="Search chats"
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
      <ul className="chat-rows">
        {visible.map((c) => (
          <ChatRow key={c.chat_id} chat={c} status={statusById.get(c.chat_id)} onPatch={patch} />
        ))}
      </ul>
    </div>
  );
}
