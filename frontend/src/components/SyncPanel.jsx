import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { formatAgo, formatSize } from '../lib/format.js';

const num = (n) => (n ?? 0).toLocaleString();

/** Where the archived media lives, and why some of it isn't stored. */
function MediaHealth({ m }) {
  const [open, setOpen] = useState(false);
  if (!m) return null;
  const parts = [
    m.in_gphotos > 0 && `${num(m.in_gphotos)} in Google Photos`,
    m.on_disk > 0 && `${num(m.on_disk)} on disk`,
    m.not_downloaded > 0 && `${num(m.not_downloaded)} not downloaded yet`,
    m.skipped > 0 && `${num(m.skipped)} skipped`,
  ].filter(Boolean);
  return (
    <div className="health small">
      <span className="health-label">Media</span>
      <span>
        {parts.join(' · ') || 'none yet'}
        {m.failed > 0 && (
          <>
            {' · '}
            <button className="link-btn err" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
              {num(m.failed)} failed {open ? '▴' : '▾'}
            </button>
          </>
        )}
      </span>
      {open && m.top_errors?.length > 0 && (
        <ul className="health-reasons">
          {m.top_errors.map((e) => (
            <li key={e.reason}>
              <strong>{num(e.n)}×</strong> {e.reason}
            </li>
          ))}
          <li className="muted">Failed files are retried on each sync of chats with sync and “Include media” on.</li>
        </ul>
      )}
    </div>
  );
}

function DiskHealth({ d, low }) {
  if (!d) return null;
  const level = d.percent >= 90 ? 'bad' : d.percent >= 80 ? 'warn' : 'ok';
  return (
    <div className="health small">
      <span className="health-label">Disk</span>
      <span>
        {formatSize(d.used)} of {formatSize(d.total)} used ({Math.round(d.percent)}%) · {formatSize(d.free)} free
      </span>
      <div className={`meter ${level}`} role="meter" aria-valuenow={d.percent} aria-valuemin={0} aria-valuemax={100}>
        <span style={{ width: `${Math.min(100, d.percent)}%` }} />
      </div>
      {low && (
        <div className="err">
          Downloads paused: the disk is almost full. Free some space (e.g. old backups in /var/lib/tele-sync/backups);
          paused files are retried automatically.
        </div>
      )}
      {!low && level !== 'ok' && <div className="warn">The disk is getting full; downloads pause below 1.5 GB free.</div>}
    </div>
  );
}

export default function SyncPanel({ onStatus }) {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const timer = useRef(null);
  const onStatusRef = useRef(onStatus);
  onStatusRef.current = onStatus;

  const poll = useCallback(async () => {
    clearTimeout(timer.current);
    try {
      const s = await api.syncStatus();
      setSt(s);
      setErr(null);
      onStatusRef.current?.(s);
      timer.current = setTimeout(poll, s.running ? 3000 : 15000);
    } catch (e) {
      setErr(e.message);
      timer.current = setTimeout(poll, 15000);
    }
  }, []);

  useEffect(() => {
    poll();
    return () => clearTimeout(timer.current);
  }, [poll]);

  const run = async () => {
    try {
      await api.syncRun();
      setTimeout(poll, 500);
    } catch (e) {
      setErr(e.message);
    }
  };

  if (!st) return <div className="sync-panel muted">{err ?? 'Loading status…'}</div>;

  return (
    <div className="sync-panel">
      <div className="sync-line">
        <span className={`dot ${st.running ? 'busy' : st.last_error ? 'bad' : 'ok'}`} />
        {st.running ? (
          <span>
            Syncing <strong>{st.current_chat_title ?? '…'}</strong> · {st.progress} new messages
          </span>
        ) : (
          <span>
            Idle · last pass {formatAgo(st.last_pass_finished_at)}
            {st.next_pass_at && ` · next ${new Date(st.next_pass_at).toLocaleTimeString()}`}
          </span>
        )}
        <button onClick={run} disabled={st.running}>
          Sync now
        </button>
      </div>
      {st.flood_wait_until && (
        <div className="warn small">Telegram rate limit: waiting until {new Date(st.flood_wait_until).toLocaleTimeString()}</div>
      )}
      {st.telegram_configured && !st.telegram_authorised && (
        <div className="err small">Telegram session not authorised — run scripts/tg_login.py on the server.</div>
      )}
      {!st.gphotos_enabled && <div className="muted small">Google Photos not configured: photos/videos are kept on disk.</div>}
      {st.last_error && <div className="err small">{st.last_error}</div>}
      <MediaHealth m={st.media} />
      <DiskHealth d={st.disk} low={st.disk_low} />
      {err && <div className="err small">{err}</div>}
    </div>
  );
}
