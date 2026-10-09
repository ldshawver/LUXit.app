import { Observable } from '../lib/emitter';
import type { LuxNumber } from '../api/types';

export const PALETTE_IDS = ['lux', 'ocean', 'forest', 'sunset', 'slate', 'rose'] as const;
export type PaletteId = (typeof PALETTE_IDS)[number];

export interface Prefs {
  paletteId: PaletteId;
  /** The user's chosen outgoing number (caller ID / new-text sender). */
  callerNumberId: number | null;
}

export interface KeyValueStore {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

export interface PrefsApi {
  preferences(): Promise<{ preferences?: { palette_id?: string } }>;
  savePalette(paletteId: PaletteId): Promise<unknown>;
}

const STORAGE_KEY = 'lux.prefs.v1';
const DEFAULTS: Prefs = { paletteId: 'lux', callerNumberId: null };

export function isPaletteId(v: unknown): v is PaletteId {
  return typeof v === 'string' && (PALETTE_IDS as readonly string[]).includes(v);
}

/**
 * Outgoing number for calls or texts: the chosen one if it is still assigned
 * and permitted for that use, else the first permitted number. The server
 * re-checks every call and text; this only picks what to ask for.
 */
export function outgoingNumber(numbers: LuxNumber[] | undefined, chosenId: number | null, use: 'call' | 'text'): LuxNumber | null {
  const ok = (n: LuxNumber) => (use === 'call' ? n.can_call && n.voice_enabled : n.can_send_sms);
  const list = (numbers ?? []).filter(ok);
  return list.find((n) => n.id === chosenId) ?? list[0] ?? null;
}

/**
 * Device-cached preferences. The palette is the user's LUXit palette (the
 * same six as the web app, stored on the account); the cache only makes the
 * first frame right before /pwa/preferences answers.
 */
export class PrefsStore extends Observable<Prefs> {
  private state: Prefs = DEFAULTS;

  constructor(private store: KeyValueStore, private api: PrefsApi) {
    super();
  }

  snapshot(): Prefs {
    return this.state;
  }

  private set(patch: Partial<Prefs>): void {
    this.state = { ...this.state, ...patch };
    this.emit(this.state);
    this.store.setItem(STORAGE_KEY, JSON.stringify(this.state)).catch(() => undefined);
  }

  async load(): Promise<void> {
    try {
      const raw = await this.store.getItem(STORAGE_KEY);
      if (!raw) return;
      const v = JSON.parse(raw) as Partial<Prefs>;
      this.state = {
        paletteId: isPaletteId(v.paletteId) ? v.paletteId : 'lux',
        callerNumberId: typeof v.callerNumberId === 'number' ? v.callerNumberId : null,
      };
      this.emit(this.state);
    } catch {
      // A corrupt cache just means defaults.
    }
  }

  /** Adopt the account's palette (signed in / app resumed). */
  async syncFromServer(): Promise<void> {
    try {
      const res = await this.api.preferences();
      const id = res.preferences?.palette_id;
      if (isPaletteId(id) && id !== this.state.paletteId) this.set({ paletteId: id });
    } catch {
      // Keep the cached palette offline.
    }
  }

  /** Optimistic; reverts if the server refuses. */
  async setPalette(id: PaletteId): Promise<void> {
    const before = this.state.paletteId;
    if (id === before) return;
    this.set({ paletteId: id });
    try {
      await this.api.savePalette(id);
    } catch (e) {
      this.set({ paletteId: before });
      throw e;
    }
  }

  setCallerNumber(id: number | null): void {
    this.set({ callerNumberId: id });
  }

  async clear(): Promise<void> {
    this.state = DEFAULTS;
    this.emit(this.state);
    await this.store.removeItem(STORAGE_KEY).catch(() => undefined);
  }
}
