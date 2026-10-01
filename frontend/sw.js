// RoadWatch.AI is no longer a PWA. This worker exists only to clean up installations of the
// old caching service worker: it removes every cache, unregisters itself and reloads the open
// tabs so they fetch fresh code. Keep this file deployed: the SPA rewrite would otherwise serve
// index.html as /sw.js, the update check would fail on the MIME type, and the old worker would
// stay installed. Only a 404/410 or a replacement script retires it.
self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    // Deliberately no claim() call: activation after skipWaiting() already switches the pages that
    // used the old worker to this one, and claiming would also seize (and below, reload) healthy
    // tabs of the current non-PWA app.
    const keys = await caches.keys();
    await Promise.all(keys.map(k => caches.delete(k)));
    await self.registration.unregister();              // so the reloaded pages come back uncontrolled
    const clients = await self.clients.matchAll({ type: 'window' });
    await Promise.all(clients.map(c => c.navigate(c.url).catch(() => {})));
  })());
});
