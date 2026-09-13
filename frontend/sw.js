const CACHE = 'rw-v5';
// Core assets to pre-cache (large webm logos excluded — fetched on demand)
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
  // Don't intercept Supabase API calls or CDN scripts — always go to network
  const url = e.request.url;
  if (url.includes('supabase.co') || url.includes('jsdelivr.net') || url.includes('googleapis.com')) {
    e.respondWith(fetch(e.request));
    return;
  }
  // Cache-first for everything else
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
