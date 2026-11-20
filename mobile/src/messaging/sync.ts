import { backoffDelay } from '../lib/backoff';
import { Observable } from '../lib/emitter';
import type { CallRecord, Conversation } from '../api/types';

/**
 * Keeps the local view of conversations / calls converged with the server,
 * which stays authoritative:
 *   - full load on start;
 *   - incremental /sync?since=<server time> on resume, on reconnect, on every
 *     live event and every 60 s while foregrounded (a cheap safety net);
 *   - a live event stream while foregrounded, reopened with backoff.
 * Push notifications cover the backgrounded app.
 */

export interface SyncApi {
  conversations(page?: number): Promise<{ conversations: Conversation[]; unread_count: number }>;
  recentCalls(): Promise<{ calls: CallRecord[] }>;
  sync(since: string | null): Promise<{ server_time: string; conversations: Conversation[]; calls: CallRecord[]; badges: Record<string, number> | null }>;
}

export type StreamOpener = (handlers: { onEvent: (type: string) => void; onOpen: () => void; onError: () => void }) => () => void;

export interface SyncSnapshot {
  conversations: Conversation[];
  calls: CallRecord[];
  unreadMessages: number;
  missedCalls: number;
  status: 'idle' | 'loading' | 'live' | 'polling' | 'offline';
  lastSyncedAt: number | null;
  /** Bumps whenever something changed, so an open thread knows to refetch. */
  version: number;
}

interface Deps {
  api: SyncApi;
  openStream: StreamOpener;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (h: unknown) => void;
  random?: () => number;
}

const LIVE_EVENTS = new Set([
  'new_message', 'incoming_sms', 'sms', 'message_status', 'conversation_updated', 'contact_updated',
  'incoming_call', 'missed_call', 'call_updated', 'voicemail', 'new_voicemail',
]);
export const POLL_MS = 60 * 1000;

export class SyncManager extends Observable<SyncSnapshot> {
  private convs = new Map<number, Conversation>();
  private calls = new Map<number, CallRecord>();
  private serverTime: string | null = null;
  private status: SyncSnapshot['status'] = 'idle';
  private lastSyncedAt: number | null = null;
  private version = 0;
  private badges: Record<string, number> | null = null;
  private foreground = true;
  private online = true;
  private closeStream: (() => void) | null = null;
  private streamFailures = 0;
  private streamTimer: unknown = null;
  private pollTimer: unknown = null;
  private debounce: unknown = null;
  private syncing: Promise<void> | null = null;
  private started = false;
  private d: Required<Deps>;

  constructor(deps: Deps) {
    super();
    this.d = {
      now: () => Date.now(),
      setTimer: (fn, ms) => setTimeout(fn, ms),
      clearTimer: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
      random: Math.random,
      ...deps,
    };
  }

  snapshot(): SyncSnapshot {
    const conversations = Array.from(this.convs.values())
      .filter((c) => !c.is_archived)
      .sort((a, b) => (b.last_message_at ?? '').localeCompare(a.last_message_at ?? ''));
    const calls = Array.from(this.calls.values()).sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
    return {
      conversations,
      calls,
      unreadMessages: this.badges?.smsUnread ?? conversations.filter((c) => !c.is_read).length,
      missedCalls: this.badges?.missedCalls ?? calls.filter((c) => c.label === 'Missed' && !c.is_read).length,
      status: this.status,
      lastSyncedAt: this.lastSyncedAt,
      version: this.version,
    };
  }

  async start(): Promise<void> {
    if (this.started) return;
    this.started = true;
    this.status = 'loading';
    this.publish();
    try {
      const [c, r] = await Promise.all([this.d.api.conversations(1), this.d.api.recentCalls()]);
      c.conversations.forEach((x) => this.convs.set(x.id, x));
      r.calls.forEach((x) => this.calls.set(x.id, x));
      this.version += 1;
    } catch {
      // The incremental sync below retries; the UI shows cached/empty state.
    }
    await this.syncNow();
    this.openStream();
    this.schedulePoll();
  }

  stop(): void {
    this.started = false;
    this.closeLive();
    if (this.pollTimer !== null) this.d.clearTimer(this.pollTimer);
    this.pollTimer = null;
    this.convs.clear();
    this.calls.clear();
    this.serverTime = null;
    this.status = 'idle';
    this.publish();
  }

  setForeground(fg: boolean): void {
    if (fg === this.foreground) return;
    this.foreground = fg;
    if (!this.started) return;
    if (fg) {
      void this.syncNow();
      this.openStream();
      this.schedulePoll();
    } else {
      this.closeLive();
    }
  }

  setOnline(online: boolean): void {
    if (online === this.online) return;
    this.online = online;
    if (!this.started) return;
    if (online) {
      this.streamFailures = 0;
      void this.syncNow();
      this.openStream();
    } else {
      this.closeLive();
      this.status = 'offline';
      this.publish();
    }
  }

  /** Locally apply a server object we just received (e.g. the send response). */
  upsertConversation(c: Conversation): void {
    this.convs.set(c.id, c);
    this.version += 1;
    this.publish();
  }

  markConversationRead(id: number): void {
    const c = this.convs.get(id);
    if (c && !c.is_read) {
      this.convs.set(id, { ...c, is_read: true });
      if (this.badges?.smsUnread) this.badges = { ...this.badges, smsUnread: Math.max(0, this.badges.smsUnread - 1) };
      this.publish();
    }
  }

  syncNow(): Promise<void> {
    if (!this.started || !this.online) return Promise.resolve();
    if (this.syncing) return this.syncing;
    this.syncing = (async () => {
      try {
        const res = await this.d.api.sync(this.serverTime);
        let changed = false;
        for (const c of res.conversations) {
          const prev = this.convs.get(c.id);
          if (!prev || prev.last_message_at !== c.last_message_at || prev.is_read !== c.is_read || prev.display_name !== c.display_name) changed = true;
          this.convs.set(c.id, c);
        }
        for (const call of res.calls) {
          if (!this.calls.has(call.id)) changed = true;
          this.calls.set(call.id, call);
        }
        this.badges = res.badges ?? this.badges;
        this.serverTime = res.server_time;
        this.lastSyncedAt = this.d.now();
        if (changed) this.version += 1;
        if (this.status === 'loading' || this.status === 'offline') this.status = this.closeStream ? 'live' : 'polling';
      } catch {
        // Next resume/reconnect/poll retries.
      } finally {
        this.publish();
      }
    })().finally(() => {
      this.syncing = null;
    });
    return this.syncing;
  }

  private openStream(): void {
    if (!this.started || !this.foreground || !this.online || this.closeStream) return;
    if (this.streamTimer !== null) {
      this.d.clearTimer(this.streamTimer);
      this.streamTimer = null;
    }
    this.closeStream = this.d.openStream({
      onOpen: () => {
        this.streamFailures = 0;
        this.status = 'live';
        this.publish();
      },
      onEvent: (type) => {
        if (!LIVE_EVENTS.has(type)) return;
        if (this.debounce !== null) this.d.clearTimer(this.debounce);
        this.debounce = this.d.setTimer(() => {
          this.debounce = null;
          void this.syncNow();
        }, 300);
      },
      onError: () => {
        this.closeLive();
        this.status = this.online ? 'polling' : 'offline';
        this.publish();
        const delay = backoffDelay(this.streamFailures++, { baseMs: 2000, capMs: 60000, random: this.d.random });
        this.streamTimer = this.d.setTimer(() => {
          this.streamTimer = null;
          this.openStream();
        }, delay);
      },
    });
  }

  private closeLive(): void {
    if (this.closeStream) {
      const close = this.closeStream;
      this.closeStream = null;
      close();
    }
    if (this.streamTimer !== null) this.d.clearTimer(this.streamTimer);
    this.streamTimer = null;
  }

  private schedulePoll(): void {
    if (this.pollTimer !== null) this.d.clearTimer(this.pollTimer);
    this.pollTimer = this.d.setTimer(() => {
      this.pollTimer = null;
      if (this.started && this.foreground) {
        void this.syncNow();
        this.schedulePoll();
      }
    }, POLL_MS);
  }

  private publish(): void {
    this.emit(this.snapshot());
  }

  /** Test helper. */
  get streamOpen(): boolean {
    return this.closeStream !== null;
  }
}
