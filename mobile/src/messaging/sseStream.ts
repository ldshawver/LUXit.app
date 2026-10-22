import EventSource from 'react-native-sse';
import type { ApiClient } from '../api/http';
import type { StreamOpener } from './sync';

/**
 * Live events over the existing /api/inbox/stream SSE endpoint (mounted under
 * the bearer-only prefix). The access token is read fresh for every (re)open,
 * so a token refreshed in the meantime is picked up.
 */
export function makeStreamOpener(http: ApiClient): StreamOpener {
  return ({ onEvent, onOpen, onError }) => {
    let es: EventSource<string> | null = null;
    let closed = false;
    void (async () => {
      const token = await http.accessToken();
      if (closed) return;
      if (!token) {
        onError();
        return;
      }
      es = new EventSource<string>(http.mobileUrl('/api/mobile/v1/inbox/stream'), {
        headers: { Authorization: { toString: () => `Bearer ${token}` } },
        pollingInterval: 0, // we own reconnection (with backoff) in SyncManager
        timeoutBeforeConnection: 0,
      });
      es.addEventListener('open', () => onOpen());
      es.addEventListener('message', (ev) => {
        try {
          const data = JSON.parse((ev as { data?: string }).data ?? '{}') as { type?: string; event?: string };
          onEvent(String(data.type ?? data.event ?? 'message'));
        } catch {
          onEvent('message');
        }
      });
      es.addEventListener('error', () => {
        if (!closed) onError();
      });
    })();
    return () => {
      closed = true;
      es?.removeAllEventListeners();
      es?.close();
    };
  };
}
