/* eslint-env serviceworker */

import { parsePushPayload, pushTargetPath } from '../src/features/notifications/push'
import { claimNotificationSound } from '../src/features/notifications/sound-claim'
import {
  cleanupOutdatedCaches,
  getCacheKeyForURL,
  matchPrecache,
  precache
} from 'workbox-precaching'

precache(self.__WB_MANIFEST)
cleanupOutdatedCaches()

self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))

self.addEventListener('message', (event) => {
  if (event.data?.type === 'SKIP_WAITING') event.waitUntil(self.skipWaiting())
  // Old cached pages still ask the waiting worker before offering the update.
  if (event.data?.type === 'WINDOW_COUNT' && event.ports[0]) {
    event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((clients) => event.ports[0].postMessage(clients.length)))
  }
})

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return
  const url = new URL(event.request.url)
  if (url.origin !== self.location.origin) return
  if (url.pathname === '/manifest.webmanifest') return

  if (event.request.mode === 'navigate') {
    if (url.pathname === '/api' || url.pathname.startsWith('/api/')
      || url.pathname === '/docs' || url.pathname.startsWith('/docs/')) return
    event.respondWith(matchPrecache('/index.html').then((response) => response || fetch(event.request)))
    return
  }

  if (getCacheKeyForURL(event.request.url)) {
    event.respondWith(matchPrecache(event.request).then((response) => response || fetch(event.request)))
  }
})

async function notifyClients() {
  const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
  for (const client of clients) client.postMessage({ type: 'matoloop:push', version: 1 })
}

async function showPushNotification({ title, options }) {
  const id = options.data.notificationId
  if (id && !options.silent) {
    const claimed = await claimNotificationSound([id]).catch(() => true)
    if (!claimed) options = { ...options, silent: true }
  }
  return self.registration.showNotification(title, options)
}

self.addEventListener('push', (event) => {
  let payload = null
  try {
    payload = parsePushPayload(event.data?.json())
  } catch {
    return
  }
  if (!payload) return

  event.waitUntil(Promise.allSettled([
    showPushNotification(payload),
    notifyClients()
  ]))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const path = pushTargetPath(event.notification.data?.target)
  const destination = new URL(path, self.location.origin)
  const id = event.notification.data?.notificationId
  if (typeof id === 'string' && /^[0-9a-f-]{36}$/i.test(id)) {
    event.waitUntil(fetch(`/api/notifications/${encodeURIComponent(id)}/read`, {
      method: 'POST', credentials: 'same-origin'
    }).catch(() => {}))
  }
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const client = clients.find((item) => new URL(item.url).origin === destination.origin)
    if (client) {
      await client.navigate(destination.href)
      return client.focus()
    }
    return self.clients.openWindow(destination.href)
  })())
})
