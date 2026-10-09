import { ApiError, NetworkError } from '../src/api/http';
import { MAX_AUTO_ATTEMPTS, Outbox, type OutboxApi } from '../src/messaging/outbox';
import { FakeClock, flush } from './helpers/fakes';

class MemStore {
  data = new Map<string, string>();
  async getItem(k: string) { return this.data.get(k) ?? null; }
  async setItem(k: string, v: string) { this.data.set(k, v); }
}

function setup(sendImpl: OutboxApi['sendMessage'], uploadImpl?: OutboxApi['uploadMedia']) {
  const clock = new FakeClock();
  const store = new MemStore();
  let k = 0;
  const sent: Parameters<OutboxApi['sendMessage']>[0][] = [];
  const uploads: string[] = [];
  let online = true;
  const api: OutboxApi = {
    sendMessage: async (p) => { sent.push(p); return sendImpl(p); },
    uploadMedia: async (f) => { uploads.push(f.uri); return uploadImpl ? uploadImpl(f) : { media: { id: uploads.length } }; },
  };
  const mk = () => new Outbox({ api, store, newKey: () => `key-${++k}`, now: clock.now, setTimer: clock.set, clearTimer: clock.clear, random: () => 0.5, isOnline: () => online });
  return { clock, store, sent, uploads, mk, setOnline: (v: boolean) => { online = v; } };
}

const ok = async () => ({ conversation_id: 5, idempotent: false, message: null });

test('every retry reuses the one idempotency key minted at Send', async () => {
  let n = 0;
  const t = setup(async () => {
    n += 1;
    if (n < 3) throw new NetworkError('timeout', true);
    return ok();
  });
  const box = t.mk();
  await box.enqueue({ to: '+14155550000', body: 'On my way' });
  await flush();
  await t.clock.advance(60_000);
  await t.clock.advance(60_000);
  expect(t.sent.map((s) => s.idempotency_key)).toEqual(['key-2', 'key-2', 'key-2']);
  expect(box.list()[0]?.state).toBe('sent');
});

test('app killed mid-send: resumes with the same key after relaunch', async () => {
  const t = setup(async () => new Promise(() => undefined)); // never answers
  const first = t.mk();
  await first.enqueue({ conversationId: 9, body: 'hello' });
  await flush();
  expect(JSON.parse((await t.store.getItem('lux.outbox.v1'))!)[0].state).toBe('sending');
  const t2Sent: string[] = [];
  const relaunched = new Outbox({
    api: { sendMessage: async (p) => { t2Sent.push(p.idempotency_key); return ok(); }, uploadMedia: async () => ({ media: { id: 1 } }) },
    store: t.store, newKey: () => 'fresh', now: t.clock.now, setTimer: t.clock.set, clearTimer: t.clock.clear,
  });
  await relaunched.load();
  await flush();
  expect(t2Sent).toEqual(['key-2']);
});

test('STOP / forbidden are final; nothing is retried', async () => {
  const t = setup(async () => { throw new ApiError(409, 'recipient_opted_out', 'This contact has replied STOP.'); });
  const box = t.mk();
  await box.enqueue({ conversationId: 1, body: 'x' });
  await flush();
  await t.clock.advance(10 * 60_000);
  expect(t.sent.length).toBe(1);
  expect(box.list()[0]).toMatchObject({ state: 'failed', error: 'This contact has replied STOP.' });
});

test('outcome unknown on the server is never auto-resent', async () => {
  const t = setup(async () => { throw new ApiError(409, 'send_in_progress', 'in flight'); });
  const box = t.mk();
  await box.enqueue({ conversationId: 1, body: 'x' });
  await flush();
  await t.clock.advance(10 * 60_000);
  expect(t.sent.length).toBe(1);
  expect(box.list()[0]?.state).toBe('unknown');
});

test('attachments upload once even if the send has to be retried', async () => {
  let n = 0;
  const t = setup(async () => {
    n += 1;
    if (n === 1) throw new ApiError(503, 'unavailable', 'down');
    return ok();
  });
  const box = t.mk();
  await box.enqueue({ conversationId: 1, body: '', attachments: [{ uri: 'file:///a.jpg', name: 'a.jpg', type: 'image/jpeg' }] });
  await flush();
  await t.clock.advance(60_000);
  expect(t.uploads).toEqual(['file:///a.jpg']);
  expect(t.sent.map((s) => s.media_ids)).toEqual([[1], [1]]);
});

test('offline: waits, then sends when the network returns', async () => {
  const t = setup(ok);
  t.setOnline(false);
  const box = t.mk();
  await box.enqueue({ conversationId: 1, body: 'x' });
  await flush();
  expect(t.sent.length).toBe(0);
  t.setOnline(true);
  box.wake();
  await flush();
  expect(t.sent.length).toBe(1);
});

test('gives up automatic retries after a bounded number, manual retry keeps the key', async () => {
  const t = setup(async () => { throw new NetworkError('offline'); });
  const box = t.mk();
  await box.enqueue({ conversationId: 1, body: 'x' });
  for (let i = 0; i < 20; i++) await t.clock.advance(5 * 60_000);
  expect(t.sent.length).toBe(MAX_AUTO_ATTEMPTS);
  expect(box.list()[0]?.state).toBe('failed');
  await box.retry(box.list()[0]!.localId);
  await flush();
  expect(new Set(t.sent.map((s) => s.idempotency_key)).size).toBe(1);
});

test('empty messages are refused', async () => {
  const t = setup(ok);
  await expect(t.mk().enqueue({ conversationId: 1, body: '   ' })).rejects.toThrow();
});
