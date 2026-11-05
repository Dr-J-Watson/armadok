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

// Notifications pushed by the server. The message was encrypted for this browser alone, so the
// push service that carried it could not read it. It is shown as plain text, never as markup,
// and clicking it opens (or focuses) the interface: nothing is fetched or stored here.
self.addEventListener('push', (event) => {
  let data = {}
  try {
    data = event.data ? event.data.json() : {}
  } catch {
    data = {}
  }
  const title = typeof data.title === 'string' && data.title ? data.title.slice(0, 120) : 'Armadok'
  const body = typeof data.body === 'string' ? data.body.slice(0, 300) : ''
  // One tag per kind of event, and a fresh alert each time: a minor message must never replace
  // a critical one that has not been read yet.
  const kind = typeof data.event === 'string' ? data.event.slice(0, 60) : ''
  event.waitUntil(
    self.registration.showNotification(title, { body, icon: '/icon.svg', tag: `armadok-${kind}`, renotify: true }),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((all) => {
      const open = all.find((c) => new URL(c.url).origin === self.location.origin)
      return open ? open.focus() : self.clients.openWindow('/')
    }),
  )
})
