// @vitest-environment jsdom
import { createApp, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { Quasar } from 'quasar'
import { quasarOptions } from './quasar-options'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm }) }))

import NotificationCenterApp from '../src/features/notifications/NotificationCenterApp.vue'
import { useInAppNotificationStore } from '../src/stores/in-app-notifications'
import { api } from 'src/boot/axios'

const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }))
const createdAt = '2026-09-06T10:00:00Z'
let app, root, pinia, response

beforeEach(() => {
  vi.resetAllMocks()
  confirm.mockResolvedValue(true)
  pinia = createPinia()
  setActivePinia(pinia)
  response = {
    items: [{ id: 'notice', title: 'Automation abgeschlossen', createdAt, readAt: null }],
    unreadCount: 1, nextCursor: null
  }
  api.get.mockImplementation(async () => ({ data: response }))
  api.post.mockResolvedValue({ data: { unreadCount: 0 } })
})

afterEach(() => {
  app?.unmount()
  root?.remove()
})

async function mountCenter(dark = false) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'chat', component: { render: () => null } },
      { path: '/settings/memory', name: 'settings-memory', component: { render: () => null } }
    ]
  })
  await router.push('/')
  root = document.createElement('div')
  document.body.append(root)
  app = createApp(NotificationCenterApp)
  app.use(pinia).use(router).use(Quasar, { ...quasarOptions, config: { dark } })
  app.config.globalProperties.$q.dark.set(dark)
  app.mount(root)
  await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(1))
  await nextTick()
  await vi.waitFor(() => expect(root.querySelector('[aria-label="Benachrichtigungen aktualisieren"] .q-spinner')).toBeNull())
  return router
}

describe('notification center', () => {
  it('does not offer a manual push pause when web push is enabled', async () => {
    const store = useInAppNotificationStore()
    store.$patch({ pushConfigured: true, pushServerEnabled: true })
    await mountCenter()
    expect(root.textContent).not.toContain('Push 30 Min. pausieren')
  })

  it('toggles the locally stored notification sound independently of push', async () => {
    await mountCenter()
    const button = root.querySelector('button[aria-label="Benachrichtigungston"]')
    expect(button.getAttribute('aria-pressed')).toBe('false')
    button.click()
    await nextTick()
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(useInAppNotificationStore(createPinia()).soundEnabled).toBe(true)
    button.click()
    await nextTick()
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(api.post).not.toHaveBeenCalled()
    window.localStorage.removeItem('matoLoopNotificationSoundEnabled')
  })

  it('deletes a notice directly without confirmation, navigation or marking it read', async () => {
    const router = await mountCenter()
    api.delete.mockResolvedValue({ data: { unreadCount: 0 } })
    root.querySelector('[aria-label="Benachrichtigung löschen"]').click()
    await vi.waitFor(() => expect(useInAppNotificationStore().items).toHaveLength(0))
    expect(confirm).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
    expect(router.currentRoute.value.query).toEqual({})
    useInAppNotificationStore().loading = true
    await nextTick()
    expect([root.textContent.includes('Keine Benachrichtigungen'), root.querySelector('[aria-label="Benachrichtigungen aktualisieren"] .q-spinner')]).toEqual([true, null])
  })

  it('renders every notification as an ordinary notice and acknowledges them together', async () => {
    response = {
      items: ['question.requested', 'approval.requested', 'memory.approval', 'session.response'].map((kind) => ({
        id: kind, kind, title: kind, createdAt, readAt: null
      })), unreadCount: 4, nextCursor: null
    }
    await mountCenter(true)
    const notifications = useInAppNotificationStore()
    const notices = root.querySelector('[aria-label="Benachrichtigungen"]')
    expect([...notices.children].map((item) => item.classList.contains('q-item'))).toEqual([true, true, true, true])
    expect(notices.querySelector('.approval-card, .question-dock, .q-expansion-item')).toBeNull()
    const readAll = root.querySelector('[aria-label="Alle Benachrichtigungen als gelesen markieren"]')
    response = {
      items: response.items.filter((item) => item.kind !== 'session.response')
        .map((item) => ({ ...item, readAt: createdAt })), unreadCount: 0, nextCursor: null
    }
    readAll.click()
    await vi.waitFor(() => expect(notifications.unreadCount).toBe(0))
    await vi.waitFor(() => expect(notifications.items).toHaveLength(3))
    expect(notifications.items.every((item) => item.readAt)).toBe(true)
    expect(api.post).toHaveBeenCalledExactlyOnceWith('/notifications/read-all')
  })

  it.each([
    [{ type: 'session', sessionId: 'parent' }, 'chat', { session: 'parent' }],
    [{ type: 'memoryApproval', approvalId: 'memory' }, 'settings-memory', { tab: 'dreaming', approval: 'memory' }]
  ])('opens the target and marks the notice read (%j)', async (target, name, query) => {
    const notice = response.items[0]
    notice.target = target
    api.post.mockResolvedValue({ data: { notification: { ...notice, readAt: createdAt }, unreadCount: 0 } })
    const router = await mountCenter()
    root.querySelector('[aria-label="Benachrichtigungen"] > .q-item').click()
    await vi.waitFor(() => expect(useInAppNotificationStore().unreadCount).toBe(0))
    expect([router.currentRoute.value.name, router.currentRoute.value.query]).toEqual([name, query])
    expect(api.post).toHaveBeenCalledExactlyOnceWith('/notifications/notice/read')
  })

  it('allows retry when notifications fail to load', async () => {
    api.get.mockRejectedValueOnce(new Error('Unavailable'))
    await mountCenter()
    expect(root.querySelector('.q-banner').textContent).toContain('konnten nicht')
    ;[...root.querySelectorAll('button')].find((item) => item.textContent.includes('Erneut laden')).click()
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
    await nextTick()
    expect(root.querySelector('.q-banner')).toBeNull()
    expect(root.textContent).toContain('Automation abgeschlossen')
  })
})
