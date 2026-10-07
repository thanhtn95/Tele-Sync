// "Hide media" switch for using the app in public, remembered per browser.
// Read synchronously at load so hidden media never flashes on the first render.
import { useEffect, useState } from 'react';

const KEY = 'hideMedia';
let hidden = (() => {
  try {
    return localStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
})();
const listeners = new Set();

export function setHideMedia(value) {
  hidden = value;
  try {
    if (value) localStorage.setItem(KEY, '1');
    else localStorage.removeItem(KEY);
  } catch {
    /* storage blocked: still applies for this page view */
  }
  listeners.forEach((fn) => fn(value));
}

/** True while media should be hidden; re-renders when the switch changes. */
export function useHideMedia() {
  const [h, setH] = useState(hidden);
  useEffect(() => {
    listeners.add(setH);
    setH(hidden);
    return () => listeners.delete(setH);
  }, []);
  return h;
}

/** Kinds that show pictures (voice notes and files are left as they are). */
export const VISUAL_KINDS = new Set(['photo', 'video', 'gif', 'sticker']);
