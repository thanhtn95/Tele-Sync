import React, { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { MEDIA_LABEL } from './Message.jsx';

const MAX = 4096; // Telegram's message length limit

/** Message box at the bottom of a chat: sends as your Telegram account. */
export default function Composer({ chatId, replyTo, onCancelReply, onSent }) {
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);
  const box = useRef(null);

  // Keep a draft per chat while you browse around.
  useEffect(() => {
    setText(sessionStorage.getItem(`draft:${chatId}`) || '');
    setError(null);
  }, [chatId]);
  useEffect(() => {
    try {
      if (text) sessionStorage.setItem(`draft:${chatId}`, text);
      else sessionStorage.removeItem(`draft:${chatId}`);
    } catch {
      /* storage unavailable: drafts just aren't kept */
    }
  }, [chatId, text]);

  // Grow with the text, up to ~6 lines.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
  }, [text]);

  useEffect(() => {
    if (replyTo) box.current?.focus();
  }, [replyTo]);

  const send = async () => {
    const t = text.trim();
    if (!t || sending) return;
    setSending(true);
    setError(null);
    try {
      const r = await api.send(chatId, t, replyTo?.id);
      setText('');
      onSent(r.id);
    } catch (e) {
      setError(e.message.replace(/^\d+: /, ''));
    } finally {
      setSending(false);
      box.current?.focus();
    }
  };

  const onKey = (e) => {
    // Enter sends on a physical keyboard; Shift+Enter (and the phone's Enter key) adds a line.
    const touch = window.matchMedia('(pointer: coarse)').matches;
    if (e.key === 'Enter' && !e.shiftKey && !touch && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    } else if (e.key === 'Escape' && replyTo) {
      onCancelReply();
    }
  };

  const replyText = replyTo && (replyTo.text || MEDIA_LABEL[replyTo.media?.kind] || 'Message');
  return (
    <div className="composer">
      {error && <div className="composer-error">{error}</div>}
      {replyTo && (
        <div className="composer-reply">
          <span className="composer-reply-icon">↩</span>
          <span className="composer-reply-body">
            <span className="reply-name">Reply to {replyTo.out ? 'yourself' : replyTo.sender_name || 'message'}</span>
            <span className="reply-text">{replyText}</span>
          </span>
          <button className="icon-btn" onClick={onCancelReply} aria-label="Cancel reply">
            ✕
          </button>
        </div>
      )}
      <div className="composer-row">
        <textarea
          ref={box}
          rows={1}
          value={text}
          maxLength={MAX}
          placeholder="Message"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          enterKeyHint="enter"
        />
        <button
          className="send-btn"
          onClick={send}
          disabled={!text.trim() || sending}
          aria-label="Send"
          title="Send (Enter)"
        >
          {sending ? '…' : '➤'}
        </button>
      </div>
      {text.length > MAX - 200 && <div className="muted small composer-count">{MAX - text.length} characters left</div>}
    </div>
  );
}
