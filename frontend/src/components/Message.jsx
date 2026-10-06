import React, { useState } from 'react';
import { fileUrl } from '../lib/api.js';
import { formatTime, initials, peerColor } from '../lib/format.js';
import Media from './Media.jsx';
import RichText from './RichText.jsx';

export const MEDIA_LABEL = {
  photo: '🖼 Photo',
  video: '🎬 Video',
  gif: 'GIF',
  voice: '🎤 Voice message',
  audio: '🎵 Audio',
  sticker: 'Sticker',
  document: '📎 File',
};

const OTHER_MEDIA_LABEL = {
  MessageMediaWebPage: null, // link preview: the link is already in the text
  MessageMediaGeo: '📍 Location',
  MessageMediaGeoLive: '📍 Live location',
  MessageMediaVenue: '📍 Venue',
  MessageMediaContact: '👤 Contact',
  MessageMediaPoll: '📊 Poll',
  MessageMediaDice: '🎲 Dice',
  MessageMediaGame: '🎮 Game',
  MessageMediaStory: 'Story',
};

export function Avatar({ name, path, id, size = 34 }) {
  const [broken, setBroken] = useState(false);
  const style = { width: size, height: size, fontSize: size * 0.4 };
  if (path && !broken) {
    return <img className="avatar" style={style} src={fileUrl(path)} alt="" loading="lazy" onError={() => setBroken(true)} />;
  }
  return (
    <span className="avatar" style={{ ...style, background: peerColor(id) }}>
      {initials(name)}
    </span>
  );
}

function ReplyPreview({ reply, onJump }) {
  const body = reply.found
    ? reply.text || MEDIA_LABEL[reply.media_kind] || 'Message'
    : 'Message not synced';
  return (
    <button className="reply" onClick={() => reply.found && onJump(reply.message_id)} disabled={!reply.found}>
      <span className="reply-name">{reply.found ? reply.sender_name || 'Unknown' : 'Reply'}</span>
      <span className="reply-text">{body}</span>
    </button>
  );
}

function AlbumGrid({ msgs, onOpen }) {
  const items = msgs.filter((m) => m.media);
  const n = items.length;
  return (
    <div className={`album album-${Math.min(n, 5)}`}>
      {items.map((m) => (
        <div className="album-cell" key={m.id}>
          <Media media={m.media} fill onOpen={() => onOpen(m)} />
        </div>
      ))}
    </div>
  );
}

/**
 * One bubble. `msgs` has several entries for an album (same grouped_id).
 */
export default function MessageBubble({ msgs, showName, showAvatar, isGroup, highlighted, onJump, onOpen }) {
  const first = msgs[0];
  const last = msgs[msgs.length - 1];

  if (first.service) {
    const pin = first.media_type == null && first.reply && /pinned$/.test(first.service);
    if (pin) {
      const r = first.reply;
      const quoted = r.found ? r.text || MEDIA_LABEL[r.media_kind] || 'a message' : 'a message';
      return (
        <div className="service-row">
          <button
            className="service service-link"
            onClick={() => r.found && onJump(r.message_id)}
            disabled={!r.found}
          >
            {first.service} «{quoted.length > 60 ? `${quoted.slice(0, 60)}…` : quoted}»
          </button>
        </div>
      );
    }
    return (
      <div className="service-row">
        <span className="service">{/pinned$/.test(first.service) ? `${first.service} a message` : first.service}</span>
      </div>
    );
  }

  const captioned = msgs.find((m) => m.text) || first;
  const media = msgs.length > 1 ? null : first.media;
  const isSticker = media?.kind === 'sticker';
  const isVisual = msgs.length > 1 || ['photo', 'video', 'gif'].includes(media?.kind);
  const otherLabel = !first.media && first.media_type ? OTHER_MEDIA_LABEL[first.media_type] : null;
  const edited = msgs.some((m) => m.edit_date);
  const out = first.out;

  return (
    <div className={`row ${out ? 'out' : 'in'}${highlighted ? ' highlight' : ''}`}>
      {isGroup && !out && (
        <div className="avatar-slot">
          {showAvatar && <Avatar name={first.sender_name} path={first.sender_avatar} id={first.sender_id} />}
        </div>
      )}
      <div
        className={`bubble${isSticker ? ' bare' : ''}${isVisual ? ' visual' : ''}${
          isVisual && !captioned.text ? ' media-only' : ''
        }`}
      >
        {showName && !out && (
          <div className="sender" style={{ color: `${peerColor(first.sender_id)}` }}>
            {first.sender_name || 'Unknown'}
          </div>
        )}
        {first.fwd_from_name && (
          <div className="fwd">
            Forwarded from <strong>{first.fwd_from_name}</strong>
          </div>
        )}
        {first.reply && <ReplyPreview reply={first.reply} onJump={onJump} />}
        {msgs.length > 1 ? <AlbumGrid msgs={msgs} onOpen={onOpen} /> : media && <Media media={media} onOpen={() => onOpen(first)} />}
        {otherLabel && <div className="other-media">{otherLabel}</div>}
        {captioned.text && (
          <div className="text">
            <RichText text={captioned.text} entities={captioned.entities} />
          </div>
        )}
        <span className="time" title={new Date(last.date).toLocaleString()}>
          {msgs.some((m) => m.pinned) && (
            <span className="pin-icon" title="Pinned" aria-label="Pinned">
              📌
            </span>
          )}
          {edited && 'edited '}
          {formatTime(last.date)}
        </span>
      </div>
    </div>
  );
}
