import React, { useCallback, useEffect, useState } from 'react';
import ChatList from './components/ChatList.jsx';
import ChatView from './components/ChatView.jsx';
import WebArchive from './components/WebArchive.jsx';
import Login from './components/Login.jsx';
import { api } from './lib/api.js';

// Tiny hash router: #/ (chat list), #/chat/<id> (viewer), #/chat/<id>/media|files|voice (tabs),
// #/archive (website archives), #/archive/<job id>.
function useHash() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return hash;
}

export default function App() {
  const hash = useHash();
  const [auth, setAuth] = useState(null); // {authenticated, auth_enabled, username}
  const [error, setError] = useState(null);

  const check = useCallback(() => {
    api.me().then(setAuth, (e) => setError(e.message));
  }, []);

  useEffect(() => {
    check();
    const onExpired = () => setAuth((a) => (a ? { ...a, authenticated: false } : a));
    window.addEventListener('auth:required', onExpired);
    return () => window.removeEventListener('auth:required', onExpired);
  }, [check]);

  const logout = useCallback(async () => {
    await api.logout().catch(() => {});
    setAuth((a) => ({ ...a, authenticated: false, username: null }));
  }, []);

  // Server unreachable or restarting (e.g. during an update): retry by itself.
  useEffect(() => {
    if (!error) return undefined;
    const t = setTimeout(() => {
      setError(null);
      check();
    }, 5000);
    return () => clearTimeout(t);
  }, [error, check]);

  if (error) {
    return (
      <div className="offline">
        <p>
          <strong>Can't reach the server</strong>
        </p>
        <p className="muted small">{error} — retrying every 5 seconds. It may be restarting after an update.</p>
        <button onClick={() => (setError(null), check())}>Retry now</button>
      </div>
    );
  }
  if (!auth) return null;
  if (!auth.authenticated) return <Login onLoggedIn={setAuth} />;

  const a = hash.match(/^#\/archive(?:\/(\d+))?/);
  if (a) return <WebArchive jobId={a[1] ? Number(a[1]) : null} />;
  const m = hash.match(/^#\/chat\/(-?\d+)(?:\/(media|files|voice))?(?:\?m=(\d+))?/);
  return m ? (
    <ChatView chatId={Number(m[1])} tab={m[2] || 'chat'} openAt={m[3] ? Number(m[3]) : null} />
  ) : (
    <ChatList onLogout={auth.auth_enabled ? logout : null} username={auth.username} />
  );
}
