import * as Crypto from 'expo-crypto';
import * as SecureStore from 'expo-secure-store';

const KEY = 'lux.device_uid.v1';

/** Random per-install id. Kept in the keychain/keystore, never derived from hardware ids. */
export async function getDeviceUid(): Promise<string> {
  const existing = await SecureStore.getItemAsync(KEY);
  if (existing) return existing;
  const uid = Crypto.randomUUID().replace(/-/g, '');
  await SecureStore.setItemAsync(KEY, uid, { keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY });
  return uid;
}
