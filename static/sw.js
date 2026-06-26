/* LUXit Inbox — Service Worker */
const SW_VERSION = new URL(self.location.href).searchParams.get('v') || '20260621-pwa-communications';
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
  e.waitUntil(
    Promise.all([
      updateBadge(),
      self.registration.showNotification(data.title, {
      body:    data.body,
      icon:    data.icon || '/static/favicon.png',
      badge:   data.badge || '/static/favicon.png',
      tag:     data.tag || 'luxit-inbox',
      data:    Object.assign({ url: data.url || '/app/inbox' }, data.data || {}),
      renotify: data.renotify !== false,
      requireInteraction: data.requireInteraction === true,
      vibrate: data.silent === true ? [] : (data.vibrate || [200, 100, 200]),
      silent:  data.silent === true,
      // Android Chrome/PWA uses the app/channel default sound when silent is false.
      sound:   data.sound || 'default',
        actions: [{ action: 'open', title: 'Open Inbox' }],
      })
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
