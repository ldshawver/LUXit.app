/* LUXit Inbox — Service Worker */
const CACHE = 'luxit-inbox-v1';
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

/* Network-first for API calls, cache-first for static assets */
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request).catch(() => new Response('{"error":"offline"}', {
      headers: {'Content-Type': 'application/json'}
    })));
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
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body:    data.body,
      icon:    '/static/favicon.png',
      badge:   '/static/favicon.png',
      tag:     'luxit-inbox',
      data:    { url: data.url || '/app/inbox' },
      actions: [{ action: 'open', title: 'Open Inbox' }],
    })
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
