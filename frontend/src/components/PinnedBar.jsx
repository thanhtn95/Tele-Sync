import React, { useEffect, useState } from 'react';
import { formatTime } from '../lib/format.js';
import { thumbUrl } from '../lib/thumb.js';
import { MEDIA_LABEL } from './Message.jsx';

export function pinPreview(p) {
  return p.text || MEDIA_LABEL[p.media_kind] || (p.media_type ? 'Media' : 'Message');
}

function PinThumb({ pin }) {
  const t = ['photo', 'video', 'gif'].includes(pin.media_kind) && thumbUrl(pin.thumb_b64);
  return t ? <img className="pin-thumb" src={t} alt="" /> : null;
}

/**
 * Telegram-style pinned bar. `pins` newest first; `current` is the index shown.
 * Clicking jumps to that pin; the parent then moves `current` to the next older one.
 */
export default function PinnedBar({ pins, current, onJump }) {
  const [listOpen, setListOpen] = useState(false);
  useEffect(() => {
    if (!listOpen) return undefined;
    const onKey = (e) => e.key === 'Escape' && setListOpen(false);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [listOpen]);

  if (!pins.length) return null;
  const pin = pins[current] ?? pins[0];
  const n = pins.length;
  // Segment indicator: like Telegram, at most 4 segments visible, window follows `current`.
  const segs = Math.min(n, 4);
  const pos = n - 1 - current; // 0 = oldest
  const winStart = Math.min(Math.max(0, pos - 3), n - segs);

  return (
    <>
      <div className="pinned-bar">
        <button className="pinned-main" onClick={() => onJump(pin.id)} title="Go to pinned message">
          <span className="pin-segs" aria-hidden>
            {Array.from({ length: segs }, (_, i) => (
              <span key={i} className={winStart + i === pos ? 'on' : ''} />
            ))}
          </span>
          <PinThumb pin={pin} />
          <span className="pin-body">
            <span className="pin-title">
              Pinned Message{n > 1 ? ` #${pos + 1}` : ''}
            </span>
            <span className="pin-text">{pinPreview(pin)}</span>
          </span>
        </button>
        {n > 1 && (
          <button className="icon-btn pin-list-btn" onClick={() => setListOpen(true)} title="All pinned messages">
            ☰
          </button>
        )}
      </div>
      {listOpen && (
        <div className="pin-overlay" onClick={() => setListOpen(false)}>
          <div className="pin-list" onClick={(e) => e.stopPropagation()}>
            <div className="pin-list-head">
              <strong>{n} Pinned Messages</strong>
              <button className="icon-btn" onClick={() => setListOpen(false)} aria-label="Close">
                ✕
              </button>
            </div>
            <ul>
              {[...pins].reverse().map((p) => (
                <li key={p.id}>
                  <button
                    onClick={() => {
                      setListOpen(false);
                      onJump(p.id);
                    }}
                  >
                    <PinThumb pin={p} />
                    <span className="pin-body">
                      <span className="pin-title">{p.sender_name || 'Unknown'}</span>
                      <span className="pin-text">{pinPreview(p)}</span>
                    </span>
                    <span className="muted small">
                      {new Date(p.date).toLocaleDateString()} {formatTime(p.date)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </>
  );
}
