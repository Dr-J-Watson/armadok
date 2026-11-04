// Service worker: makes the interface installable and fast to open. It keeps only the built
// files of the interface (named by content, so they never go stale). Nothing from the API is
// ever stored: every answer there may hold private data and must always be fresh and rights-checked.
const CACHE = 'armadok-shell-v1'

self.addEventListener('install', () => self.skipWaiting())

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  const url = new URL(request.url)
  if (request.method !== 'GET' || url.origin !== self.location.origin) return
  // Built assets: the name changes with the content, so a stored copy is always right.
  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const hit = await cache.match(request)
        if (hit) return hit
        const fresh = await fetch(request)
        if (fresh.ok) void cache.put(request, fresh.clone())
        return fresh
      }),
    )
  }
  // Everything else (pages, the API, metrics) goes straight to the network, untouched.
})
