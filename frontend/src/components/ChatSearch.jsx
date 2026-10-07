import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { formatTime } from '../lib/format.js';
import { snippetAround } from '../lib/fold.js';
import { MEDIA_LABEL } from './Message.jsx';
import RichText from './RichText.jsx';

const PAGE = 50;

/**
 * Telegram-style in-chat search. Results are newest first; ▲ goes to the next older
 * match, ▼ back to newer ones, and each step jumps the chat to that message.
 * Renders the header search field, the nav bar and (optionally) the results list.
 */
export default function ChatSearch({ chatId, onClose, onJump, onQuery }) {
  const [q, setQ] = useState('');
  const [results, setResults] = useState([]);
  const [total, setTotal] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const [idx, setIdx] = useState(-1);
  const [busy, setBusy] = useState(false);
  const [listOpen, setListOpen] = useState(false);
  const input = useRef(null);
  const reqId = useRef(0);
  const state = useRef({ results, hasMore, idx, q });
  state.current = { results, hasMore, idx, q };

  useEffect(() => input.current?.focus(), []);

  // Debounced search; the newest match is shown first, like Telegram.
  useEffect(() => {
    const query = q.trim();
    onQuery(query);
    if (!query) {
      setResults([]);
      setTotal(null);
      setIdx(-1);
      return undefined;
    }
    const my = ++reqId.current;
    const t = setTimeout(async () => {
      setBusy(true);
      try {
        const r = await api.search(chatId, query, null, PAGE);
        if (my !== reqId.current) return;
        setResults(r.results);
        setTotal(r.total == null ? null : `${r.total.toLocaleString()}${r.total_capped ? '+' : ''}`);
        setHasMore(r.has_more);
        setIdx(r.results.length ? 0 : -1);
        if (r.results.length) onJump(r.results[0].id);
      } finally {
        if (my === reqId.current) setBusy(false);
      }
    }, 350);
    return () => clearTimeout(t);
  }, [q, chatId, onJump, onQuery]);

  const loadMore = useCallback(async () => {
    const s = state.current;
    if (!s.hasMore || !s.results.length) return s.results;
    const r = await api.search(chatId, s.q.trim(), s.results[s.results.length - 1].id, PAGE);
    const merged = [...s.results, ...r.results];
    setResults(merged);
    setHasMore(r.has_more);
    return merged;
  }, [chatId]);

  const step = useCallback(
    async (dir) => {
      const s = state.current;
      let list = s.results;
      const next = s.idx + dir; // +1 = older, -1 = newer
      if (next < 0) return;
      if (next >= list.length) {
        if (!s.hasMore) return;
        list = await loadMore();
        if (next >= list.length) return;
      }
      setIdx(next);
      onJump(list[next].id);
    },
    [loadMore, onJump],
  );

  const pick = (i) => {
    setIdx(i);
    setListOpen(false);
    onJump(results[i].id);
  };

  const onKey = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      step(e.shiftKey ? -1 : 1);
    } else if (e.key === 'Escape') {
      onClose();
    }
  };

  const query = q.trim();
  const counter = !query
    ? ''
    : busy && total == null
      ? 'Searching…'
      : total === '0'
        ? 'No results'
        : `${idx + 1} of ${total ?? results.length}`;

  return (
    <>
      <div className="search-head">
        <button className="icon-btn" onClick={onClose} aria-label="Close search">
          ←
        </button>
        <input
          ref={input}
          className="search-input"
          type="search"
          placeholder="Search in this chat"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
          enterKeyHint="search"
        />
      </div>
      {query && (
        <div className="search-nav">
          <span className="search-count">{counter}</span>
          <button
            className="icon-btn"
            onClick={() => setListOpen((o) => !o)}
            disabled={!results.length}
            title={listOpen ? 'Back to chat' : 'Show results as a list'}
            aria-pressed={listOpen}
          >
            {listOpen ? '💬' : '☰'}
          </button>
          <button
            className="icon-btn"
            onClick={() => step(1)}
            disabled={!results.length || (idx >= results.length - 1 && !hasMore)}
            title="Older match" aria-label="Older match">
            ▲
          </button>
          <button className="icon-btn" onClick={() => step(-1)} disabled={idx <= 0} title="Newer match" aria-label="Newer match">
            ▼
          </button>
        </div>
      )}
      {listOpen && results.length > 0 && (
        <div className="search-list">
          <ul>
            {results.map((r, i) => (
              <li key={r.id}>
                <button className={i === idx ? 'current' : ''} onClick={() => pick(i)}>
                  <span className="search-row-head">
                    <strong>{r.sender_name || 'Unknown'}</strong>
                    <span className="muted small">
                      {new Date(r.date).toLocaleDateString()} {formatTime(r.date)}
                    </span>
                  </span>
                  <span className="search-snippet">
                    <RichText
                      text={snippetAround(r.text || r.file_name || MEDIA_LABEL[r.media_kind] || '', query)}
                      highlight={query}
                    />
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {hasMore && (
            <button className="link-btn search-more" onClick={loadMore}>
              Load more results
            </button>
          )}
        </div>
      )}
    </>
  );
}
