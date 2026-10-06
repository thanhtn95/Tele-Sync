import React, { useCallback, useEffect, useState } from 'react';
import ChatList from './components/ChatList.jsx';
import ChatView from './components/ChatView.jsx';
import Login from './components/Login.jsx';
import { api } from './lib/api.js';

// Tiny hash router: #/ (chat list), #/chat/<id> (viewer), #/chat/<id>/media|files|voice (tabs).
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

  if (error) return <div className="error-bar">{error}</div>;
  if (!auth) return null;
  if (!auth.authenticated) return <Login onLoggedIn={setAuth} />;

  const m = hash.match(/^#\/chat\/(-?\d+)(?:\/(media|files|voice))?/);
  return m ? (
    <ChatView chatId={Number(m[1])} tab={m[2] || 'chat'} />
  ) : (
    <ChatList onLogout={auth.auth_enabled ? logout : null} username={auth.username} />
  );
}
