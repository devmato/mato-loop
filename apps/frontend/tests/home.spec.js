// @vitest-environment jsdom
import { createApp, h, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar, QBadge, QBanner, QBtn, QCard, QCardActions, QCardSection, QDialog, QForm, QIcon, QInput, QItem, QItemLabel, QItemSection, QLayout, QList, QMenu, QPageContainer, QSeparator, QSkeleton, QSpinnerGrid, QTooltip, ClosePopup, Ripple } from 'quasar'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
const mocks = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('src/boot/axios', () => ({ api: { get: mocks.get } }))
import HomeContent from '../src/features/home/HomeContent.vue'
import ComputerChoiceStrip from '../src/features/chat/components/ComputerChoiceStrip.vue'
import { useExecutionAgentStore } from '../src/features/computers/store'
import { useProjectStore } from '../src/features/projects/store'
import { useAutomationStore } from '../src/features/automations/store'
import { useApprovalStore } from '../src/features/approvals/store'
import { useQuestionStore } from '../src/features/questions/store'
import { useInAppNotificationStore } from '../src/stores/in-app-notifications'
import { useUserProfileStore } from '../src/features/personalization/store'
import { useHomeStore } from '../src/features/home/store'
import { useChatStore } from '../src/features/chat/store'

let app, root
afterEach(() => { app?.unmount(); root?.remove(); vi.resetAllMocks() })

it('reloads after a change received during the initial Home request', async () => {
  setActivePinia(createPinia())
  let finishLoad
  mocks.get.mockReturnValueOnce(new Promise((resolve) => { finishLoad = resolve }))
    .mockResolvedValue({ data: { items: [{ id: 'new' }] } })
  const home = useHomeStore()
  const loading = home.load()
  const refreshing = home.load()
  finishLoad({ data: { items: [] } })
  await Promise.all([loading, refreshing])
  expect(home.sessions).toEqual([{ id: 'new' }])
})

it('shows effective status, opens a session and lets failed or empty data be refreshed', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const projects = [{ id: 'project-1', name: 'Mein Projekt' }]
  const automations = [{ id: 'automation-1', name: 'Bericht', enabled: true, scheduleCount: 1, effectiveState: { enabled: false, nextRunAt: null } }]
  useProjectStore().$patch({ items: projects, loaded: true })
  useAutomationStore().$patch({ items: automations, loaded: true })
  useApprovalStore().$patch({ pending: [{ id: 'approval-1' }], loaded: true })
  useQuestionStore().$patch({ loaded: true })
  useInAppNotificationStore().$patch({ loaded: true, unreadCount: 2 })
  const computers = [{ id: 'computer-1', alias: 'Notebook', connected: true, homeDirectory: '/home/user' }, { id: 'computer-2', alias: 'Server', connected: false }]
  useExecutionAgentStore().$patch({ loaded: true, items: computers })
  let sessions = [{ id: 'session-1', title: 'Ein Gespräch fortsetzen', projectId: 'project-1', createdAt: '2026-10-03T12:00:00Z', hasUnreadMessages: true, activeRunStatus: 'running' }]
  let failed = false
  mocks.get.mockImplementation(async (path) => {
    if (path === '/sessions' && failed) throw new Error('unavailable')
    return { data: {
      '/sessions': { items: sessions }, '/projects': { items: projects },
      '/automations': { items: automations }, '/execution-agents': { items: computers },
      '/approvals/pending': [{ id: 'approval-1' }], '/questions/pending': [],
      '/notifications': { items: [], unreadCount: 2 },
      '/user-profile': { preferredName: { privateOnly: 'Ada', publicOnly: 'A.' }, fullName: { both: 'Ada Lovelace' } }
    }[path] }
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/home', component: HomeContent },
    ...['chat', 'settings-execution-agents', 'settings-automation-create', 'settings-automation-detail'].map((name) => ({ path: `/${name}${name === 'settings-automation-detail' ? '/:automationId' : ''}`, name, component: { render: () => null } }))
  ] })
  await router.push('/home')
  root = document.createElement('div'); document.body.append(root)
  app = createApp({ render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(RouterView))) })
  app.provide('confirmDiscardedEdit', async () => true)
  app.use(pinia).use(router).use(Quasar, { components: { QBadge, QBanner, QBtn, QCard, QCardActions, QCardSection, QDialog, QForm, QIcon, QInput, QItem, QItemLabel, QItemSection, QList, QMenu, QSeparator, QSkeleton, QSpinnerGrid, QTooltip }, directives: { ClosePopup, Ripple } })
  app.mount(root)

  await vi.waitFor(() => expect(root.textContent).toContain('Ein Gespräch fortsetzen'))
  expect(root.querySelector('h1').textContent).toBe('Willkommen zurück, Ada')
  useUserProfileStore().assignProfile({ fullName: { both: 'Ada Lovelace' } })
  await vi.waitFor(() => expect(root.querySelector('h1').textContent).toBe('Willkommen zurück, Ada Lovelace'))
  useUserProfileStore().assignProfile({})
  await vi.waitFor(() => expect(root.querySelector('h1').textContent).toBe('Willkommen zurück'))
  expect(mocks.get).toHaveBeenCalledWith('/sessions', { params: { pageSize: 6, sort: 'recent' } })
  expect(root.textContent).not.toContain('Neue Antwort')
  expect(root.querySelector('.home-activity-row [role="status"]').getAttribute('aria-label')).toBe('Agent arbeitet')
  app.config.globalProperties.$q.screen.gt.xs = false
  app.config.globalProperties.$q.screen.lt.sm = true
  await nextTick()
  expect(root.querySelector('#home-sessions').textContent).toBe('Zuletzt aktiv')
  expect(root.querySelector('.home-activity-row').classList.contains('q-item--dense')).toBe(true)
  app.config.globalProperties.$q.screen.gt.xs = true
  app.config.globalProperties.$q.screen.lt.sm = false
  await nextTick()
  expect(root.querySelector('#home-automations')).toBeNull()
  useAutomationStore().items[0].effectiveState = { enabled: true, nextRunAt: '2026-10-04T12:00:00Z' }
  await vi.waitFor(() => expect(root.textContent).toContain('Nächste Automationen'))
  const menus = []
  for (const title of ['Ein Gespräch fortsetzen', 'Bericht']) {
    const button = root.querySelector(`button[aria-label="Aktionen für ${title}"]`)
    for (const type of ['mousedown', 'mouseup', 'click']) button.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }))
    await vi.waitFor(() => expect(document.querySelector('.q-menu')).not.toBeNull())
    menus.push({ insideLink: Boolean(button.closest('a')), items: [...document.querySelectorAll('.q-menu .q-item__label')].map((item) => item.textContent) })
    button.click()
    await vi.waitFor(() => expect(document.querySelector('.q-menu')).toBeNull())
    for (const type of ['keydown', 'keyup']) button.dispatchEvent(new KeyboardEvent(type, { key: 'Enter', keyCode: 13, bubbles: true, cancelable: true }))
    await vi.waitFor(() => expect(document.querySelector('.q-menu')).not.toBeNull())
    button.click()
    await vi.waitFor(() => expect(document.querySelector('.q-menu')).toBeNull())
  }
  expect({ menus, path: router.currentRoute.value.path }).toEqual({
    menus: [
      { insideLink: false, items: ['Anheften', 'Umbenennen', 'Verschieben', 'Archivieren', 'Löschen'] },
      { insideLink: false, items: ['Automation bearbeiten', 'Automation löschen'] }
    ],
    path: '/home'
  })
  root.querySelector('.home-activity-row').click()
  await vi.waitFor(() => expect(router.currentRoute.value.query).toEqual({ session: 'session-1', project: 'project-1' }))
  await router.push('/home')
  await vi.waitFor(() => expect(root.textContent).toContain('Ein Gespräch fortsetzen'))
  failed = true
  await useHomeStore().load().catch(() => {})
  await vi.waitFor(() => expect(root.textContent).toContain('Sessions konnten nicht geladen werden.'))
  failed = false
  sessions = []
  root.querySelector('button').click()
  await vi.waitFor(() => expect(root.textContent).toContain('Deine letzten Gespräche erscheinen hier.'))
})

it('selects, switches and clears a computer while keeping offline computers last', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  useExecutionAgentStore().$patch({ loaded: true, items: [
    { id: 'offline', alias: 'Server', connected: false },
    { id: 'one', alias: 'Notebook', connected: true, homeDirectory: '/home/user' },
    { id: 'two', alias: 'Desktop', connected: true, homeDirectory: '/work' }
  ] })
  const chat = useChatStore()
  root = document.createElement('div'); document.body.append(root)
  app = createApp({ render: () => h(ComputerChoiceStrip, { session: chat.activeContext, onUpdate: chat.updateActive }) })
  app.use(pinia).use(Quasar, { components: { QBtn, QIcon } })
  app.mount(root)
  await nextTick()
  expect([...root.querySelectorAll('button')].map(button => button.textContent)).toEqual([
    'computerNotebook', 'computerDesktop', 'desktop_access_disabledServer'
  ])
  const selections = []
  for (const name of ['Notebook', 'Desktop', 'Desktop']) {
    root.querySelector(`[aria-label^="${name}:"]`).click()
    await nextTick()
    selections.push({
      id: chat.activeContext.preferredExecutionAgentId,
      directory: chat.activeContext.currentDirectory,
      active: root.querySelectorAll('button[aria-pressed="true"]').length
    })
  }
  expect(selections).toEqual([
    { id: 'one', directory: '/home/user', active: 1 },
    { id: 'two', directory: '/work', active: 1 },
    { id: null, directory: null, active: 0 }
  ])
})
