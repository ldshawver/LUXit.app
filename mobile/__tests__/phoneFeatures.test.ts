import type { AutomationRule, CallRouting, LuxNumber } from '../src/api/types';
import { routingSummary } from '../src/screens/routingText';
import { describeRule } from '../src/screens/ruleText';
import { PALETTE_IDS, PrefsStore, outgoingNumber, type PaletteId } from '../src/state/prefs';
import { LUX_PALETTES, buildPalette } from '../src/ui/palettes';

const num = (id: number, over: Partial<LuxNumber> = {}): LuxNumber => ({
  id, phone_number: `+1916555000${id}`, friendly_name: `N${id}`, voice_enabled: true, can_call: true, can_send_sms: true,
  messaging: { status: 'registered', checked_at: null, detail: null }, ...over,
});

describe('caller ID choice', () => {
  test('uses the chosen number when it is assigned and permitted', () => {
    expect(outgoingNumber([num(1), num(2)], 2, 'call')?.id).toBe(2);
  });
  test('falls back to the first permitted number when the choice is gone or not allowed', () => {
    expect(outgoingNumber([num(1), num(2)], 99, 'call')?.id).toBe(1);
    expect(outgoingNumber([num(1), num(2, { can_call: false })], 2, 'call')?.id).toBe(1);
    expect(outgoingNumber([num(1, { can_send_sms: false }), num(2)], 1, 'text')?.id).toBe(2);
    expect(outgoingNumber([num(1, { voice_enabled: false })], 1, 'call')).toBeNull();
    expect(outgoingNumber(undefined, null, 'text')).toBeNull();
  });
});

class Mem {
  data = new Map<string, string>();
  async getItem(k: string) { return this.data.get(k) ?? null; }
  async setItem(k: string, v: string) { this.data.set(k, v); }
  async removeItem(k: string) { this.data.delete(k); }
}

describe('prefs store', () => {
  const api = (palette = 'ocean', fail = false) => {
    const saved: string[] = [];
    return {
      saved,
      preferences: async () => ({ preferences: { palette_id: palette } }),
      savePalette: async (id: PaletteId) => { if (fail) throw new Error('nope'); saved.push(id); },
    };
  };

  test('adopts the account palette and caches it with the caller choice', async () => {
    const store = new Mem();
    const a = api('forest');
    const s = new PrefsStore(store, a);
    await s.syncFromServer();
    s.setCallerNumber(4);
    const again = new PrefsStore(store, a);
    await again.load();
    expect(again.snapshot()).toEqual({ paletteId: 'forest', callerNumberId: 4 });
  });

  test('ignores unknown palettes from the server or a corrupt cache', async () => {
    const store = new Mem();
    store.data.set('lux.prefs.v1', '{not json');
    const s = new PrefsStore(store, api('neon'));
    await s.load();
    await s.syncFromServer();
    expect(s.snapshot().paletteId).toBe('lux');
  });

  test('palette change is saved to the account, and reverted if the server refuses', async () => {
    const ok = api();
    const s = new PrefsStore(new Mem(), ok);
    await s.setPalette('rose');
    expect(ok.saved).toEqual(['rose']);
    const bad = new PrefsStore(new Mem(), api('lux', true));
    await expect(bad.setPalette('sunset')).rejects.toThrow('nope');
    expect(bad.snapshot().paletteId).toBe('lux');
  });

  test('sign-out clears the caller choice and palette', async () => {
    const store = new Mem();
    const s = new PrefsStore(store, api());
    s.setCallerNumber(3);
    await s.clear();
    expect(s.snapshot()).toEqual({ paletteId: 'lux', callerNumberId: null });
    expect(store.data.size).toBe(0);
  });
});

describe('palettes', () => {
  test('all six LUXit palettes exist in both modes', () => {
    expect(PALETTE_IDS).toEqual(['lux', 'ocean', 'forest', 'sunset', 'slate', 'rose']);
    for (const id of PALETTE_IDS) {
      const d = buildPalette(id, 'dark');
      expect(d.accent).toBe(LUX_PALETTES[id].primary);
      expect(d.bg).toBe(LUX_PALETTES[id].background);
      const l = buildPalette(id, 'light');
      expect(l.accent).toBe(LUX_PALETTES[id].primary);
      expect(l.bg).toBe('#FFFFFF'); // light mode keeps text readable on white
    }
  });
  test('same inputs return the same object (stable navigation theme)', () => {
    expect(buildPalette('ocean', 'dark')).toBe(buildPalette('ocean', 'dark'));
  });
});

describe('call routing text', () => {
  const r = (b: CallRouting['business_hours'], a: CallRouting['after_hours'], inHours = true): CallRouting =>
    ({ in_business_hours: inHours, business_hours: b, after_hours: a, now: inHours ? b : a });
  test('says plainly when calls bypass the app', () => {
    const s = routingSummary(r({ destination: 'forward', forward_to: '+14155550123' }, { destination: 'voicemail', forward_to: null }));
    expect(s.bypassesApp).toBe(true);
    expect(s.now).toContain('forwarded to (415) 555-0123');
    expect(s.now).toContain('won’t ring');
    expect(s.detail).toContain('After hours: go to voicemail');
  });
  test('masked destinations are shown as given', () => {
    expect(routingSummary(r({ destination: 'forward', forward_to: '•••0123' }, { destination: 'forward', forward_to: '•••0123' })).now)
      .toContain('•••0123');
  });
  test('ringing the app is not a warning', () => {
    const s = routingSummary(r({ destination: 'app', forward_to: null }, { destination: 'app', forward_to: null }));
    expect(s).toEqual({ now: 'Calls ring this app', detail: null, bypassesApp: false });
    expect(routingSummary(null).bypassesApp).toBe(false);
  });
});

describe('rule text', () => {
  const base: Pick<AutomationRule, 'trigger_type' | 'keywords' | 'action' | 'forward_to' | 'tag_value' | 'response'> =
    { trigger_type: 'keyword_contains', keywords: ['urgent'], action: 'forward', forward_to: '+14155550100', tag_value: null, response: '' };
  test('describes forward, reply and system rules', () => {
    expect(describeRule(base)).toBe('Message contains “urgent” → forward to (415) 555-0100');
    expect(describeRule({ ...base, trigger_type: 'after_hours', keywords: [], action: 'reply', response: 'Closed' })).toBe('After business hours → reply “Closed”');
    expect(describeRule({ ...base, trigger_type: 'stop_keyword', keywords: [], action: 'opt_out' })).toBe('STOP keywords → opt out');
  });
});
