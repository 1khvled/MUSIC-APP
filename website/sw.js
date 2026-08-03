const CACHE = 'downloads-shell-v4';
const SHELL = ['./', './index.html', './styles.css', './polish.css', './mobile-fixes.css', './app.js', './config.js', './manifest.webmanifest', './icon.svg'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL))));
self.addEventListener('install', event => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim().then(() => caches.keys()).then(keys => Promise.all(keys.filter(key => ![CACHE, 'downloads-media-v1'].includes(key)).map(key => caches.delete(key))))));
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok || response.type === 'opaque') {
      const copy = response.clone();
      caches.open(CACHE).then(cache => cache.put(event.request, copy)).catch(() => {});
    }
    return response;
  }).catch(async () => {
    const cached = await caches.match(event.request);
    if (cached) return cached;
    if (event.request.mode === 'navigate') return caches.match('./index.html');
    return Response.error();
  }));
});
