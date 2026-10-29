import type { TokenStore } from '../api/http';
import type { Tokens } from '../api/types';

/** A stored sign-in that cannot be read in this time is treated as unreadable. */
export const RESTORE_TIMEOUT_MS = 5000;

export type RestoreResult =
  | { kind: 'tokens'; tokens: Tokens }
  | { kind: 'none' }
  | { kind: 'failed'; error: string };

/**
 * Reads the stored sign-in for boot without ever throwing or hanging, so boot
 * always leaves "booting" (a throw or a hang here used to strand the app on
 * the startup logo). The keychain can refuse, e.g. before the first unlock
 * after a reboot, or hold corrupt data.
 */
export async function restoreStoredTokens(
  store: Pick<TokenStore, 'get'>,
  timeoutMs = RESTORE_TIMEOUT_MS,
): Promise<RestoreResult> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<RestoreResult>((resolve) => {
    timer = setTimeout(() => resolve({ kind: 'failed', error: 'timeout' }), timeoutMs);
  });
  const read = store.get().then(
    (tokens): RestoreResult => (tokens ? { kind: 'tokens', tokens } : { kind: 'none' }),
    (e): RestoreResult => ({ kind: 'failed', error: String(e) }),
  );
  try {
    return await Promise.race([read, timeout]);
  } finally {
    clearTimeout(timer);
  }
}
