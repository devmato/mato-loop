// @vitest-environment jsdom
import { createApp, h, nextTick, reactive } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar, QBanner, QBtn, QCheckbox, QExpansionItem, QForm, QIcon, QInput, QItem, QItemLabel, QItemSection, QList, QMenu, QPopupProxy, QScrollArea, QSelect, QSeparator, QSpinner, QToggle, ClosePopup, Ripple } from 'quasar'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
const mocks = vi.hoisted(() => ({ stores: {}, confirm: vi.fn(async () => true) }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm: mocks.confirm }) }))
vi.mock('stores/automations', () => ({ useAutomationStore: () => mocks.stores.automations }))
vi.mock('src/features/chat/store', () => ({ useChatStore: () => mocks.stores.chat }))
vi.mock('stores/control-center', () => ({ useControlCenterStore: () => mocks.stores.controlCenter }))
vi.mock('stores/providers', () => ({ useProviderStore: () => ({ selectionOptions: () => [{ label: 'Automation model', value: 'model' }], fetchEffective: vi.fn() }) }))
import AutomationApp from '../src/features/automations/AutomationApp.vue'
import { scheduleDraft } from '../src/features/automations/schedules'

let app, root
const definition = () => ({
  id: 'automation', executionSessionId: 'session', revision: 4, name: 'Tagesbericht', task: 'Berichte über den Tag.',
  enabled: true, archivedAt: null, schedules: [], effectiveState: { activeRunStatus: null }
})

async function mount(value = definition(), selected = value ? 'automation' : 'new', query = {}, mobile = false) {
  let saved = value ? (Array.isArray(value) ? value : [value]) : []
  const store = reactive({
    items: saved, sessionItems: saved,
    fetchForSession: vi.fn(async () => { store.sessionItems = saved; return saved }),
    fetchAutomation: vi.fn(async (id) => saved.find((item) => item.id === id)),
    createAutomation: vi.fn(async (payload) => { const item = { ...definition(), ...payload }; saved = [...saved, item]; store.items = saved; store.sessionItems = saved; return item }),
    updateAutomation: vi.fn(async (id, payload) => { const item = { ...saved.find((item) => item.id === id), ...payload, revision: payload.expectedRevision + 1 }; saved = saved.map((value) => value.id === id ? item : value); store.items = saved; store.sessionItems = saved; return item }),
    deleteAutomation: vi.fn(async (id) => { saved = saved.filter((item) => item.id !== id); store.items = saved; store.sessionItems = saved }),
    runNow: vi.fn(async () => ({ id: 'run', status: 'queued' })),
    fetchRun: vi.fn(async () => ({ id: 'run', status: 'completed', trigger: 'manual', messageId: 'message', definition: {} }))
  })
  mocks.stores = {
    automations: store,
    chat: reactive({ current: { id: 'session', title: 'Meine Session' }, running: false }),
    controlCenter: reactive({ sessionId: 'session', open: true, activeAppId: 'automation', activeContentId: 'automation', ensureApp: vi.fn(), setOpen: vi.fn() })
  }
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', name: 'chat', component: AutomationApp }
  ] })
  await router.push({ name: 'chat', query: { session: 'session', ...(selected ? { automation: selected } : {}), ...query } })
  root = document.createElement('div'); document.body.append(root)
  let page
  app = createApp({ render: () => h(RouterView, null, { default: ({ Component }) => h(Component, { ref: (value) => { if (value) page = value.$.setupState } }) }) })
  app.use(router).use(Quasar, { components: { QBanner, QBtn, QCheckbox, QExpansionItem, QForm, QIcon, QInput, QItem, QItemLabel, QItemSection, QList, QMenu, QPopupProxy, QScrollArea, QSelect, QSeparator, QSpinner, QToggle }, directives: { ClosePopup, Ripple } })
  app.config.globalProperties.$q.notify = vi.fn()
  app.config.globalProperties.$q.screen.lt.md = mobile
  const restoreSplit = vi.fn()
  app.provide('controlCenterLayout', { restoreSplit })
  app.mount(root)
  await vi.waitFor(() => expect(page?.savedDraft).toBeTruthy())
  return { page, store, router, restoreSplit }
}

afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); mocks.confirm.mockClear() })

it('defers hidden overview reads until opening the app in the latest session', async () => {
  const { store } = await mount(null, null)
  store.fetchForSession.mockClear()
  const center = mocks.stores.controlCenter
  center.open = false
  center.sessionId = 'intermediate'
  await nextTick()
  center.sessionId = 'other'
  await nextTick()
  center.open = true
  await vi.waitFor(() => expect(store.fetchForSession.mock.calls).toEqual([['other']]))
})

it('retains an edited automation when hiding and reopening the app', async () => {
  const { page, store } = await mount()
  page.form.task = 'Unsaved edit'
  mocks.stores.controlCenter.open = false
  await nextTick()
  mocks.stores.controlCenter.open = true
  await nextTick()
  expect([page.form.task, store.fetchForSession.mock.calls.length]).toEqual(['Unsaved edit', 1])
})

it('creates an automation in the current session without runtime copies or output configuration', async () => {
  const { page, store } = await mount(null)
  page.form.name = 'Meine Session'
  page.form.task = 'Prüfe die Aufgabe.'
  await page.save()
  expect(store.createAutomation).toHaveBeenCalledWith({
    name: 'Meine Session', task: 'Prüfe die Aufgabe.', sessionId: 'session',
    enabled: true, schedules: [], triggers: [], modelId: null
  })
})

it('validates schedules and preserves existing one-time timestamps', async () => {
  const { page, store } = await mount()
  page.addSchedule()
  page.form.schedules[0].timezone = 'Invalid/Zone'
  await page.save()
  expect(store.updateAutomation).not.toHaveBeenCalled()
  page.form.schedules[0].timezone = 'UTC'
  page.form.schedules.push(scheduleDraft({ runAt: '2027-02-03T11:12:43.123456+01:00', enabled: false }))
  await page.save()
  expect(store.updateAutomation).toHaveBeenCalledWith('automation', expect.objectContaining({
    expectedRevision: 4, schedules: [
      { cron: '0 8 * * *', timezone: 'UTC', validUntil: null, enabled: true },
      { runAt: '2027-02-03T11:12:43.123456+01:00', enabled: false }
    ]
  }))
})

it('saves and clears the run-only model override and preserves it when pausing', async () => {
  const { page, store } = await mount({ ...definition(), modelId: 'model' })
  await page.toggleEnabled()
  expect(store.updateAutomation).toHaveBeenLastCalledWith('automation', expect.objectContaining({ modelId: 'model' }))
  page.form.modelId = null
  await page.save()
  expect(store.updateAutomation).toHaveBeenLastCalledWith('automation', expect.objectContaining({ modelId: null }))
  page.form.modelId = 'model'
  await page.save()
  expect(store.updateAutomation).toHaveBeenLastCalledWith('automation', expect.objectContaining({ modelId: 'model' }))
})

it('saves an event trigger through the normal automation form', async () => {
  const { page, store } = await mount()
  root.querySelector('.q-checkbox').click()
  await nextTick()
  await page.save()
  expect(store.updateAutomation).toHaveBeenCalledWith('automation', expect.objectContaining({ triggers: ['agent_response'] }))
})

it('preserves dirty inputs on status updates and refuses an unsaved manual start', async () => {
  const { page, store } = await mount()
  page.form.task = 'Entwurf'
  store.items = [{ ...definition(), revision: 5, effectiveState: { activeRunStatus: 'running' } }]
  await page.runNow()
  await page.save()
  expect(page.form.task).toBe('Entwurf')
  expect(store.runNow).not.toHaveBeenCalled()
  expect(store.updateAutomation).not.toHaveBeenCalled()
})

it('keeps opening schedule options separate from editing the saved task', async () => {
  const value = { ...definition(), schedules: [{ cron: '0 8 * * *', timezone: 'UTC', enabled: true }] }
  const { page } = await mount(value)
  page.form.schedules[0].advanced = true
  expect(page.dirty).toBe(false)
})

it('restores the pause state when saving fails', async () => {
  const { page, store } = await mount()
  store.updateAutomation.mockRejectedValueOnce(new Error('failure'))
  await page.toggleEnabled()
  expect(page.form.enabled).toBe(true)
})

it('lists multiple automations and keeps the other one when removing an entry', async () => {
  const values = [definition(), { ...definition(), id: 'second', name: 'Wochenbericht' }]
  const { page, store } = await mount(values, null)
  expect(root.querySelector('form')).toBeNull()
  expect(root.textContent).toContain('Tagesbericht')
  expect(root.textContent).toContain('Wochenbericht')
  await page.remove(values[0])
  expect(store.sessionItems.map((item) => item.id)).toEqual(['second'])
})

it('keeps schedules collapsed and opens only the selected one', async () => {
  const { page } = await mount({ ...definition(), schedules: [
    { cron: '0 18 26 8 *', timezone: 'UTC', enabled: false },
    { cron: '15 17 26 8 *', timezone: 'UTC', enabled: false }
  ] })
  expect(root.textContent).toContain('Jährlich am 26. August um 18:00 Uhr')
  expect(root.textContent).not.toContain('Cron')
  expect(page.expandedSchedule).toBeNull()
  page.expandedSchedule = 1
  await nextTick()
  expect([...root.querySelectorAll('.q-expansion-item--expanded')].filter((item) => item.textContent.includes('Wann?'))).toHaveLength(1)
})

it('keeps an edited automation open when discarding changes is declined', async () => {
  const { page, router } = await mount([definition(), { ...definition(), id: 'second' }])
  page.form.task = 'Entwurf'
  mocks.confirm.mockResolvedValueOnce(false)
  await page.select('second')
  expect(router.currentRoute.value.query.automation).toBe('automation')
})

it('removes the automation without navigating away from its session', async () => {
  const { page, store, router } = await mount()
  await page.remove()
  expect(store.deleteAutomation).toHaveBeenCalledWith('automation', 4)
  expect(router.currentRoute.value.query.session).toBe('session')
})

it('leaves an automation reopened before its pending deletion completes', async () => {
  const { page, store, router } = await mount()
  const remove = store.deleteAutomation.getMockImplementation()
  let finish
  store.deleteAutomation.mockImplementationOnce(async (id) => {
    await new Promise((resolve) => { finish = resolve })
    await remove(id)
  })
  const deleting = store.deleteAutomation('automation', 4)
  await page.select(null)
  await page.select('automation')
  await vi.waitFor(() => expect(page.definition?.id).toBe('automation'))
  finish()
  await deleting
  await vi.waitFor(() => expect(router.currentRoute.value.query).toEqual({ session: 'session' }))
})

it.each([['completed', false], ['failed', true]])('opens the last %s run at its original session message (mobile=%s)', async (status, mobile) => {
  const { router, restoreSplit } = await mount({ ...definition(), lastRun: {
    id: 'run', status, startedAt: '2026-10-06T14:32:00Z', messageId: 'message'
  } }, 'automation', {}, mobile)
  const reveal = mobile ? mocks.stores.controlCenter.setOpen : restoreSplit
  const unused = mobile ? restoreSplit : mocks.stores.controlCenter.setOpen
  const button = [...root.querySelectorAll('button')].find((item) => item.textContent.includes('Letzter Lauf:'))
  expect(button.textContent).toContain(status === 'failed' ? 'Fehlgeschlagen' : 'Abgeschlossen')
  expect(root.textContent).not.toContain('Bisherige Ausführungen')
  button.click()
  await vi.waitFor(() => expect(router.currentRoute.value.query).toEqual({ session: 'session', message: 'message' }))
  await vi.waitFor(() => expect(reveal).toHaveBeenCalledExactlyOnceWith(...(mobile ? [false] : [])))
  await vi.waitFor(() => expect([...root.querySelectorAll('button')].some((item) => item.textContent.includes('Letzter Lauf:'))).toBe(true))
  const repeatedButton = [...root.querySelectorAll('button')].find((item) => item.textContent.includes('Letzter Lauf:'))
  repeatedButton.click()
  await vi.waitFor(() => expect(reveal).toHaveBeenCalledTimes(2))
  expect(unused).not.toHaveBeenCalled()
})

it('resolves an old session-bound run link to its message', async () => {
  const { router } = await mount(definition(), 'automation', { run: 'run' })
  await vi.waitFor(() => expect(router.currentRoute.value.query).toEqual({ session: 'session', message: 'message' }))
})
