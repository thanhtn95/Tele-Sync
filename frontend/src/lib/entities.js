// Split message text into segments, each carrying the Telegram entities that cover it.
// Entity offsets/lengths are in UTF-16 code units, which is exactly how JS strings index.

/** @returns {{text: string, ents: object[]}[]} */
export function buildSegments(text, entities) {
  if (!text) return [];
  const ents = (entities || []).filter((e) => e && e.length > 0 && e.offset < text.length);
  if (!ents.length) return [{ text, ents: [] }];
  const cuts = new Set([0, text.length]);
  for (const e of ents) {
    cuts.add(Math.max(0, e.offset));
    cuts.add(Math.min(text.length, e.offset + e.length));
  }
  const points = [...cuts].sort((a, b) => a - b);
  const segs = [];
  for (let i = 0; i < points.length - 1; i++) {
    const [s, t] = [points[i], points[i + 1]];
    if (s === t) continue;
    segs.push({ text: text.slice(s, t), ents: ents.filter((e) => e.offset <= s && e.offset + e.length >= t) });
  }
  return segs;
}

export function entityHref(e, segText) {
  switch (e._) {
    case 'MessageEntityTextUrl':
      return e.url;
    case 'MessageEntityUrl':
      return /^[a-z][a-z0-9+.-]*:/i.test(segText) ? segText : `https://${segText}`;
    case 'MessageEntityEmail':
      return `mailto:${segText}`;
    case 'MessageEntityPhone':
      return `tel:${segText.replace(/[^\d+]/g, '')}`;
    case 'MessageEntityMention':
      return `https://t.me/${segText.replace(/^@/, '')}`;
    default:
      return null;
  }
}

/** For entities that span several segments (e.g. a link with bold inside), the full text. */
export const entityText = (text, e) => text.slice(e.offset, e.offset + e.length);
