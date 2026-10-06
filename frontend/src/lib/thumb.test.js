import { describe, expect, it } from 'vitest';
import { strippedToJpeg } from './thumb.js';

describe('strippedToJpeg', () => {
  it('rebuilds a JPEG with dimensions patched in', () => {
    const out = strippedToJpeg(new Uint8Array([1, 40, 30, 7, 8]));
    expect([out[0], out[1]]).toEqual([0xff, 0xd8]);
    expect(out[164]).toBe(40);
    expect(out[166]).toBe(30);
    expect([...out.slice(-4)]).toEqual([7, 8, 0xff, 0xd9]);
    expect(out.length).toBe(623 + 2 + 2);
  });
  it('passes through non-stripped data', () => {
    const d = new Uint8Array([2, 3, 4]);
    expect(strippedToJpeg(d)).toBe(d);
  });
});
