async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && !path.startsWith('/api/auth/')) {
    window.dispatchEvent(new Event('auth:required')); // session expired -> App shows the login form
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(`${res.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
  }
  return res.json();
}

export const api = {
  me: () => request('GET', '/api/auth/me'),
  login: (username, password) => request('POST', '/api/auth/login', { username, password }),
  logout: () => request('POST', '/api/auth/logout'),
  dialogs: (refresh = false) => request('GET', `/api/dialogs${refresh ? '?refresh=true' : ''}`),
  chat: (id) => request('GET', `/api/chats/${id}`),
  patchChat: (id, patch) => request('PATCH', `/api/chats/${id}`, patch),
  send: (id, text, replyTo) =>
    request('POST', `/api/chats/${id}/send`, replyTo ? { text, reply_to: replyTo } : { text }),
  pinned: (id) => request('GET', `/api/chats/${id}/pinned`),
  messages: (id, before, limit = 50) =>
    request('GET', `/api/chats/${id}/messages?limit=${limit}${before != null ? `&before=${before}` : ''}`),
  messagesAfter: (id, after, limit = 50) => request('GET', `/api/chats/${id}/messages?limit=${limit}&after=${after}`),
  messagesAround: (id, around, limit = 60) =>
    request('GET', `/api/chats/${id}/messages?limit=${limit}&around=${around}`),
  search: (id, q, before, limit = 50) =>
    request(
      'GET',
      `/api/chats/${id}/search?q=${encodeURIComponent(q)}&limit=${limit}${before != null ? `&before=${before}` : ''}`,
    ),
  chatMedia: (id, { group = 'media', before, after, limit = 60 } = {}) =>
    request(
      'GET',
      `/api/chats/${id}/media?group=${group}&limit=${limit}` +
        (before != null ? `&before=${before}` : '') +
        (after != null ? `&after=${after}` : ''),
    ),
  gphotosUrls: (mediaIds, force = false) => request('POST', '/api/gphotos/urls', { media_ids: mediaIds, force }),
  syncStatus: () => request('GET', '/api/sync/status'),
  syncRun: () => request('POST', '/api/sync/run'),
};

/** URL for a file stored on the VM disk (served by nginx). */
export const fileUrl = (path) => '/files/' + path.split('/').map(encodeURIComponent).join('/');
