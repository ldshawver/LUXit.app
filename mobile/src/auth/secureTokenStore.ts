import * as SecureStore from 'expo-secure-store';
import type { TokenStore } from '../api/http';
import type { Tokens } from '../api/types';

const KEY = 'lux.tokens.v1';
// AFTER_FIRST_UNLOCK: a VoIP push can launch the app while the phone is
// locked; the refresh token must still be readable then.
const OPTS: SecureStore.SecureStoreOptions = { keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY };

export class SecureTokenStore implements TokenStore {
  private cache: Tokens | null | undefined;

  async get(): Promise<Tokens | null> {
    if (this.cache !== undefined) return this.cache;
    const raw = await SecureStore.getItemAsync(KEY, OPTS);
    this.cache = raw ? (JSON.parse(raw) as Tokens) : null;
    return this.cache;
  }

  async set(tokens: Tokens): Promise<void> {
    this.cache = tokens;
    await SecureStore.setItemAsync(KEY, JSON.stringify(tokens), OPTS);
  }

  async clear(): Promise<void> {
    this.cache = null;
    await SecureStore.deleteItemAsync(KEY, OPTS);
  }
}
