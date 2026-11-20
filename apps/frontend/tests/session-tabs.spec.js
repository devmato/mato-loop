// @vitest-environment jsdom
import { createApp, h, nextTick, reactive } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar } from 'quasar'
import { quasarOptions } from './quasar-options'
import { useSessionTabsStore } from '../src/features/chat/session-tabs-store'
import { useSessionTabs } from '../src/features/chat/useSessionTabs'
import { useUserPreferencesStore } from '../src/features/settings/user-preferences-store'
import { useChatStore } from '../src/features/chat/store'
import { useProjectStore } from '../src/features/projects/store'
import { useQuestionStore } from '../src/stores/questions'
import SessionTabs from '../src/features/chat/components/SessionTabs.vue'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn() } }))
import { api } from 'src/boot/axios'

let app, root
const a = { id: 'a', title: 'Alpha', projectId: null }
const b = { id: 'b', title: 'Beta', projectId: 'project' }
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks() })

async function mount({ restoring = false } = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore()
  const tabs = useSessionTabsStore()
  const settings = useUserPreferencesStore()
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'chat', component: { render: () => null } }] })
  await router.push({ name: 'chat', query: { session: 'a' } })
  chat.current = a
  let navigation
  const route = reactive({ query: { session: 'a' }, fullPath: '/?session=a', meta: { restoreSessionTabs: restoring } })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ setup() {
    navigation = useSessionTabs({ chat, route })
    return () => h(SessionTabs, { enabled: navigation.enabled.value, tabs, activeKey: navigation.activeKey.value })
  } })
  app.use(pinia).use(router).use(Quasar, quasarOptions)
  app.mount(root)
  return { chat, tabs, settings, router, route, navigation }
}

it('reuses one preview and keeps edited sessions without losing their drafts when a tab closes', async () => {
  const { chat, tabs, settings, router, route } = await mount()
  expect(root.textContent).toBe('')
  settings.sessionTabsEnabled = true
  await nextTick()
  expect(tabs.items.map(tab => [tab.key, tab.preview])).toEqual([['a', true]])
  chat.current = b
  route.query = { session: 'b', project: 'project' }
  await router.push({ name: 'chat', query: route.query })
  await nextTick()
  expect(tabs.items.map(tab => tab.key)).toEqual(['b'])
  chat.setDraft('Keep this draft')
  expect(tabs.items[0].preview).toBe(false)
  chat.current = a
  route.query = { session: 'a' }
  await router.push({ name: 'chat', query: route.query })
  await nextTick()
  root.querySelector('[aria-label="Tab Beta schließen"]').click()
  await nextTick()
  expect([tabs.items.map(tab => tab.key), chat.drafts.sessionMessages.b]).toEqual([['a'], 'Keep this draft'])
})

it('keeps a preview by double click and avoids duplicates through explicit open', async () => {
  const { tabs, settings, navigation } = await mount()
  settings.sessionTabsEnabled = true
  await nextTick()
  root.querySelector('[role="tab"]').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
  await navigation.openKeptSession(a)
  expect(tabs.items.map(tab => [tab.key, tab.preview])).toEqual([['a', false]])
})

it('retains the active tab when navigation is cancelled', async () => {
  const { tabs, settings, router } = await mount()
  settings.sessionTabsEnabled = true
  await nextTick()
  router.beforeEach(() => false)
  root.querySelector('[aria-label="Tab Alpha schließen"]').click()
  await vi.waitFor(() => expect(tabs.items.map(tab => tab.key)).toEqual(['a']))
})

it('makes room for a newly opened tab by dropping the least recently visited tab, not its draft', async () => {
  const { tabs, chat, settings } = await mount()
  settings.sessionTabsLimit = 2
  chat.setDraft('Keep this draft')
  tabs.visit(a, { keep: true })
  tabs.visit(b, { keep: true })
  tabs.visit(a)
  tabs.visit(b)
  tabs.visit({ id: 'c', title: 'Gamma' }, { keep: true })
  expect(tabs.items.map(tab => tab.key)).toEqual(['b', 'c'])
  expect(chat.drafts.sessionMessages.a).toBe('Keep this draft')
})

it('follows sidebar project order while preserving standalone tabs and order within a project', async () => {
  const { tabs, settings } = await mount()
  const projects = useProjectStore()
  projects.items = [
    { id: 'project', name: 'MatoLoop', createdAt: '2026-01-01T00:00:00Z' },
    { id: 'another', name: 'Alpha-Projekt', createdAt: '2026-01-02T00:00:00Z' }
  ]
  settings.sessionTabsEnabled = true
  tabs.visit(b, { keep: true })
  tabs.visit({ id: 'c', title: 'Zweiter Tab', projectId: 'another' }, { keep: true })
  tabs.visit({ id: 'd', title: 'Erster im Alphabet', projectId: 'another' }, { keep: true })
  await nextTick()
  const labels = () => [...root.querySelectorAll('[role="tab"]')].map(tab => tab.getAttribute('aria-label'))
  const before = labels()
  projects.items.reverse()
  await nextTick()
  expect([before, labels()]).toEqual([
    ['Alpha, Vorschau', 'Beta · MatoLoop', 'Zweiter Tab · Alpha-Projekt', 'Erster im Alphabet · Alpha-Projekt'],
    ['Alpha, Vorschau', 'Zweiter Tab · Alpha-Projekt', 'Erster im Alphabet · Alpha-Projekt', 'Beta · MatoLoop']
  ])
  expect([...root.querySelectorAll('.session-tabs__project-badge')].map(icon => icon.style.getPropertyValue('--project-icon-light')))
    .toEqual(['#0e667e', '#0e667e', '#1d4ed8'])
})

it('orders tabs by when they were added, not by manual order or latest use', async () => {
  const { tabs, settings } = await mount()
  settings.sessionTabsEnabled = true
  tabs.visit(b, { keep: true })
  tabs.visit({ id: 'c', title: 'Gamma', projectId: 'project' }, { keep: true })
  tabs.reorder('c', 'b')
  tabs.visit(b)
  settings.sessionTabsOrder = 'added'
  await nextTick()
  const projectTabs = [...root.querySelectorAll('[role="tab"]')].filter(tab => /^(Beta|Gamma)/.test(tab.getAttribute('aria-label')))
  expect(projectTabs.map(tab => [tab.getAttribute('aria-label'), tab.draggable])).toEqual([['Beta', false], ['Gamma', false]])
})

it('limits tabs to ten by default and applies a lower limit only on the next new tab', async () => {
  const { tabs, settings } = await mount()
  for (let index = 0; index < 11; index++) tabs.visit({ id: String(index) }, { keep: true })
  expect(tabs.items.map(tab => tab.key)).toEqual(Array.from({ length: 10 }, (_, index) => String(index + 1)))
  settings.sessionTabsLimit = 2
  tabs.visit({ id: '1' })
  expect(tabs.items).toHaveLength(10)
  tabs.visit({ id: '11' })
  expect(tabs.items.map(tab => tab.key)).toEqual(['1', '11'])
})

it('shows active-run status from summaries instead of session details', async () => {
  const { chat, settings } = await mount()
  chat.current = { ...a, activeRun: { status: 'running' } }
  chat.ensureSessionList().items = [{ ...a, activeRunStatus: 'running' }]
  settings.sessionTabsEnabled = true
  await nextTick()
  expect(root.querySelector('.q-tab--active .q-spinner')).not.toBeNull()
  chat.ensureSessionList().items = [a]
  await nextTick()
  expect(root.querySelector('.q-tab--active .session-tabs__status')).toBeNull()
})

it('shows pending questions even before a restored session summary is loaded', async () => {
  const { tabs, settings } = await mount()
  useProjectStore().items = [{ id: 'project', name: 'MatoLoop' }]
  useQuestionStore().pending = [{ id: 'question', sessionId: 'b', status: 'pending' }]
  tabs.visit(b, { keep: true })
  settings.sessionTabsEnabled = true
  await nextTick()
  const badge = root.querySelector('[role="tab"][aria-label="Beta · MatoLoop"] .session-tabs__project-badge')
  expect([badge?.querySelector('[aria-hidden="true"]')?.textContent, badge?.querySelector('[role="status"]')?.getAttribute('aria-label')])
    .toEqual(['ML', '1 offene Anfrage'])
})

it('loads summaries for restored project sessions without loading sidebar scopes', async () => {
  const { tabs } = await mount()
  tabs.$patch({ owner: 'owner', loaded: true, items: [{ key: 'b', sessionId: 'b', title: 'Beta', projectId: 'project', preview: true }] })
  api.get.mockResolvedValueOnce({ data: { items: [{ ...b, activeRunStatus: 'running' }] } })
  await tabs.refreshSummaries()
  expect(tabs.summaries.b.activeRunStatus).toBe('running')
})

it('preserves targeted updates while an in-flight refresh loads other tab summaries', async () => {
  const { tabs } = await mount()
  tabs.$patch({ owner: 'owner', loaded: true, items: [{ key: 'a', sessionId: 'a' }, { key: 'b', sessionId: 'b' }] })
  let resolve
  api.get.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const refreshing = tabs.refreshSummaries()
  const newA = { ...a, title: 'Updated Alpha' }
  tabs.applySummaryUpdates(['a'], { a: newA })
  resolve({ data: { items: [a, b] } })
  await refreshing
  expect(tabs.summaries).toEqual({ a: newA, b })
})

it('removes project draft tabs before saving the remaining tabs', async () => {
  const { tabs, chat } = await mount()
  tabs.visit({ projectId: 'deleted-project' }, { keep: true })
  tabs.visit(a, { keep: true })
  await chat.removeProjectScope('deleted-project')
  expect(tabs.items.map(tab => tab.key)).toEqual(['a'])
})

it('accepts server-pruned tab references after saving', async () => {
  const { tabs } = await mount()
  tabs.$patch({ owner: 'owner', loaded: true })
  api.put.mockResolvedValueOnce({ data: { sessionTabs: [], controlCenterOpen: false } })
  tabs.visit({ id: 'deleted', title: 'Deleted elsewhere' }, { keep: true })
  await tabs.saving
  expect(tabs.items).toEqual([])
})

it('restores the ordered preview list on another device and persists removal', async () => {
  const { tabs } = await mount()
  let saved = { sessionTabs: [
    { sessionId: 'b', projectId: 'project', title: 'Beta', preview: true },
    { sessionId: 'a', projectId: null, title: 'Alpha', preview: false }
  ], controlCenterOpen: true }
  api.get.mockImplementation(async () => ({ data: JSON.parse(JSON.stringify(saved)) }))
  api.put.mockImplementation(async (_url, patch) => ({ data: (saved = { ...saved, ...patch }) }))
  await tabs.setOwner('owner')
  tabs.close('a')
  await tabs.saving
  const otherDevice = useSessionTabsStore(createPinia())
  await otherDevice.setOwner('owner')
  expect([otherDevice.items.map(tab => [tab.key, tab.preview]), otherDevice.controlCenterOpen])
    .toEqual([[['b', true]], true])
})

it('keeps a remotely closed tab closed when a stale client switches tabs', async () => {
  const first = useSessionTabsStore(createPinia())
  const second = useSessionTabsStore(createPinia())
  let saved = { sessionTabs: [a, b].map(({ id, title, projectId }) => ({ sessionId: id, title, projectId, preview: false })), controlCenterOpen: false }
  api.get.mockImplementation(async () => ({ data: saved }))
  api.put.mockImplementation(async (_url, { sessionTabUsage, ...patch }) => {
    saved = { ...saved, ...patch }
    for (const usage of sessionTabUsage || []) {
      saved.sessionTabs = saved.sessionTabs.map(tab => tab.sessionId === usage.sessionId ? { ...tab, lastUsedAt: usage.lastUsedAt } : tab)
    }
    return { data: saved }
  })
  await first.setOwner('owner')
  await second.setOwner('owner')
  first.close('b')
  await first.saving
  second.visit(a)
  await second.saving
  expect(saved.sessionTabs).toEqual([{ sessionId: a.id, title: a.title, projectId: null, preview: false, addedAt: expect.any(Number), lastUsedAt: expect.any(Number) }])
})

it('reorders without navigating and keeps the moved preview', async () => {
  const { tabs, settings, router } = await mount()
  settings.sessionTabsEnabled = true
  tabs.visit({ ...b, projectId: null }, { keep: true })
  await nextTick()
  const preview = root.querySelector('[aria-label="Alpha, Vorschau"]')
  preview.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', altKey: true, bubbles: true }))
  expect([tabs.items.map(tab => [tab.key, tab.preview]), router.currentRoute.value.query.session])
    .toEqual([[['a', false], ['b', false]], 'a'])
})

it('does not accept a previous account response after the owner changes', async () => {
  const { tabs } = await mount()
  let resolve
  api.get.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const loading = tabs.setOwner('old')
  await tabs.setOwner(null)
  resolve({ data: { sessionTabs: [{ sessionId: 'old-session', preview: false }], controlCenterOpen: true } })
  await loading
  expect([tabs.items, tabs.controlCenterOpen]).toEqual([[], false])
})

it('retains the restored preview when the old route finishes loading later', async () => {
  const { tabs, settings, chat } = await mount({ restoring: true })
  chat.loading = true
  settings.sessionTabsEnabled = true
  api.get.mockResolvedValueOnce({ data: { sessionTabs: [
    { sessionId: 'b', projectId: 'project', title: 'Beta', preview: true }
  ], controlCenterOpen: false } })
  api.put.mockResolvedValue({ data: { sessionTabs: [], controlCenterOpen: false } })
  await tabs.setOwner('owner')
  await nextTick()
  chat.loading = false
  await nextTick()
  expect(tabs.items.map(tab => [tab.key, tab.preview])).toEqual([['b', true], ['a', false]])
  await tabs.saving
})

it('preserves a full restored list until the user opens another session', async () => {
  const { tabs, settings, chat, route } = await mount({ restoring: true })
  chat.loading = true
  settings.sessionTabsEnabled = true
  settings.sessionTabsLimit = 1
  const saved = { sessionTabs: [{ sessionId: 'b', projectId: 'project', title: 'Beta', preview: false }], controlCenterOpen: false }
  api.get.mockResolvedValueOnce({ data: saved })
  api.put.mockImplementation(async (_url, patch) => ({ data: { ...saved, ...patch } }))
  await tabs.setOwner('owner')
  chat.loading = false
  await nextTick()
  chat.setDraft('Editing the restored chat must not evict a tab')
  await nextTick()
  const afterRestore = tabs.items.map(tab => tab.key)
  chat.current = { id: 'c', title: 'Gamma' }
  route.query = { session: 'c' }
  await nextTick()
  await tabs.saving
  expect([afterRestore, tabs.items.map(tab => tab.key)]).toEqual([['b'], ['c']])
})

it('waits for restoration before persisting an explicit visibility change', async () => {
  const { tabs } = await mount()
  let resolve
  api.get.mockReturnValueOnce(new Promise(done => { resolve = done }))
  api.put.mockResolvedValue({ data: { sessionTabs: [], controlCenterOpen: true } })
  const loading = tabs.setOwner('owner')
  const opening = tabs.setControlCenterOpen(true)
  resolve({ data: { sessionTabs: [], controlCenterOpen: false } })
  await Promise.all([loading, opening])
  expect(api.put).toHaveBeenLastCalledWith('/users/me/workspace-state', { controlCenterOpen: true })
})
