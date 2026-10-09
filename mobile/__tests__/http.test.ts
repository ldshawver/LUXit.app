import { ApiClient, ApiError, NetworkError, type TokenStore } from '../src/api/http';
import type { Tokens } from '../src/api/types';

class MemTokens implements TokenStore {
  constructor(public t: Tokens | null) {}
  async get() { return this.t; }
  async set(t: Tokens) { this.t = t; }
  async clear() { this.t = null; }
}

const tokens = (n: number): Tokens => ({ access_token: `lxa_${n}`, refresh_token: `lxr_${n}`, access_expires_at: '', refresh_expires_at: '' });
const json = (status: number, body: unknown) => ({ ok: status < 400, status, json: async () => body }) as unknown as Response;

function client(store: MemTokens, handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const lost: string[] = [];
  const calls: { url: string; auth?: string; body?: string }[] = [];
  const c = new ApiClient({
    baseUrl: 'https://staging.luxit.app', tokens: store, sleep: async () => undefined, onAuthLost: (code) => lost.push(code),
    fetchImpl: async (url, init) => {
      calls.push({ url, auth: (init.headers as Record<string, string>)?.Authorization, body: init.body as string });
      return handler(url, init);
    },
  });
  return { c, lost, calls };
}

test('maps PWA-relative URLs onto the bearer-only prefix', () => {
  const { c } = client(new MemTokens(tokens(1)), () => json(200, {}));
  expect(c.mobileUrl('/api/inbox/messages/5/media/0')).toBe('https://staging.luxit.app/api/mobile/v1/inbox/messages/5/media/0');
  expect(c.mobileUrl('/api/mobile/v1/me')).toBe('https://staging.luxit.app/api/mobile/v1/me');
  expect(c.mobileUrl('https://cdn.example/x.png')).toBe('https://cdn.example/x.png');
});

test('expired access token: one refresh shared by concurrent requests, then retried', async () => {
  const store = new MemTokens(tokens(1));
  let refreshes = 0;
  const { c, calls } = client(store, async (url, init) => {
    if (url.endsWith('/auth/refresh')) {
      refreshes += 1;
      await new Promise((r) => setTimeout(r, 5));
      return json(200, tokens(2));
    }
    const auth = (init.headers as Record<string, string>).Authorization;
    return auth === 'Bearer lxa_2' ? json(200, { ok: true }) : json(401, { code: 'token_expired' });
  });
  const results = await Promise.all([c.get('/me'), c.get('/sync'), c.post('/devices/current', {})]);
  expect(results).toHaveLength(3);
  expect(refreshes).toBe(1);
  expect(store.t?.refresh_token).toBe('lxr_2');
  expect(calls.filter((x) => x.url.endsWith('/auth/refresh'))[0]!.body).toContain('lxr_1');
});

test('rejected refresh signs the app out once', async () => {
  const store = new MemTokens(tokens(1));
  const { c, lost } = client(store, (url) => (url.endsWith('/auth/refresh') ? json(401, { code: 'session_revoked' }) : json(401, { code: 'token_expired' })));
  await expect(c.get('/me')).rejects.toBeInstanceOf(ApiError);
  expect(store.t).toBeNull();
  expect(lost).toEqual(['session_revoked']);
});

test('revoked device (non-refreshable 401) signs out without trying to refresh', async () => {
  const store = new MemTokens(tokens(1));
  const { c, lost, calls } = client(store, () => json(401, { code: 'device_revoked', error: 'signed out' }));
  await expect(c.get('/me')).rejects.toMatchObject({ code: 'device_revoked' });
  expect(calls.some((x) => x.url.endsWith('/auth/refresh'))).toBe(false);
  expect(lost).toEqual(['device_revoked']);
});

test('refresh while offline keeps the tokens', async () => {
  const store = new MemTokens(tokens(1));
  const { c, lost } = client(store, (url) => {
    if (url.endsWith('/auth/refresh')) throw new TypeError('Network request failed');
    return json(401, { code: 'token_expired' });
  });
  await expect(c.get('/me', { retries: 0 })).rejects.toBeInstanceOf(NetworkError);
  expect(store.t?.access_token).toBe('lxa_1');
  expect(lost).toEqual([]);
});

test('GETs retry network errors and 503; writes do not', async () => {
  let n = 0;
  const { c } = client(new MemTokens(tokens(1)), () => {
    n += 1;
    if (n < 3) return json(503, { code: 'unavailable' });
    return json(200, { ok: n });
  });
  await expect(c.get('/sync')).resolves.toEqual({ ok: 3 });
  n = 0;
  const w = client(new MemTokens(tokens(1)), () => {
    n += 1;
    throw new TypeError('offline');
  });
  await expect(w.c.post('/messages', {})).rejects.toBeInstanceOf(NetworkError);
  expect(n).toBe(1);
});

test('surfaces the server error code and message', async () => {
  const { c } = client(new MemTokens(tokens(1)), () => json(409, { code: 'recipient_opted_out', error: 'This contact has replied STOP.' }));
  await expect(c.post('/messages', {})).rejects.toMatchObject({ status: 409, code: 'recipient_opted_out', message: 'This contact has replied STOP.' });
});

test('no tokens: fails fast without a network call', async () => {
  const { c, calls } = client(new MemTokens(null), () => json(200, {}));
  await expect(c.get('/me')).rejects.toMatchObject({ code: 'signed_out' });
  expect(calls).toEqual([]);
});
