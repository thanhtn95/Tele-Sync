// Theme preference: 'system' | 'light' | 'dark', remembered per browser.
const KEY = 'theme';
const ORDER = ['system', 'light', 'dark'];

export function getTheme() {
  try {
    const t = localStorage.getItem(KEY);
    return ORDER.includes(t) ? t : 'system';
  } catch {
    return 'system';
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
  // Mobile browser toolbar colour follows the effective theme.
  const dark = theme === 'dark' || (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#17212b' : '#ffffff');
}

export function setTheme(theme) {
  try {
    if (theme === 'system') localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch {
    /* storage blocked: still applies for this page view */
  }
  applyTheme(theme);
}

export const nextTheme = (t) => ORDER[(ORDER.indexOf(t) + 1) % ORDER.length];
