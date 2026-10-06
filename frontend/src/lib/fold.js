// Accent- and case-insensitive matching (mirrors the server's fold_text):
// "tieng viet" matches "Tiếng Việt", "duong" matches "Đường".

const foldChar = (c) =>
  c === 'đ' || c === 'Đ' ? 'd' : c.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

export function fold(s) {
  let out = '';
  for (const c of s) out += foldChar(c);
  return out;
}

/** All non-overlapping matches of `query` in `text`, as [{offset, length}] in UTF-16 units of `text`. */
export function findMatches(text, query) {
  if (!text || !query) return [];
  const q = fold(query.trim());
  if (!q) return [];
  // Folded text plus, for each folded code unit, where its source char starts/ends.
  let folded = '';
  const start = [];
  const end = [];
  let pos = 0;
  for (const c of text) {
    const f = foldChar(c);
    for (let k = 0; k < f.length; k++) {
      start.push(pos);
      end.push(pos + c.length);
    }
    folded += f;
    pos += c.length;
  }
  const out = [];
  let i = folded.indexOf(q);
  while (i !== -1) {
    const s = start[i];
    const e = end[i + q.length - 1];
    out.push({ offset: s, length: e - s });
    i = folded.indexOf(q, i + q.length);
  }
  return out;
}

/** A short excerpt of `text` around the first match, for result lists. */
export function snippetAround(text, query, radius = 40) {
  if (!text) return '';
  const m = findMatches(text, query)[0];
  if (!m || text.length <= radius * 2) return text.slice(0, radius * 2);
  const a = Math.max(0, m.offset - radius);
  const b = Math.min(text.length, m.offset + m.length + radius);
  return (a > 0 ? '…' : '') + text.slice(a, b) + (b < text.length ? '…' : '');
}
