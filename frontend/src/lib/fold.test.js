import { describe, expect, it } from 'vitest';
import { findMatches, fold, snippetAround } from './fold.js';

describe('fold / findMatches', () => {
  it('ignores accents, case and đ', () => {
    expect(fold('Tiếng Việt ĐƯỜNG')).toBe('tieng viet duong');
    expect(findMatches('Hôm nay Tiếng Việt và tiếng Anh', 'tieng')).toEqual([
      { offset: 8, length: 5 },
      { offset: 22, length: 5 },
    ]);
    expect(findMatches('Đường đi', 'DUONG DI')).toEqual([{ offset: 0, length: 8 }]);
  });

  it('works with decomposed input and emoji (UTF-16 offsets)', () => {
    const text = '😀 Việt nam'; // decomposed "Việt"
    const [m] = findMatches(text, 'viet');
    expect(text.slice(m.offset, m.offset + m.length)).toBe('Việt');
  });

  it('handles no match / empty query', () => {
    expect(findMatches('abc', 'x')).toEqual([]);
    expect(findMatches('abc', '  ')).toEqual([]);
    expect(findMatches('', 'a')).toEqual([]);
  });

  it('snippets around the first match', () => {
    const t = 'x'.repeat(100) + ' Tiếng Việt ' + 'y'.repeat(100);
    const s = snippetAround(t, 'viet', 10);
    expect(s.startsWith('…') && s.endsWith('…') && s.includes('Việt')).toBe(true);
  });
});
