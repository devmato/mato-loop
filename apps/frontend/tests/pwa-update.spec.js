import { IDBFactory } from 'fake-indexeddb'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('workbox-precaching', () => ({
  precache: vi.fn(),
  cleanupOutdatedCaches: vi.fn(),
  getCacheKeyForURL: vi.fn(),
  matchPrecache: vi.fn()
}))

const originalNavigator = globalThis.navigator
const originalSessionStorage = globalThis.sessionStorage
const originalWindow = globalThis.window
let updates

beforeEach(async () => {
  vi.resetModules()
  vi.stubEnv('MATOLOOP_WEB_REVISION', 'a'.repeat(40))
  const values = new Map()
  vi.stubGlobal('sessionStorage', {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key)
  })
  updates = await import('../src/app/pwa-update')
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: originalNavigator })
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: originalSessionStorage })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: originalWindow })
})

describe('PWA updates', () => {
  function updateEnvironment({ controlled = true, waiting = true } = {}) {
    const reload = vi.fn()
    const worker = Object.assign(new EventTarget(), { postMessage: vi.fn(), state: 'installed' })
    const serviceWorker = Object.assign(new EventTarget(), {
      controller: controlled ? { state: 'activated' } : null,
      getRegistration: vi.fn().mockResolvedValue({ waiting: waiting ? worker : null })
    })
    const window = Object.assign(new EventTarget(), {
      location: { reload }, setTimeout, clearTimeout
    })
    Object.defineProperty(globalThis, 'window', { configurable: true, value: window })
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { serviceWorker } })
    updates.observePwaUpdates()
    return { reload, serviceWorker, worker }
  }

  it('activates the waiting worker and reloads once after takeover', async () => {
    const { reload, serviceWorker, worker } = updateEnvironment()
    const update = updates.requestPwaUpdate()
    await Promise.resolve()

    expect(worker.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' })
    expect(reload).not.toHaveBeenCalled()
    serviceWorker.controller = worker
    serviceWorker.dispatchEvent(new Event('controllerchange'))
    serviceWorker.dispatchEvent(new Event('controllerchange'))

    await expect(update).resolves.toBe(true)
    expect(reload).toHaveBeenCalledOnce()
    expect(updates.isPwaUpdateReload()).toBe(true)
  })

  it('reloads another open tab on takeover', () => {
    const { reload, serviceWorker } = updateEnvironment()
    serviceWorker.controller = { state: 'activated' }
    serviceWorker.dispatchEvent(new Event('controllerchange'))
    expect(reload).toHaveBeenCalledOnce()
  })

  it('does not reload on the first installation', () => {
    const { reload, serviceWorker, worker } = updateEnvironment({ controlled: false })
    serviceWorker.controller = worker
    serviceWorker.dispatchEvent(new Event('controllerchange'))
    expect(reload).not.toHaveBeenCalled()
  })

  it('shows the requested exact changelog once even after a reload of the previous revision', async () => {
    const { serviceWorker, worker } = updateEnvironment()
    const pending = updates.requestPwaUpdate({ showChanges: true, userId: 'owner' })
    await Promise.resolve()
    serviceWorker.controller = worker
    serviceWorker.dispatchEvent(new Event('controllerchange'))
    await pending
    vi.resetModules()
    const previous = await import('../src/app/pwa-update')
    expect(previous.consumePwaChangelog('owner')).toBeNull()
    vi.stubEnv('MATOLOOP_WEB_REVISION', 'b'.repeat(40))
    vi.resetModules()
    const next = await import('../src/app/pwa-update')
    expect([next.consumePwaChangelog('owner'), next.consumePwaChangelog('owner')]).toEqual([
      { base: 'a'.repeat(40), target: 'b'.repeat(40) }, null
    ])
  })

  it.each([
    { showChanges: false, target: 'b', owner: 'owner', waiting: true },
    { showChanges: true, target: 'a', owner: 'owner', waiting: true },
    { showChanges: true, target: 'b', owner: 'other', waiting: true },
    { showChanges: true, target: 'b', owner: 'owner', waiting: false }
  ])('skips unchecked, unchanged, other-account or missing updates: %j', async ({ showChanges, target, owner, waiting }) => {
    const { serviceWorker, worker } = updateEnvironment({ waiting })
    const pending = updates.requestPwaUpdate({ showChanges, userId: 'owner' })
    await Promise.resolve()
    if (waiting) {
      serviceWorker.controller = worker
      serviceWorker.dispatchEvent(new Event('controllerchange'))
    }
    await pending
    vi.stubEnv('MATOLOOP_WEB_REVISION', target.repeat(40))
    vi.resetModules()
    const next = await import('../src/app/pwa-update')
    expect(next.consumePwaChangelog(owner)).toBeNull()
  })

  it('answers the old page window count request on the waiting worker', async () => {
    const matchAll = vi.fn().mockResolvedValue([{}])
    const worker = { __WB_MANIFEST: [], clients: { matchAll }, addEventListener: vi.fn() }
    vi.stubGlobal('self', worker)
    await import('../src-pwa/custom-service-worker')

    const port = { postMessage: vi.fn() }
    const [, onMessage] = worker.addEventListener.mock.calls.find(([type]) => type === 'message')
    let pending
    onMessage({ data: { type: 'WINDOW_COUNT' }, ports: [port], waitUntil: (promise) => { pending = promise } })

    expect(pending).toBeDefined()
    await pending
    expect(port.postMessage).toHaveBeenCalledWith(1)
    expect(matchAll).toHaveBeenCalledWith({ type: 'window', includeUncontrolled: true })
  })

  it('does not reload if no update is waiting', async () => {
    const { reload } = updateEnvironment({ waiting: false })
    await expect(updates.requestPwaUpdate()).resolves.toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })

  it('shows push without a second sound when a tab has already played the notice', async () => {
    vi.stubGlobal('indexedDB', new IDBFactory())
    const { claimNotificationSound } = await import('../src/features/notifications/sound-claim')
    const id = '3e6e1d85-4c5e-4fb9-8b8b-77542d0e24ad'
    const showNotification = vi.fn().mockResolvedValue()
    const worker = { __WB_MANIFEST: [], addEventListener: vi.fn(),
      registration: { showNotification }, clients: { matchAll: vi.fn().mockResolvedValue([]) } }
    vi.stubGlobal('self', worker)
    await import('../src-pwa/custom-service-worker')
    const [, push] = worker.addEventListener.mock.calls.find(([type]) => type === 'push')
    const play = vi.fn(() => true)
    await Promise.all([claimNotificationSound([id], play), claimNotificationSound([id], play)])
    expect(play).toHaveBeenCalledOnce()
    const payload = { version: 1, notification: { title: 'Neue Antwort', notificationId: id } }
    await new Promise((resolve) => push({ data: { json: () => payload }, waitUntil: resolve }))
    expect(showNotification).toHaveBeenCalledExactlyOnceWith('Neue Antwort', expect.objectContaining({ silent: true }))
  })

  it('opens the session from Push without waiting for the read acknowledgement', async () => {
    const id = '3e6e1d85-4c5e-4fb9-8b8b-77542d0e24ad'
    let acknowledge
    const fetch = vi.fn(() => new Promise((resolve) => { acknowledge = resolve }))
    const openWindow = vi.fn().mockResolvedValue()
    const worker = {
      __WB_MANIFEST: [], location: { origin: 'https://mato.test' }, addEventListener: vi.fn(),
      clients: { matchAll: vi.fn().mockResolvedValue([]), openWindow }
    }
    vi.stubGlobal('self', worker)
    vi.stubGlobal('fetch', fetch)
    await import('../src-pwa/custom-service-worker')
    const [, click] = worker.addEventListener.mock.calls.find(([type]) => type === 'notificationclick')
    const pending = []
    click({
      notification: { close: vi.fn(), data: { notificationId: id, target: { type: 'session', sessionId: id } } },
      waitUntil: (promise) => pending.push(promise)
    })
    await vi.waitFor(() => expect(openWindow).toHaveBeenCalledWith(`https://mato.test/?session=${id}`))
    expect(fetch).toHaveBeenCalledWith(`/api/notifications/${id}/read`, { method: 'POST', credentials: 'same-origin' })
    acknowledge({ ok: true })
    await Promise.all(pending)
  })

  it('recovers from a stalled activation and reloads on a late takeover', async () => {
    vi.useFakeTimers()
    const { reload, serviceWorker, worker } = updateEnvironment()
    const update = updates.requestPwaUpdate({ showChanges: true, userId: 'owner' })
    const failure = expect(update).rejects.toThrow('timed out')
    await vi.advanceTimersByTimeAsync(15_000)

    await failure
    expect(reload).not.toHaveBeenCalled()
    serviceWorker.controller = worker
    serviceWorker.dispatchEvent(new Event('controllerchange'))
    expect(reload).toHaveBeenCalledOnce()
    vi.stubEnv('MATOLOOP_WEB_REVISION', 'b'.repeat(40))
    vi.resetModules()
    const next = await import('../src/app/pwa-update')
    expect(next.consumePwaChangelog('owner')).toEqual({ base: 'a'.repeat(40), target: 'b'.repeat(40) })
  })

  it('allows retry after a failed message', async () => {
    const { reload, serviceWorker, worker } = updateEnvironment()
    worker.postMessage.mockImplementationOnce(() => { throw new Error('Worker unavailable') })
    await expect(updates.requestPwaUpdate()).rejects.toThrow('Worker unavailable')

    const retry = updates.requestPwaUpdate()
    await Promise.resolve()
    expect(worker.postMessage).toHaveBeenCalledTimes(2)
    serviceWorker.controller = worker
    serviceWorker.dispatchEvent(new Event('controllerchange'))
    await expect(retry).resolves.toBe(true)
    expect(reload).toHaveBeenCalledOnce()
  })

  it('snoozes an offered update for one hour even when storage is unavailable', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-21T12:00:00Z'))
    const storage = {
      getItem: vi.fn(() => null),
      removeItem: vi.fn(),
      setItem: vi.fn(() => { throw new Error('Storage unavailable') })
    }
    Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage })

    updates.snoozePwaUpdate()
    expect(storage.setItem).toHaveBeenCalledWith(
      updates.PWA_UPDATE_SNOOZE_KEY,
      String(Date.now() + 60 * 60 * 1000)
    )
    expect(updates.pwaUpdateSnoozeRemaining()).toBe(60 * 60 * 1000)
    vi.advanceTimersByTime(60 * 60 * 1000)
    expect(updates.pwaUpdateSnoozeRemaining()).toBe(0)
  })
})
