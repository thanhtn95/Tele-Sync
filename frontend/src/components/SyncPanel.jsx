import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { formatAgo } from '../lib/format.js';

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
      {err && <div className="err small">{err}</div>}
    </div>
  );
}
