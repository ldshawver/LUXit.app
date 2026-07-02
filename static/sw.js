/* LUXit Inbox — Service Worker */
const SW_VERSION = new URL(self.location.href).searchParams.get('v') || '20260702-push-sound-diagnostics';
const CACHE = `luxit-inbox-${SW_VERSION}`;
const APP_SHELL = [
  '/app/inbox',
  '/static/manifest.json',
  '/static/favicon.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'GET_SW_VERSION') {
    e.source && e.source.postMessage({ type: 'SW_VERSION', version: SW_VERSION });
  }
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});

async function storePushDebug(payload, options) {
  const debug = {
    swVersion: SW_VERSION,
    lastPushReceivedAt: new Date().toISOString(),
    lastPushPayload: payload,
    lastEventType: payload.eventType || (payload.data && payload.data.event_type) || null,
    lastNotificationOptions: options,
    lastNotificationSilent: options.silent === true,
    lastNotificationTag: options.tag || '',
    lastNotificationRenotify: options.renotify === true,
    lastNotificationVibrate: options.vibrate || [],
    badgeCount: payload.badgeCount ?? (payload.data && payload.data.badgeCount) ?? null,
  };
  const clientsList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  clientsList.forEach(c => c.postMessage({ type: 'PUSH_DIAGNOSTICS', debug }));
  const cache = await caches.open(`luxit-push-debug-${SW_VERSION}`);
  await cache.put('/__luxit_push_debug__', new Response(JSON.stringify(debug), { headers: { 'Content-Type': 'application/json' } }));
}

/* Network-first for API/app shell calls, cache-first for versioned static assets */
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/app/')) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request).then(cached => cached || new Response('{"error":"offline"}', {
      headers: {'Content-Type': 'application/json'}
    }))));
    return;
  }
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).then(res => {
      if (res.ok && e.request.method === 'GET') {
        caches.open(CACHE).then(c => c.put(e.request, res.clone()));
      }
      return res;
    }))
  );
});

/* Push notification handler */
self.addEventListener('push', e => {
  let data = { title: 'LUXit Inbox', body: 'New message', url: '/app/inbox' };
  try { data = e.data ? e.data.json() : data; } catch {}
  const badgeCount = data.badgeCount ?? (data.data && data.data.badgeCount);
  const updateBadge = () => {
    if (!self.registration.setAppBadge && !self.registration.clearAppBadge) return Promise.resolve();
    if (!Number.isFinite(Number(badgeCount)) || Number(badgeCount) <= 0) {
      return self.registration.clearAppBadge ? self.registration.clearAppBadge() : Promise.resolve();
    }
    return self.registration.setAppBadge ? self.registration.setAppBadge(Number(badgeCount)) : Promise.resolve();
  };
  const notificationOptions = {
    body:    data.body,
    icon:    data.icon || '/static/favicon.png',
    badge:   data.badge || '/static/favicon.png',
    tag:     data.tag || `luxit-${data.eventType || (data.data && data.data.event_type) || 'notification'}`,
    data:    Object.assign({ url: data.url || '/app/inbox' }, data.data || {}),
    renotify: data.renotify !== false,
    requireInteraction: data.requireInteraction === true,
    vibrate: data.silent === true ? [] : (data.vibrate || [200, 100, 200]),
    silent:  data.silent === true,
    // Android Chrome/PWA uses the app/channel default sound when silent is false.
    sound:   data.sound || 'default',
    actions: [{ action: 'open', title: 'Open Inbox' }],
  };
  if (notificationOptions.renotify && !notificationOptions.tag) {
    notificationOptions.tag = `luxit-${Date.now()}`;
  }
  e.waitUntil(
    Promise.all([
      updateBadge(),
      storePushDebug(data, notificationOptions),
      self.registration.showNotification(data.title, notificationOptions)
    ])
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/app/inbox';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes('/app/inbox')) { c.focus(); c.navigate(url); return; }
      }
      return clients.openWindow(url);
    })
  );
});
