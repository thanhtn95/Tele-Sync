import React, { useState } from 'react';
import { buildSegments, entityHref, entityText } from '../lib/entities.js';

function Spoiler({ children }) {
  const [shown, setShown] = useState(false);
  return (
    <span className={shown ? 'spoiler shown' : 'spoiler'} onClick={() => setShown(true)}>
      {children}
    </span>
  );
}

const WRAP = {
  MessageEntityBold: (c, k) => <strong key={k}>{c}</strong>,
  MessageEntityItalic: (c, k) => <em key={k}>{c}</em>,
  MessageEntityUnderline: (c, k) => <u key={k}>{c}</u>,
  MessageEntityStrike: (c, k) => <s key={k}>{c}</s>,
  MessageEntityCode: (c, k) => <code key={k}>{c}</code>,
  MessageEntitySpoiler: (c, k) => <Spoiler key={k}>{c}</Spoiler>,
  MessageEntityHashtag: (c, k) => <span key={k} className="ent-tag">{c}</span>,
  MessageEntityCashtag: (c, k) => <span key={k} className="ent-tag">{c}</span>,
  MessageEntityBotCommand: (c, k) => <span key={k} className="ent-tag">{c}</span>,
  MessageEntityMentionName: (c, k) => <span key={k} className="ent-mention">{c}</span>,
};

// Block-level entities (pre, blockquote) group consecutive segments into one element.
const BLOCKS = new Set(['MessageEntityPre', 'MessageEntityBlockquote']);

function renderInline(text, seg, key) {
  let node = seg.text;
  // Inline formatting first (inner), links last (outermost).
  const sorted = [...seg.ents].sort((a, b) => (entityHref(a, '') !== null) - (entityHref(b, '') !== null));
  for (const e of sorted) {
    if (BLOCKS.has(e._)) continue;
    const href = entityHref(e, entityText(text, e));
    if (href !== null) {
      node = (
        <a key={key} href={href} target="_blank" rel="noopener noreferrer">
          {node}
        </a>
      );
    } else if (WRAP[e._]) {
      node = WRAP[e._](node, key);
    }
  }
  return <React.Fragment key={key}>{node}</React.Fragment>;
}

export default function RichText({ text, entities }) {
  if (!text) return null;
  const segs = buildSegments(text, entities);
  const out = [];
  let i = 0;
  while (i < segs.length) {
    const block = segs[i].ents.find((e) => BLOCKS.has(e._));
    if (!block) {
      out.push(renderInline(text, segs[i], i));
      i++;
      continue;
    }
    const children = [];
    const start = i;
    while (i < segs.length && segs[i].ents.includes(block)) {
      children.push(renderInline(text, segs[i], i));
      i++;
    }
    out.push(
      block._ === 'MessageEntityPre' ? (
        <pre key={`b${start}`} data-lang={block.language || undefined}>
          <code>{children}</code>
        </pre>
      ) : (
        <blockquote key={`b${start}`}>{children}</blockquote>
      ),
    );
  }
  return <span className="rich">{out}</span>;
}
