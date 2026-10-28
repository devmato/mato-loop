import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { api } = vi.hoisted(() => ({
  api: { delete: vi.fn(), get: vi.fn(), post: vi.fn(), put: vi.fn() }
}))

vi.mock('src/boot/axios', () => ({ api }))

import { useInAppNotificationStore } from '../src/stores/in-app-notifications'

describe('in-app notification store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  afterEach(() => {
    delete global.window
  })

  it('reports focused app presence without transient error toasts', async () => {
    api.put.mockResolvedValue({ data: null })
    const store = useInAppNotificationStore()

    await store.markPresent()

    expect(api.put).toHaveBeenCalledWith('/notifications/presence', null, {
      suppressErrorToast: true
    })
  })

  it('loads the latest notifications and unread count', async () => {
    api.get.mockResolvedValue({
      data: {
        items: [{ id: 'notification-1', readAt: null }],
        nextCursor: 'next',
        unreadCount: 3
      }
    })
    const store = useInAppNotificationStore()

    await store.fetchLatest({ suppressErrorToast: true })

    expect(api.get).toHaveBeenCalledWith('/notifications', {
      timeout: 30_000,
      suppressErrorToast: true,
      params: { pageSize: 20 }
    })
    expect(store.items).toEqual([{ id: 'notification-1', readAt: null }])
    expect(store.nextCursor).toBe('next')
    expect(store.unreadCount).toBe(3)
    expect(store.loaded).toBe(true)
  })

  it('removes a notification only after successful deletion', async () => {
    const store = useInAppNotificationStore()
    store.items = [{ id: 'notice', readAt: null }]
    store.unreadCount = 1
    api.delete.mockRejectedValueOnce(new Error('network'))
    await expect(store.remove('notice')).rejects.toThrow('network')
    expect(store.items).toHaveLength(1)
    api.delete.mockResolvedValueOnce({ data: { unreadCount: 0 } })
    await store.remove('notice')
    expect([store.items, store.unreadCount]).toEqual([[], 0])
  })

  it('does not let an older fetch overwrite newer state', async () => {
    let resolveFirst
    api.get
      .mockReturnValueOnce(new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce({ data: { items: [{ id: 'new' }], unreadCount: 1 } })
    const store = useInAppNotificationStore()
    const first = store.fetchLatest()

    const second = store.fetchLatest()
    resolveFirst({ data: { items: [{ id: 'old' }], unreadCount: 9 } })
    await Promise.all([first, second])

    expect(store.items).toEqual([{ id: 'new' }])
    expect(store.unreadCount).toBe(1)
  })

  it('appends pages without duplicate items', async () => {
    api.get.mockResolvedValue({
      data: {
        items: [{ id: 'notification-1' }, { id: 'notification-2' }],
        nextCursor: null,
        unreadCount: 2
      }
    })
    const store = useInAppNotificationStore()
    store.items = [{ id: 'notification-1' }]
    store.nextCursor = 'next'

    await store.fetchMore()

    expect(api.get).toHaveBeenCalledWith('/notifications', {
      params: { pageSize: 20, cursor: 'next' }
    })
    expect(store.items).toEqual([
      { id: 'notification-1' },
      { id: 'notification-2' }
    ])
  })

  it('ignores a page response made stale by a read mutation', async () => {
    let resolvePage
    api.get.mockReturnValue(new Promise((resolve) => { resolvePage = resolve }))
    api.post.mockResolvedValue({
      data: {
        notification: { id: 'notification-1', readAt: '2026-08-14T10:00:00Z' },
        unreadCount: 0
      }
    })
    const store = useInAppNotificationStore()
    store.items = [{ id: 'notification-1', readAt: null }]
    store.nextCursor = 'next'
    store.unreadCount = 1
    const page = store.fetchMore()

    await store.markRead('notification-1')
    resolvePage({
      data: {
        items: [{ id: 'notification-2', readAt: null }],
        nextCursor: null,
        unreadCount: 2
      }
    })
    await page

    expect(store.items).toEqual([
      { id: 'notification-1', readAt: '2026-08-14T10:00:00Z' }
    ])
    expect(store.unreadCount).toBe(0)
  })

  it('marks one notification only after the request succeeds', async () => {
    const updated = { id: 'notification-1', readAt: '2026-08-14T10:00:00Z' }
    api.post.mockResolvedValue({ data: { notification: updated, unreadCount: 1 } })
    const store = useInAppNotificationStore()
    store.items = [{ id: 'notification-1', readAt: null }]
    store.unreadCount = 2

    await store.markRead('notification-1')

    expect(api.post).toHaveBeenCalledWith('/notifications/notification-1/read')
    expect(store.items).toEqual([updated])
    expect(store.unreadCount).toBe(1)
  })

  it.each(['session.response', 'approval.requested', 'question.requested'])('removes an acknowledged %s notice when the server returns no record', async (kind) => {
    const store = useInAppNotificationStore()
    store.items = [{ id: 'response', kind, readAt: null }]
    api.post.mockResolvedValue({ data: { notification: null, unreadCount: 0 } })
    await store.markRead('response')
    expect(store.items).toEqual([])
  })

  it('keeps unread state when marking one notification fails', async () => {
    api.post.mockRejectedValue(new Error('request failed'))
    const store = useInAppNotificationStore()
    store.items = [{ id: 'notification-1', readAt: null }]
    store.unreadCount = 1

    await expect(store.markRead('notification-1')).rejects.toThrow('request failed')

    expect(store.items[0].readAt).toBeNull()
    expect(store.unreadCount).toBe(1)
  })

  it('serializes read mutations', async () => {
    let resolveRead
    api.post.mockReturnValue(new Promise((resolve) => { resolveRead = resolve }))
    const store = useInAppNotificationStore()
    store.items = [
      { id: 'notification-1', readAt: null },
      { id: 'notification-2', readAt: null }
    ]
    store.unreadCount = 2
    const first = store.markRead('notification-1')

    await store.markRead('notification-2')
    resolveRead({
      data: {
        notification: { id: 'notification-1', readAt: '2026-08-14T10:00:00Z' },
        unreadCount: 1
      }
    })
    await first

    expect(api.post).toHaveBeenCalledTimes(1)
    expect(store.items[1].readAt).toBeNull()
    expect(store.unreadCount).toBe(1)
  })

  it('reloads after bulk read, removing responses and preserving newer unread arrivals', async () => {
    api.post.mockResolvedValue({ data: { updatedCount: 8, unreadCount: 0 } })
    const remaining = [
      { id: 'notification-2', readAt: '2026-08-13T10:00:00Z' },
      { id: 'new-response', kind: 'session.response', readAt: null }
    ]
    api.get.mockResolvedValue({ data: { items: remaining, unreadCount: 1, nextCursor: null } })
    const store = useInAppNotificationStore()
    store.items = [
      { id: 'notification-1', kind: 'session.response', readAt: null },
      { id: 'notification-2', readAt: '2026-08-13T10:00:00Z' }
    ]
    store.unreadCount = 8

    await store.markAllRead()

    expect(api.post).toHaveBeenCalledWith('/notifications/read-all')
    expect([store.items, store.unreadCount]).toEqual([remaining, 1])
  })

  it('reconciles an existing granted push subscription without prompting', async () => {
    const values = new Map()
    const subscription = { toJSON: () => ({ endpoint: 'https://push.example/subscription' }) }
    const requestPermission = vi.fn()
    global.window = {
      isSecureContext: true,
      Notification: { permission: 'granted', requestPermission },
      PushManager: function PushManager() {},
      crypto: { randomUUID: () => '3e6e1d85-4c5e-4fb9-8b8b-77542d0e24ad' },
      localStorage: {
        getItem: (key) => values.get(key) ?? null,
        setItem: (key, value) => values.set(key, value)
      },
      navigator: {
        serviceWorker: {
          getRegistration: vi.fn().mockResolvedValue({
            active: {},
            pushManager: { getSubscription: vi.fn().mockResolvedValue(subscription) }
          }),
          ready: Promise.resolve(null)
        }
      }
    }
    api.get.mockResolvedValue({ data: { enabled: true, configured: true, publicKey: 'AQID' } })
    api.put.mockResolvedValue({ data: null })
    const store = useInAppNotificationStore()

    await store.reconcilePush({ suppressErrorToast: true })

    expect(requestPermission).not.toHaveBeenCalled()
    expect(api.put).toHaveBeenCalledWith(
      '/notifications/push/subscriptions/3e6e1d85-4c5e-4fb9-8b8b-77542d0e24ad',
      { endpoint: 'https://push.example/subscription' },
      { suppressErrorToast: true }
    )
    expect(store.pushSubscribed).toBe(true)
  })

  it('subscribes synchronously from the explicit enable action', async () => {
    const subscription = { toJSON: () => ({ endpoint: 'https://push.example/new' }) }
    let userActivation = true
    const subscribe = vi.fn(() => {
      expect(userActivation).toBe(true)
      window.Notification.permission = 'granted'
      return Promise.resolve(subscription)
    })
    const registration = {
      active: {},
      pushManager: { getSubscription: vi.fn().mockResolvedValue(null), subscribe }
    }
    global.window = {
      isSecureContext: true,
      Notification: {
        permission: 'default',
        requestPermission: vi.fn().mockResolvedValue('granted')
      },
      PushManager: function PushManager() {},
      crypto: { randomUUID: () => '3e6e1d85-4c5e-4fb9-8b8b-77542d0e24ad' },
      localStorage: { getItem: () => null, setItem: vi.fn() },
      navigator: {
        serviceWorker: {
          ready: Promise.resolve(registration),
          getRegistration: vi.fn().mockResolvedValue(registration)
        }
      }
    }
    api.get.mockResolvedValue({ data: { enabled: true, configured: true, publicKey: 'AQID' } })
    api.put.mockResolvedValue({ data: null })
    const store = useInAppNotificationStore()
    await store.reconcilePush()

    const enabling = store.enablePush()
    userActivation = false
    await enabling

    expect(window.Notification.requestPermission).not.toHaveBeenCalled()
    expect(subscribe).toHaveBeenCalledWith({
      userVisibleOnly: true,
      applicationServerKey: new Uint8Array([1, 2, 3])
    })
    expect(store.pushSubscribed).toBe(true)
  })

  it('does not wait indefinitely for a missing service worker registration', async () => {
    global.window = {
      isSecureContext: true,
      Notification: { permission: 'default' },
      PushManager: function PushManager() {},
      navigator: {
        serviceWorker: {
          getRegistration: vi.fn().mockResolvedValue(null),
          ready: new Promise(() => {})
        }
      }
    }
    api.get.mockResolvedValue({ data: { enabled: true, configured: true, publicKey: 'AQID' } })
    const store = useInAppNotificationStore()

    await store.reconcilePush()

    expect(store.pushLoaded).toBe(true)
    expect(store.pushRegistrationReady).toBe(false)
  })
})
