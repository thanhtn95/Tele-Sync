import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api, fileUrl } from '../lib/api.js';
import { formatTime } from '../lib/format.js';
import { thumbUrl } from '../lib/thumb.js';
import { useBaseUrl } from './Media.jsx';

const VISUAL = { group: 'media' };

/** Resolve the full-size source of one item (Google Photos or local file). */
function useFullSource(media) {
  const { url, failed, refresh } = useBaseUrl(media?.file_path ? null : media?.gphotos_media_id);
  if (!media) return {};
  if (media.file_path) return { src: fileUrl(media.file_path), refresh: () => {} };
  if (url) return { src: media.kind === 'photo' ? `${url}=d` : `${url}=dv`, refresh };
  return { failed: failed || !media.gphotos_media_id, refresh };
}

/** One item; keyed by message id so its expired-link retry state is per item. */
function ViewerMedia({ media }) {
  const { src, failed, refresh } = useFullSource(media);
  const [errored, setErrored] = useState(false);
  useEffect(() => setErrored(false), [src]);
  const onError = () => {
    setErrored(true);
    refresh(); // baseUrls expire after ~1h: fetch a fresh one once
  };
  if (src && !errored) {
    return media.kind === 'photo' ? (
      <img src={src} alt="" onError={onError} />
    ) : (
      <video
        src={src}
        controls
        autoPlay
        loop={media.kind === 'gif'}
        muted={media.kind === 'gif'}
        playsInline
        onError={onError}
      />
    );
  }
  const placeholder = thumbUrl(media?.thumb_b64);
  return (
    <div className="viewer-ph">
      {placeholder && <img src={placeholder} alt="" className="thumb-blur" />}
      <span>{failed || errored ? 'Not available' : 'Loading…'}</span>
    </div>
  );
}

/**
 * Full-screen photo/video viewer with previous/next across the whole chat
 * (not only what is loaded): ‹ › buttons, ←/→ keys, swipe on touch screens.
 * `start` is a message-like object {id, date, text, sender_name, media}.
 */
export default function MediaViewer({ chatId, start, onClose, onShowInChat }) {
  const [item, setItem] = useState(start);
  const [nav, setNav] = useState({ older: undefined, newer: undefined }); // undefined = loading, null = none
  const touch = useRef(null);

  useEffect(() => setItem(start), [start]);

  // Neighbours of the current item, fetched from the server.
  useEffect(() => {
    if (!item) return undefined;
    let alive = true;
    setNav({ older: undefined, newer: undefined });
    Promise.all([
      api.chatMedia(chatId, { ...VISUAL, before: item.id, limit: 1 }),
      api.chatMedia(chatId, { ...VISUAL, after: item.id, limit: 1 }),
    ]).then(
      ([o, n]) => alive && setNav({ older: o.items[0] ?? null, newer: n.items[0] ?? null }),
      () => alive && setNav({ older: null, newer: null }),
    );
    return () => {
      alive = false;
    };
  }, [chatId, item?.id]);

  const navRef = useRef(nav);
  navRef.current = nav;
  const go = useCallback((dir) => {
    const target = dir < 0 ? navRef.current.older : navRef.current.newer;
    if (target) setItem(target);
  }, []);

  useEffect(() => {
    if (!item) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
      else if (e.key === 'ArrowLeft') go(-1);
      else if (e.key === 'ArrowRight') go(1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [item, go, onClose]);

  if (!item) return null;

  const onTouchStart = (e) => {
    const t = e.touches[0];
    touch.current = { x: t.clientX, y: t.clientY };
  };
  const onTouchEnd = (e) => {
    const s = touch.current;
    touch.current = null;
    if (!s) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - s.x;
    const dy = t.clientY - s.y;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) go(dx > 0 ? -1 : 1); // swipe right = older
    else if (dy > 90 && Math.abs(dy) > Math.abs(dx) * 1.5) onClose(); // swipe down closes
  };

  return (
    <div className="viewer" onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
      <div className="viewer-top" onClick={(e) => e.stopPropagation()}>
        <div className="viewer-meta">
          <strong>{item.sender_name || ''}</strong>
          <span className="small">
            {new Date(item.date).toLocaleDateString()} {formatTime(item.date)}
          </span>
        </div>
        <button onClick={() => onShowInChat(item.id)}>Show in chat</button>
        <button className="viewer-close" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </div>
      <div className="viewer-stage" onClick={onClose}>
        <div className="viewer-media" onClick={(e) => e.stopPropagation()}>
          <ViewerMedia key={item.id} media={item.media} />
        </div>
      </div>
      {nav.older && (
        <button className="viewer-nav prev" onClick={() => go(-1)} aria-label="Previous (older)">
          ‹
        </button>
      )}
      {nav.newer && (
        <button className="viewer-nav next" onClick={() => go(1)} aria-label="Next (newer)">
          ›
        </button>
      )}
      {item.text && (
        <div className="viewer-caption" onClick={(e) => e.stopPropagation()}>
          {item.text}
        </div>
      )}
    </div>
  );
}
