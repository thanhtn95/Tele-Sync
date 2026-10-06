// Fetches Google Photos baseUrls in batches. baseUrls expire after ~60 min, so they
// are only kept in memory and re-fetched (force) when an <img>/<video> fails to load.
import { api } from './api.js';

const TTL_MS = 40 * 60 * 1000;
const cache = new Map(); // id -> { url, at }
let queue = new Map(); // id -> [resolve, reject][]
let forceQueue = false;
let timer = null;

function flush() {
  timer = null;
  const batch = queue;
  const force = forceQueue;
  queue = new Map();
  forceQueue = false;
  const ids = [...batch.keys()];
  api
    .gphotosUrls(ids, force)
    .then(({ urls }) => {
      const now = Date.now();
      for (const id of ids) {
        const url = urls[id];
        if (url) cache.set(id, { url, at: now });
        for (const [res, rej] of batch.get(id)) url ? res(url) : rej(new Error('not found'));
      }
    })
    .catch((err) => {
      for (const waiters of batch.values()) for (const [, rej] of waiters) rej(err);
    });
}

/** Resolve a gphotos media id to a fresh baseUrl. */
export function getBaseUrl(id, { force = false } = {}) {
  const hit = cache.get(id);
  if (hit && !force && Date.now() - hit.at < TTL_MS) return Promise.resolve(hit.url);
  if (force) {
    cache.delete(id);
    forceQueue = true;
  }
  return new Promise((resolve, reject) => {
    if (!queue.has(id)) queue.set(id, []);
    queue.get(id).push([resolve, reject]);
    if (!timer) timer = setTimeout(flush, 30); // coalesce one render's worth of requests
  });
}
