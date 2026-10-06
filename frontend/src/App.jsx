import React, { useEffect, useState } from 'react';
import ChatList from './components/ChatList.jsx';
import ChatView from './components/ChatView.jsx';

// Tiny hash router: #/ (chat list) and #/chat/<id> (viewer).
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
  const m = hash.match(/^#\/chat\/(-?\d+)/);
  return m ? <ChatView chatId={Number(m[1])} /> : <ChatList />;
}
