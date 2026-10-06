import React, { useCallback, useEffect, useRef, useState } from 'react';
import { fileUrl } from '../lib/api.js';
import { formatDuration, formatSize } from '../lib/format.js';
import { getBaseUrl } from '../lib/gphotos.js';
import { thumbUrl } from '../lib/thumb.js';

const Lottie = React.lazy(() => import('./Lottie.jsx'));

/** Fresh Google Photos baseUrl for a media id; call `refresh()` when the URL has expired. */
export function useBaseUrl(mediaId) {
  const [url, setUrl] = useState(null);
  const [failed, setFailed] = useState(false);
  const retried = useRef(false);
  useEffect(() => {
    if (!mediaId) return undefined;
    let alive = true;
    getBaseUrl(mediaId)
      .then((u) => alive && setUrl(u))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [mediaId]);
  const refresh = useCallback(() => {
    if (!mediaId || retried.current) {
      setFailed(true);
      return;
    }
    retried.current = true; // one forced refresh per mount, avoids loops on broken items
    getBaseUrl(mediaId, { force: true })
      .then((u) => setUrl(u))
      .catch(() => setFailed(true));
  }, [mediaId]);
  return { url, failed, refresh };
}

/** Where a photo/video lives: Google Photos (baseUrl) or local disk (/files). */
function useSources(media) {
  const { url, failed, refresh } = useBaseUrl(media.gphotos_media_id);
  if (media.file_path) {
    const f = fileUrl(media.file_path);
    return { thumb: f, full: f, video: f, ready: true, failed: false, refresh: () => {} };
  }
  if (media.gphotos_media_id && url) {
    return { thumb: `${url}=w800-h800`, full: `${url}=d`, video: `${url}=dv`, ready: true, failed, refresh };
  }
  return { ready: false, failed: failed || !media.gphotos_media_id, refresh };
}

function aspectStyle(media, fill) {
  if (fill) return undefined;
  const w = media.width || 4;
  const h = media.height || 3;
  const width = Math.min(360, Math.max(140, w >= h ? 360 : (360 * w) / h));
  return { width, aspectRatio: `${w} / ${h}`, maxHeight: 420 };
}

function Missing({ media }) {
  const why = media.error?.startsWith('skipped:')
    ? media.error.slice(8).trim()
    : media.error
      ? 'Download failed'
      : 'Not downloaded';
  return (
    <div className="media-missing" title={media.error || ''}>
      {why}
    </div>
  );
}

export function PhotoMedia({ media, fill, onOpen }) {
  const src = useSources(media);
  const [loaded, setLoaded] = useState(false);
  const placeholder = thumbUrl(media.thumb_b64);
  return (
    <div
      className={`media-photo${fill ? ' fill' : ''}`}
      style={aspectStyle(media, fill)}
      onClick={() => src.ready && onOpen?.({ kind: 'photo', src: src.full, onError: src.refresh })}
    >
      {placeholder && <img className="thumb-blur" src={placeholder} alt="" aria-hidden />}
      {src.ready && (
        <img
          className={loaded ? 'real loaded' : 'real'}
          src={src.thumb}
          alt=""
          loading="lazy"
          onLoad={() => setLoaded(true)}
          onError={() => {
            setLoaded(false);
            src.refresh();
          }}
        />
      )}
      {!src.ready && src.failed && <Missing media={media} />}
    </div>
  );
}

export function VideoMedia({ media, fill, onOpen }) {
  const src = useSources(media);
  const gif = media.kind === 'gif';
  const placeholder = thumbUrl(media.thumb_b64);
  const [posterOk, setPosterOk] = useState(true);
  const local = Boolean(media.file_path);
  return (
    <div
      className={`media-photo media-video${fill ? ' fill' : ''}`}
      style={aspectStyle(media, fill)}
      onClick={() => src.ready && onOpen?.({ kind: 'video', src: src.video, loop: gif, onError: src.refresh })}
    >
      {placeholder && <img className="thumb-blur" src={placeholder} alt="" aria-hidden />}
      {src.ready && local && <video className="real loaded" src={src.video} preload="metadata" muted />}
      {src.ready && !local && posterOk && (
        <img
          className="real loaded"
          src={src.thumb}
          alt=""
          loading="lazy"
          onError={() => (src.failed ? setPosterOk(false) : src.refresh())}
        />
      )}
      {!src.ready && src.failed && <Missing media={media} />}
      <span className="play">▶</span>
      <span className="badge">{gif ? 'GIF' : formatDuration(media.duration)}</span>
    </div>
  );
}

function VoiceMedia({ media }) {
  if (!media.file_path) return <Missing media={media} />;
  return (
    <div className="media-voice">
      <audio controls preload="none" src={fileUrl(media.file_path)} />
      <span className="muted">{formatDuration(media.duration)}</span>
    </div>
  );
}

function StickerMedia({ media }) {
  const placeholder = thumbUrl(media.thumb_b64);
  const fallback = placeholder ? <img className="sticker" src={placeholder} alt="sticker" /> : <span className="sticker-ph">🖼</span>;
  if (!media.file_path) return fallback;
  const url = fileUrl(media.file_path);
  if (media.mime === 'video/webm') {
    return <video className="sticker" src={url} autoPlay loop muted playsInline />;
  }
  if (media.mime === 'application/x-tgsticker' || media.file_path.endsWith('.tgs')) {
    return (
      <React.Suspense fallback={fallback}>
        <Lottie url={url} fallback={fallback} />
      </React.Suspense>
    );
  }
  return <img className="sticker" src={url} alt="sticker" loading="lazy" />;
}

function DocumentMedia({ media }) {
  const name = media.file_name || `${media.kind}${media.mime ? ` (${media.mime})` : ''}`;
  const ext = (name.split('.').pop() || 'file').slice(0, 4).toUpperCase();
  const body = (
    <>
      <span className="doc-icon">{ext}</span>
      <span className="doc-meta">
        <span className="doc-name">{name}</span>
        <span className="muted">
          {formatSize(media.size)}
          {!media.file_path && ` · ${media.error ? 'download failed' : 'not downloaded'}`}
        </span>
      </span>
    </>
  );
  return (
    <div className="media-doc-wrap">
      {media.file_path ? (
        <a className="media-doc" href={fileUrl(media.file_path)} target="_blank" rel="noopener noreferrer" download>
          {body}
        </a>
      ) : (
        <div className="media-doc">{body}</div>
      )}
      {media.kind === 'audio' && media.file_path && (
        <audio controls preload="none" src={fileUrl(media.file_path)} />
      )}
    </div>
  );
}

export default function Media({ media, fill, onOpen }) {
  switch (media.kind) {
    case 'photo':
      return <PhotoMedia media={media} fill={fill} onOpen={onOpen} />;
    case 'video':
    case 'gif':
      return <VideoMedia media={media} fill={fill} onOpen={onOpen} />;
    case 'voice':
      return <VoiceMedia media={media} />;
    case 'sticker':
      return <StickerMedia media={media} />;
    default:
      return <DocumentMedia media={media} />;
  }
}

export function Lightbox({ item, onClose }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  if (!item) return null;
  return (
    <div className="lightbox" onClick={onClose}>
      <button className="lightbox-close" onClick={onClose} aria-label="Close">
        ✕
      </button>
      <div onClick={(e) => e.stopPropagation()}>
        {item.kind === 'photo' ? (
          <img src={item.src} alt="" onError={item.onError} />
        ) : (
          <video src={item.src} controls autoPlay loop={item.loop} onError={item.onError} />
        )}
      </div>
    </div>
  );
}
