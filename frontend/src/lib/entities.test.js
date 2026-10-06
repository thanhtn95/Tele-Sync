import { describe, expect, it } from 'vitest';
import { buildSegments, entityHref } from './entities.js';

describe('buildSegments', () => {
  it('returns plain text when no entities', () => {
    expect(buildSegments('hi', [])).toEqual([{ text: 'hi', ents: [] }]);
  });

  it('handles nesting and overlap', () => {
    const bold = { _: 'MessageEntityBold', offset: 0, length: 9 };
    const link = { _: 'MessageEntityTextUrl', offset: 6, length: 7, url: 'https://x' };
    const segs = buildSegments('hello world foo', [bold, link]);
    expect(segs.map((s) => s.text)).toEqual(['hello ', 'wor', 'ld f', 'oo']);
    expect(segs[1].ents).toEqual([bold, link]);
    expect(segs[2].ents).toEqual([link]);
    expect(segs[3].ents).toEqual([]);
  });

  it('uses UTF-16 offsets (emoji = 2 units)', () => {
    const text = '😀 bold';
    const segs = buildSegments(text, [{ _: 'MessageEntityBold', offset: 3, length: 4 }]);
    expect(segs.map((s) => s.text)).toEqual(['😀 ', 'bold']);
  });

  it('ignores out-of-range entities', () => {
    expect(buildSegments('abc', [{ _: 'MessageEntityBold', offset: 10, length: 2 }])).toEqual([
      { text: 'abc', ents: [] },
    ]);
  });
});

describe('entityHref', () => {
  it('builds hrefs', () => {
    expect(entityHref({ _: 'MessageEntityUrl' }, 'example.com')).toBe('https://example.com');
    expect(entityHref({ _: 'MessageEntityUrl' }, 'http://a.b')).toBe('http://a.b');
    expect(entityHref({ _: 'MessageEntityMention' }, '@durov')).toBe('https://t.me/durov');
    expect(entityHref({ _: 'MessageEntityBold' }, 'x')).toBe(null);
  });
});
