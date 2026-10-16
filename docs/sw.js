// ORACLE app service worker: makes the home-screen app open instantly and work offline.
// Fresh-first for the page and its data (so each morning's update shows up as soon as you're online),
// cache-first for everything else (icons, libraries). Bump VERSION when this file's logic changes.
const VERSION = 'oracle-v1';
const SHELL = [
  './',
  'index.html',
  'data.js',
  'manifest.webmanifest',
  'icon-192.png',
  'icon-512.png',
  'apple-touch-icon.png',
  'https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function fresh(req) {
  // Ask the network first (revalidating past the browser cache); fall back to the last saved copy offline.
  return fetch(req, { cache: 'no-cache' })
    .then((res) => {
      if (res && res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
      return res;
    })
    .catch(() => caches.match(req, { ignoreSearch: true }).then((hit) => hit || caches.match('index.html')));
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const isPage = req.mode === 'navigate';
  const isData = url.origin === self.location.origin && url.pathname.endsWith('/data.js');
  if (isPage || isData) { event.respondWith(fresh(req)); return; }
  event.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res && (res.ok || res.type === 'opaque')) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
      return res;
    }))
  );
});
