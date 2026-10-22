/* Boot's stored sign-in read never throws or hangs, so the app always leaves
 * the startup screen (signed in, or on Sign in when the keychain refuses). */
import { RESTORE_TIMEOUT_MS, restoreStoredTokens } from '../src/auth/restoreSession';
import type { Tokens } from '../src/api/types';

const tokens = { access_token: 'a', refresh_token: 'r' } as unknown as Tokens;

describe('restoreStoredTokens', () => {
  test('stored sign-in', async () => {
    await expect(restoreStoredTokens({ get: async () => tokens })).resolves.toEqual({ kind: 'tokens', tokens });
  });

  test('nothing stored', async () => {
    await expect(restoreStoredTokens({ get: async () => null })).resolves.toEqual({ kind: 'none' });
  });

  test('keychain error (e.g. before first unlock, corrupt JSON) -> failed, not thrown', async () => {
    const r = await restoreStoredTokens({ get: async () => { throw new Error('User interaction is not allowed'); } });
    expect(r).toEqual({ kind: 'failed', error: 'Error: User interaction is not allowed' });
  });

  test('a read that never settles -> failed after RESTORE_TIMEOUT_MS', async () => {
    jest.useFakeTimers();
    try {
      const p = restoreStoredTokens({ get: () => new Promise<Tokens | null>(() => undefined) });
      jest.advanceTimersByTime(RESTORE_TIMEOUT_MS - 1);
      let settled = false;
      void p.then(() => { settled = true; });
      await Promise.resolve();
      expect(settled).toBe(false);
      jest.advanceTimersByTime(1);
      await expect(p).resolves.toEqual({ kind: 'failed', error: 'timeout' });
    } finally {
      jest.useRealTimers();
    }
  });

  test('a fast read clears its timeout', async () => {
    jest.useFakeTimers();
    try {
      await restoreStoredTokens({ get: async () => null });
      expect(jest.getTimerCount()).toBe(0);
    } finally {
      jest.useRealTimers();
    }
  });
});
