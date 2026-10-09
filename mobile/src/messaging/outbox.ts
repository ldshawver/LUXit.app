import { backoffDelay } from '../lib/backoff';
import { Observable } from '../lib/emitter';
import { ApiError, NetworkError } from '../api/http';
import type { Message } from '../api/types';

/**
 * Durable, idempotent outbound text/MMS queue.
 *
 * The idempotency key is minted ONCE when the user taps Send and persisted
 * with the item, so every retry (network drop, app killed mid-send, timeout
 * after the server already sent) presents the same key and the server sends
 * at most once. Attachments are uploaded once; their server ids are persisted
 * so a retry never re-uploads.
 *
 * States: queued -> uploading -> sending -> sent
 *                                        -> failed   (server refused: STOP, not registered, forbidden)
 *                                        -> unknown  (server says a send for this key is in flight
 *                                                     or its outcome is unknown; never auto-resent)
 * Network / 5xx errors go back to queued with backoff.
 */

export type OutboxState = 'queued' | 'uploading' | 'sending' | 'sent' | 'failed' | 'unknown';

export interface OutboxAttachment {
  uri: string;
  name: string;
  type: string;
  mediaId?: number;
}

export interface OutboxItem {
  localId: string;
  idempotencyKey: string;
  conversationId?: number;
  to?: string;
  body: string;
  attachments: OutboxAttachment[];
  state: OutboxState;
  attempts: number;
  nextAttemptAt: number;
  error?: string;
  createdAt: number;
  serverConversationId?: number;
  serverMessage?: Message | null;
}

export interface KeyValueStore {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
}

export interface OutboxApi {
  uploadMedia(file: { uri: string; name: string; type: string }): Promise<{ media: { id: number } }>;
  sendMessage(payload: { conversation_id?: number; to?: string; body: string; media_ids: number[]; idempotency_key: string }):
    Promise<{ conversation_id: number; idempotent: boolean; message: Message | null }>;
}

interface Deps {
  api: OutboxApi;
  store: KeyValueStore;
  newKey: () => string;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (h: unknown) => void;
  random?: () => number;
  isOnline?: () => boolean;
}

const STORAGE_KEY = 'lux.outbox.v1';
export const MAX_AUTO_ATTEMPTS = 8;
const KEEP_SENT_MS = 60 * 1000;

export class Outbox extends Observable<OutboxItem[]> {
  private items: OutboxItem[] = [];
  private loaded = false;
  private running = false;
  private timer: unknown = null;
  private deps: Required<Omit<Deps, 'isOnline'>> & { isOnline: () => boolean };

  constructor(deps: Deps) {
    super();
    this.deps = {
      now: () => Date.now(),
      setTimer: (fn, ms) => setTimeout(fn, ms),
      clearTimer: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
      random: Math.random,
      isOnline: () => true,
      ...deps,
    } as typeof this.deps;
  }

  list(): OutboxItem[] {
    return this.items;
  }

  forConversation(conversationId: number): OutboxItem[] {
    return this.items.filter((i) => (i.serverConversationId ?? i.conversationId) === conversationId && i.state !== 'sent');
  }

  async load(): Promise<void> {
    if (this.loaded) return;
    const raw = await this.deps.store.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as OutboxItem[]) : [];
    // Anything that was mid-flight when the app died is retried with its key.
    this.items = parsed.map((i) => (i.state === 'sending' || i.state === 'uploading' ? { ...i, state: 'queued', nextAttemptAt: 0 } : i));
    this.loaded = true;
    this.publish();
    void this.kick();
  }

  async enqueue(input: { conversationId?: number; to?: string; body: string; attachments?: OutboxAttachment[] }): Promise<OutboxItem> {
    await this.load();
    const body = input.body.trim();
    if (!body && !(input.attachments?.length)) throw new Error('Nothing to send.');
    const item: OutboxItem = {
      localId: this.deps.newKey(),
      idempotencyKey: this.deps.newKey(),
      conversationId: input.conversationId,
      to: input.to,
      body,
      attachments: (input.attachments ?? []).map((a) => ({ ...a })),
      state: 'queued',
      attempts: 0,
      nextAttemptAt: 0,
      createdAt: this.deps.now(),
    };
    this.items = [...this.items, item];
    await this.persist();
    void this.kick();
    return item;
  }

  /** User tapped "Retry" on a failed/unknown item. Same key: still at most one real send. */
  async retry(localId: string): Promise<void> {
    this.update(localId, { state: 'queued', attempts: 0, nextAttemptAt: 0, error: undefined });
    await this.persist();
    void this.kick();
  }

  async discard(localId: string): Promise<void> {
    this.items = this.items.filter((i) => i.localId !== localId);
    await this.persist();
  }

  /** Network came back / app resumed. */
  wake(): void {
    for (const i of this.items) if (i.state === 'queued') i.nextAttemptAt = 0;
    void this.kick();
  }

  async kick(): Promise<void> {
    if (this.running) return;
    this.running = true;
    try {
      for (;;) {
        if (!this.deps.isOnline()) break;
        const now = this.deps.now();
        const next = this.items.find((i) => i.state === 'queued' && i.nextAttemptAt <= now);
        if (!next) break;
        await this.process(next.localId);
      }
    } finally {
      this.running = false;
    }
    this.prune();
    this.scheduleNext();
  }

  private scheduleNext(): void {
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = null;
    const waiting = this.items.filter((i) => i.state === 'queued');
    if (!waiting.length) return;
    const soonest = Math.min(...waiting.map((i) => i.nextAttemptAt));
    const ms = Math.max(250, soonest - this.deps.now());
    this.timer = this.deps.setTimer(() => {
      this.timer = null;
      void this.kick();
    }, ms);
  }

  private async process(localId: string): Promise<void> {
    let item = this.get(localId)!;
    try {
      if (item.attachments.some((a) => a.mediaId === undefined)) {
        this.update(localId, { state: 'uploading' });
        for (const att of item.attachments) {
          if (att.mediaId !== undefined) continue;
          const res = await this.deps.api.uploadMedia({ uri: att.uri, name: att.name, type: att.type });
          att.mediaId = res.media.id;
          await this.persist();
        }
      }
      item = this.get(localId)!;
      this.update(localId, { state: 'sending' });
      await this.persist();
      const res = await this.deps.api.sendMessage({
        conversation_id: item.conversationId,
        to: item.conversationId ? undefined : item.to,
        body: item.body,
        media_ids: item.attachments.map((a) => a.mediaId!),
        idempotency_key: item.idempotencyKey,
      });
      this.update(localId, { state: 'sent', serverConversationId: res.conversation_id, serverMessage: res.message, error: undefined });
    } catch (e) {
      this.handleError(localId, e);
    }
    await this.persist();
  }

  private handleError(localId: string, e: unknown): void {
    const item = this.get(localId)!;
    if (e instanceof ApiError) {
      if (e.code === 'send_in_progress') {
        this.update(localId, { state: 'unknown', error: 'Not sure this was sent. Check the conversation before sending again.' });
        return;
      }
      if (e.code === 'invalid_media') {
        // An upload expired server-side; re-upload on the next attempt.
        for (const a of item.attachments) a.mediaId = undefined;
      } else if (e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429) {
        this.update(localId, { state: 'failed', error: e.message });
        return;
      }
    } else if (!(e instanceof NetworkError)) {
      this.update(localId, { state: 'failed', error: String((e as Error)?.message ?? e) });
      return;
    }
    const attempts = item.attempts + 1;
    if (attempts >= MAX_AUTO_ATTEMPTS) {
      this.update(localId, { state: 'failed', attempts, error: 'Couldn’t send. Tap to retry.' });
      return;
    }
    this.update(localId, {
      state: 'queued', attempts,
      nextAttemptAt: this.deps.now() + backoffDelay(attempts - 1, { baseMs: 2000, capMs: 2 * 60 * 1000, random: this.deps.random }),
      error: e instanceof NetworkError ? 'Waiting for connection…' : 'Retrying…',
    });
  }

  private prune(): void {
    const cutoff = this.deps.now() - KEEP_SENT_MS;
    const before = this.items.length;
    this.items = this.items.filter((i) => i.state !== 'sent' || i.createdAt > cutoff);
    if (this.items.length !== before) void this.persist();
  }

  private get(localId: string): OutboxItem | undefined {
    return this.items.find((i) => i.localId === localId);
  }

  private update(localId: string, patch: Partial<OutboxItem>): void {
    this.items = this.items.map((i) => (i.localId === localId ? { ...i, ...patch } : i));
    this.publish();
  }

  private publish(): void {
    this.emit(this.items);
  }

  private async persist(): Promise<void> {
    await this.deps.store.setItem(STORAGE_KEY, JSON.stringify(this.items));
  }
}
