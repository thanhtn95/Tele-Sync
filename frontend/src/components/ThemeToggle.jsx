import React, { useEffect, useState } from 'react';
import { applyTheme, getTheme, nextTheme, setTheme } from '../lib/theme.js';

const LABEL = { system: '🖥 System', light: '☀️ Light', dark: '🌙 Dark' };

// One shared state so every mounted toggle stays in sync.
const listeners = new Set();
let current = getTheme();

export default function ThemeToggle() {
  const [theme, setLocal] = useState(current);
  useEffect(() => {
    listeners.add(setLocal);
    // "System" must react when the OS switches light/dark (e.g. at sunset).
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onOs = () => current === 'system' && applyTheme('system');
    mq.addEventListener('change', onOs);
    return () => {
      listeners.delete(setLocal);
      mq.removeEventListener('change', onOs);
    };
  }, []);
  const cycle = () => {
    current = nextTheme(current);
    setTheme(current);
    listeners.forEach((fn) => fn(current));
  };
  return (
    <button type="button" className="theme-btn" onClick={cycle} title="Theme: click to switch System → Light → Dark">
      {LABEL[theme]}
    </button>
  );
}
