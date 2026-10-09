import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';
import type { Observable } from '../lib/emitter';
import { getController } from './controller';

export function useObservable<T>(obs: Observable<T>, get: () => T): T {
  const subscribe = useCallback((cb: () => void) => obs.subscribe(() => cb()), [obs]);
  return useSyncExternalStore(subscribe, get, get);
}

export function useController() {
  return getController();
}

export function useSession() {
  const c = getController();
  return useObservable(c, () => c.snapshot());
}

export function useVoice() {
  const c = getController();
  return useObservable(c.voice, () => c.voice.snapshot());
}

export function useSync() {
  const c = getController();
  return useObservable(c.sync, () => c.sync.snapshot());
}

export function useOutbox() {
  const c = getController();
  return useObservable(c.outbox, () => c.outbox.list());
}

/** Re-renders every second while `active` (call timers). */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}
