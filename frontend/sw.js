// RoadWatch.AI is no longer a PWA. This worker exists only to clean up
// installations of the old caching service worker: it removes every cache,
// unregisters itself and reloads open tabs so they fetch fresh code.
self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map(k => caches.delete(k)));
    await self.registration.unregister();
    const clients = await self.clients.matchAll({ type: 'window' });
    clients.forEach(c => c.navigate(c.url));
  })());
});
