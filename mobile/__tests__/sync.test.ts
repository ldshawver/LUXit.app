import { SyncManager, POLL_MS, type SyncApi, type StreamOpener } from '../src/messaging/sync';
import type { Conversation } from '../src/api/types';
import { FakeClock, flush } from './helpers/fakes';

const conv = (id: number, at: string, read = true): Conversation => ({
  id, from_number: `+1415555${String(id).padStart(4, '0')}`, display_name: null, contact_name: null, is_read: read,
  is_opted_out: false, is_archived: false, last_message_at: at, last_message_preview: 'hi', message_count: 1,
});

function setup() {
  const clock = new FakeClock();
  const sinces: (string | null)[] = [];
  let next: Conversation[] = [];
  const api: SyncApi = {
    conversations: async () => ({ conversations: [conv(1, '2026-10-09T10:00:00')], unread_count: 0 }),
    recentCalls: async () => ({ calls: [] }),
    sync: async (since) => {
      sinces.push(since);
      const out = next;
      next = [];
      return { server_time: `T${sinces.length}`, conversations: out, calls: [], badges: null };
    },
  };
  let opens = 0;
  let handlers: Parameters<StreamOpener>[0] | null = null;
  let closes = 0;
  const openStream: StreamOpener = (h) => {
    opens += 1;
    handlers = h;
    return () => { closes += 1; };
  };
  const m = new SyncManager({ api, openStream, now: clock.now, setTimer: clock.set, clearTimer: clock.clear, random: () => 0.5 });
  return { m, clock, sinces, push: (c: Conversation[]) => { next = c; }, h: () => handlers!, opens: () => opens, closes: () => closes };
}

test('full load then incremental sync from the last server time', async () => {
  const t = setup();
  await t.m.start();
  expect(t.m.snapshot().conversations.map((c) => c.id)).toEqual([1]);
  t.push([conv(2, '2026-10-09T11:00:00', false)]);
  await t.m.syncNow();
  expect(t.sinces).toEqual([null, 'T1']);
  expect(t.m.snapshot().conversations.map((c) => c.id)).toEqual([2, 1]);
  expect(t.m.snapshot().unreadMessages).toBe(1);
});

test('live events debounce into one sync', async () => {
  const t = setup();
  await t.m.start();
  t.h().onOpen();
  expect(t.m.snapshot().status).toBe('live');
  t.h().onEvent('new_message');
  t.h().onEvent('message_status');
  t.h().onEvent('heartbeat');
  await t.clock.advance(400);
  expect(t.sinces.length).toBe(2);
});

test('stream error reconnects with backoff; offline closes it; online resyncs', async () => {
  const t = setup();
  await t.m.start();
  t.h().onError();
  expect(t.m.snapshot().status).toBe('polling');
  await t.clock.advance(3000);
  expect(t.opens()).toBe(2);
  t.m.setOnline(false);
  expect(t.m.snapshot().status).toBe('offline');
  expect(t.m.streamOpen).toBe(false);
  t.m.setOnline(true);
  await flush();
  expect(t.m.streamOpen).toBe(true);
  expect(t.sinces.length).toBe(2);
});

test('background closes the stream; foreground reopens and catches up', async () => {
  const t = setup();
  await t.m.start();
  t.m.setForeground(false);
  expect(t.m.streamOpen).toBe(false);
  t.m.setForeground(true);
  await flush();
  expect(t.m.streamOpen).toBe(true);
  expect(t.sinces.length).toBe(2);
});

test('periodic safety-net sync while foregrounded', async () => {
  const t = setup();
  await t.m.start();
  await t.clock.advance(POLL_MS);
  expect(t.sinces.length).toBe(2);
});
