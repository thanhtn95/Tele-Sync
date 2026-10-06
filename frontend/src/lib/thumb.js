// Inflate Telegram "stripped" thumbnails (PhotoStrippedSize) into a tiny JPEG.
// Telegram strips the constant JPEG header/footer; we add them back.
// Port of tdesktop's Images::FromInlineBytes (same as telethon.utils.stripped_photo_to_jpg).
const HEADER_B64 =
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDACgcHiMeGSgjISMtKygwPGRBPDc3PHtYXUlkkYCZlo+AjIqgtObDoKrarYqMyP/L2u71////m8H////6/+b9//j/2wBDASstLTw1PHZBQXb4pYyl+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj4+Pj/wAARCAAAAAADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwA=';

let header = null;
const cache = new Map();

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function strippedToJpeg(stripped) {
  if (stripped.length < 3 || stripped[0] !== 1) return stripped;
  header ??= b64ToBytes(HEADER_B64);
  const out = new Uint8Array(header.length + stripped.length - 3 + 2);
  out.set(header, 0);
  out[164] = stripped[1];
  out[166] = stripped[2];
  out.set(stripped.subarray(3), header.length);
  out[out.length - 2] = 0xff;
  out[out.length - 1] = 0xd9;
  return out;
}

/** base64 stripped thumb -> blob: URL (cached). */
export function thumbUrl(thumbB64) {
  if (!thumbB64) return null;
  let url = cache.get(thumbB64);
  if (!url) {
    const jpeg = strippedToJpeg(b64ToBytes(thumbB64));
    url = URL.createObjectURL(new Blob([jpeg], { type: 'image/jpeg' }));
    cache.set(thumbB64, url);
  }
  return url;
}
