// @vitest-environment jsdom
import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar, QBanner, QBtn, QExpansionItem, QIcon, QItem, QItemLabel, QItemSection, QLayout, QList, QMenu, QPage, QPageContainer, QSpinner, Ripple, ClosePopup } from 'quasar'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), restore: vi.fn(), refresh: vi.fn() }))
vi.mock('src/boot/axios', () => ({ api: { get: mocks.get, post: mocks.post } }))
vi.mock('src/features/chat/store', () => ({ useChatStore: () => ({ setArchived: mocks.restore, refreshSessions: mocks.refresh }) }))
vi.mock('stores/projects', () => ({ useProjectStore: () => ({ fetchAll: mocks.refresh }) }))
vi.mock('stores/automations', () => ({ useAutomationStore: () => ({ fetchAutomations: mocks.refresh }) }))
import ArchivePage from '../src/features/archive/ArchivePage.vue'

let app, root
afterEach(() => { app?.unmount(); root?.remove(); vi.resetAllMocks() })

it('opens archived conversations and restores a session using its archive action', async () => {
  let items = [{ id: 'session', title: 'Archiviertes Gespräch', archivedAt: '2026-10-02T10:00:00Z' }]
  mocks.get.mockImplementation(async (path) => ({ data: { items: path === '/projects' ? [] : items } }))
  mocks.restore.mockImplementation(async () => { items = [] })
  const remove = vi.fn().mockResolvedValue(false)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', name: 'archive', component: ArchivePage },
    { path: '/chat', name: 'chat', component: { render: () => null } }
  ] })
  await router.push('/')
  root = document.createElement('div'); document.body.append(root)
  app = createApp({ render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(RouterView))) })
  app.provide('deleteActions', { sessions: remove })
  app.use(createPinia()).use(router).use(Quasar, { components: { QBanner, QBtn, QExpansionItem, QIcon, QItem, QItemLabel, QItemSection, QList, QMenu, QPage, QSpinner }, directives: { Ripple, ClosePopup } })
  app.config.globalProperties.$q.notify = vi.fn()
  app.mount(root)
  await vi.waitFor(() => expect(root.textContent).toContain('Archiviertes Gespräch'))
  expect(root.querySelector('.archive-list > .q-item').getAttribute('tabindex')).toBe('0')
  root.querySelector('[aria-label="Aktionen für Archiviertes Gespräch"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-menu')).not.toBeNull())
  Array.from(document.querySelectorAll('.q-menu .q-item')).find(item => item.textContent.endsWith('Löschen')).click()
  await vi.waitFor(() => expect(remove).toHaveBeenCalledWith(items[0], { permanent: false }))
  expect(router.currentRoute.value.name).toBe('archive')
  await vi.waitFor(() => expect(root.querySelector('[aria-label="Session wiederherstellen"]').disabled).toBe(false))
  root.querySelector('[aria-label="Session wiederherstellen"]').click()
  await vi.waitFor(() => expect(root.textContent).toContain('Keine einzeln archivierten Sessions.'))
  expect(mocks.restore).toHaveBeenCalledWith('session', false)
  expect(router.currentRoute.value.name).toBe('archive')
})

it('keeps trash closed to previews, disables expired restore and reuses permanent deletion', async () => {
  const active = { id: 'active', title: 'Gelöscht', projectId: null, deleteAt: '2999-01-01T00:00:00Z' }
  const expired = { id: 'expired', title: 'Abgelaufen', deleteAt: '2000-01-01T00:00:00Z' }
  let items = [active, expired]
  mocks.get.mockImplementation(async () => ({ data: { projects: [], sessions: items } }))
  mocks.post.mockImplementation(async () => { items = [expired]; return { data: { worktreeRestored: false } } })
  const remove = vi.fn().mockImplementation(async () => { items = []; return true })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: ArchivePage, props: { mode: 'trash' } }] })
  await router.push('/')
  root = document.createElement('div'); document.body.append(root)
  app = createApp({ render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(RouterView))) })
  app.provide('deleteActions', { sessions: remove })
  app.use(createPinia()).use(router).use(Quasar, { components: { QBanner, QBtn, QExpansionItem, QIcon, QItem, QItemLabel, QItemSection, QList, QMenu, QPage, QSpinner }, directives: { Ripple, ClosePopup } })
  app.config.globalProperties.$q.notify = vi.fn()
  app.mount(root)
  await vi.waitFor(() => expect(root.textContent).toContain('Abgelaufen'))
  expect(root.querySelector('a, .q-expansion-item')).toBeNull()
  expect(root.querySelectorAll('[aria-label="Session wiederherstellen"]')[1].disabled).toBe(true)
  root.querySelector('[aria-label="Session wiederherstellen"]').click()
  await vi.waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/trash/sessions/active/restore'))
  await vi.waitFor(() => expect(app.config.globalProperties.$q.notify).toHaveBeenCalledWith(expect.objectContaining({ type: 'warning' })))
  await vi.waitFor(() => expect(root.querySelectorAll('[aria-label="Aktionen für Abgelaufen"]')).toHaveLength(1))
  await vi.waitFor(() => expect(root.querySelector('[aria-label="Session wiederherstellen"]').disabled).toBe(true))
  await new Promise(resolve => setTimeout(resolve, 20))
  root.querySelector('[aria-label="Aktionen für Abgelaufen"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-menu')).not.toBeNull())
  Array.from(document.querySelectorAll('.q-menu .q-item')).find(item => item.textContent.includes('Sofort löschen')).click()
  await vi.waitFor(() => expect(root.textContent).toContain('Keine einzeln gelöschten Sessions.'))
  expect(remove).toHaveBeenCalledWith(expired, { permanent: true })
})

it('only offers permanent deletion for independently trashed sessions inside a trashed project', async () => {
  const independent = { id: 'independent', title: 'Einzeln gelöscht', deleteAt: '2000-01-01T00:00:00Z' }
  const remove = vi.fn().mockResolvedValue(false)
  mocks.get.mockImplementation(async (path) => ({ data: path === '/trash'
    ? { projects: [{ id: 'project', name: 'Gelöschtes Projekt', deleteAt: '2999-01-01T00:00:00Z' }], sessions: [] }
    : { items: [{ id: 'child', title: 'Enthaltenes Gespräch', deleteAt: null }, independent] } }))
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: ArchivePage, props: { mode: 'trash' } }] })
  await router.push('/')
  root = document.createElement('div'); document.body.append(root)
  app = createApp({ render: () => h(QLayout, null, () => h(QPageContainer, null, () => h(RouterView))) })
  app.provide('deleteActions', { sessions: remove })
  app.use(createPinia()).use(router).use(Quasar, { components: { QBanner, QBtn, QExpansionItem, QIcon, QItem, QItemLabel, QItemSection, QList, QMenu, QPage, QSpinner }, directives: { Ripple, ClosePopup } })
  app.mount(root)
  await vi.waitFor(() => expect(root.querySelector('.q-expansion-item__container > .q-item')).not.toBeNull())
  root.querySelector('.q-expansion-item__container > .q-item').click()
  await vi.waitFor(() => expect(root.textContent).toContain('Enthaltenes Gespräch'))
  expect([...root.querySelectorAll('.archive-children a, .archive-children button')].map(node => node.getAttribute('aria-label')))
    .toEqual(['Aktionen für Einzeln gelöscht'])
  root.querySelector('[aria-label="Aktionen für Einzeln gelöscht"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-menu .q-item')?.textContent).toContain('Sofort löschen'))
  document.querySelector('.q-menu .q-item').click()
  await vi.waitFor(() => expect(remove).toHaveBeenCalledWith({ ...independent, projectId: 'project' }, { permanent: true }))
})
