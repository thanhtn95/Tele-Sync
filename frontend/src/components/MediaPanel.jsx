import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Virtuoso, VirtuosoGrid } from 'react-virtuoso';
import { api, fileUrl } from '../lib/api.js';
import { formatDuration, formatSize, formatTime } from '../lib/format.js';
import { thumbUrl } from '../lib/thumb.js';
import { useBaseUrl } from './Media.jsx';

const PAGE = 90;

function useItems(chatId, group) {
  const [items, setItems] = useState([]);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const busy = useRef(false);
  const ref = useRef({ items, hasMore });
  ref.current = { items, hasMore };

  const load = useCallback(
    async (reset) => {
      if (busy.current || (!reset && !ref.current.hasMore)) return;
      busy.current = true;
      setLoading(true);
      try {
        const last = reset ? null : ref.current.items[ref.current.items.length - 1];
        const r = await api.chatMedia(chatId, { group, before: last?.id, limit: PAGE });
        setItems((cur) => (reset ? r.items : [...cur, ...r.items]));
        setHasMore(r.has_more);
      } finally {
        busy.current = false;
        setLoading(false);
      }
    },
    [chatId, group],
  );

  useEffect(() => {
    setItems([]);
    setHasMore(true);
    load(true);
  }, [load]);

  return { items, hasMore, loading, loadMore: () => load(false) };
}

function Tile({ item, onOpen }) {
  const m = item.media;
  const { url } = useBaseUrl(m.file_path ? null : m.gphotos_media_id);
  const [ok, setOk] = useState(false);
  const thumb = thumbUrl(m.thumb_b64);
  const src = m.file_path
    ? m.kind === 'photo'
      ? fileUrl(m.file_path)
      : null
    : url
      ? `${url}=w300-h300-c`
      : null;
  return (
    <button className="tile" onClick={() => onOpen(item)}>
      {thumb && <img className="thumb-blur" src={thumb} alt="" aria-hidden />}
      {src && <img className={ok ? 'real loaded' : 'real'} src={src} alt="" loading="lazy" onLoad={() => setOk(true)} />}
      {!src && m.file_path && m.kind !== 'photo' && (
        <video className="real loaded" src={fileUrl(m.file_path)} preload="metadata" muted />
      )}
      {m.kind !== 'photo' && <span className="badge">{m.kind === 'gif' ? 'GIF' : formatDuration(m.duration)}</span>}
      {!m.gphotos_media_id && !m.file_path && <span className="tile-missing">!</span>}
    </button>
  );
}

const when = (d) => `${new Date(d).toLocaleDateString()} ${formatTime(d)}`;

function FileRow({ item, onShowInChat }) {
  const m = item.media;
  const name = m.file_name || m.kind;
  const ext = (name.split('.').pop() || 'file').slice(0, 4).toUpperCase();
  return (
    <div className="file-row">
      <span className="doc-icon">{ext}</span>
      <div className="doc-meta">
        {m.file_path ? (
          <a className="doc-name" href={fileUrl(m.file_path)} target="_blank" rel="noopener noreferrer" download>
            {name}
          </a>
        ) : (
          <span className="doc-name">{name}</span>
        )}
        <span className="muted small">
          {formatSize(m.size)} · {when(item.date)}
          {item.sender_name ? ` · ${item.sender_name}` : ''}
          {!m.file_path && ' · not downloaded'}
        </span>
        {m.kind === 'audio' && m.file_path && <audio controls preload="none" src={fileUrl(m.file_path)} />}
      </div>
      <button className="icon-btn" onClick={() => onShowInChat(item.id)} title="Show in chat">
        ↗
      </button>
    </div>
  );
}

function VoiceRow({ item, onShowInChat }) {
  const m = item.media;
  return (
    <div className="file-row">
      <div className="doc-meta">
        <span className="muted small">
          {item.sender_name || 'Unknown'} · {when(item.date)} · {formatDuration(m.duration)}
        </span>
        {m.file_path ? (
          <audio controls preload="none" src={fileUrl(m.file_path)} />
        ) : (
          <span className="muted small">not downloaded</span>
        )}
      </div>
      <button className="icon-btn" onClick={() => onShowInChat(item.id)} title="Show in chat">
        ↗
      </button>
    </div>
  );
}

const EMPTY = { media: 'No photos or videos synced.', files: 'No files synced.', voice: 'No voice messages synced.' };

/** Telegram-style shared media browser: Media grid, Files list, Voice list. */
export default function MediaPanel({ chatId, group, onOpen, onShowInChat }) {
  const { items, hasMore, loading, loadMore } = useItems(chatId, group);
  const footer = () => <div className="list-header muted small">{hasMore ? 'Loading…' : ''}</div>;
  if (!items.length) {
    return <div className="media-panel empty muted">{loading ? 'Loading…' : EMPTY[group]}</div>;
  }
  if (group === 'media') {
    return (
      <VirtuosoGrid
        className="media-panel"
        data={items}
        listClassName="tile-grid"
        computeItemKey={(_, it) => it.id}
        endReached={loadMore}
        overscan={600}
        itemContent={(_, it) => <Tile item={it} onOpen={onOpen} />}
        components={{ Footer: footer }}
      />
    );
  }
  const Row = group === 'voice' ? VoiceRow : FileRow;
  return (
    <Virtuoso
      className="media-panel"
      data={items}
      computeItemKey={(_, it) => it.id}
      endReached={loadMore}
      itemContent={(_, it) => <Row item={it} onShowInChat={onShowInChat} />}
      components={{ Footer: footer }}
    />
  );
}
