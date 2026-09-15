const CACHE = 'rw-v6';
// Core assets to pre-cache
const ASSETS = [
  '/', '/index.html', '/app.js', '/admin.html',
  '/logo.png', '/manifest.json',
  '/icons/icon-192.png', '/icons/icon-512.png', '/icons/favicon.png',
  '/assets/frame-demo.jpg', '/assets/frame-raw.jpg', '/assets/frame-clean.jpg',
  '/assets/evidence_t15.5.jpg', '/assets/evidence_t16.0.jpg', '/assets/evidence_t16.5.jpg',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(ASSETS.filter(Boolean)))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = e.request.url;

  // Supabase, Google, CDNs: always direct network
  if (url.includes('supabase.co') || url.includes('jsdelivr.net') || url.includes('googleapis.com')) {
    e.respondWith(fetch(e.request));
    return;
  }

  // HTML and JS scripts: Network-first (so code updates are instant without hard refresh)
  if (e.request.mode === 'navigate' || url.endsWith('.html') || url.endsWith('.js') || url.endsWith('/')) {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const resClone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, resClone));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Cache-first for images, fonts, and static media
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
