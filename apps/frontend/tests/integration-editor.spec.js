// @vitest-environment jsdom
import { createApp, h, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Quasar } from 'quasar'
import { quasarOptions } from './quasar-options'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn() } }))
vi.mock('src/features/browser/BrowserSetupDialog.vue', () => ({ default: { render: () => null } }))
const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm }) }))

import Dialog from '../src/features/integrations/IntegrationCreateDialog.vue'
import Editor from '../src/features/integrations/IntegrationEditorPage.vue'
import { useAuthStore } from '../src/stores/auth'
import { useIntegrationStore } from '../src/stores/integrations'
import { api } from 'src/boot/axios'

let app, root, accounts
const account = { id: 'calendar-1', integrationType: 'calendar', providerType: 'caldav', name: 'Kalender', enabled: true, hasCredential: true, config: { baseUrl: 'https://calendar.test', username: 'owner' } }
const adapters = [{ integrationType: 'calendar', adapterType: 'caldav', executionLocation: 'backend', availability: 'full' }]
beforeEach(() => {
  vi.resetAllMocks()
  confirm.mockResolvedValue(true)
  accounts = [{ ...account }]
  api.get.mockImplementation(async (url) => ({ data: url === '/integrations/catalog' ? { adapters } : url.endsWith('/permissions') ? {
    name: account.name, providerType: 'caldav', profiles: [],
    permissions: { privatePermissions: { policies: {}, resourcePolicies: {} }, publicPermissions: { policies: {}, resourcePolicies: {} } },
    inherited: Object.fromEntries(['private', 'public'].map((trust) => [trust, { actions: [{ capability: 'calendar.read', label: 'Kalender lesen', policy: 'ask' }], resources: [] }]))
  } : {
    providers: [], accounts: url === '/integrations/calendar' ? accounts : [], bases: [], instances: []
  } }))
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
})
afterEach(() => {
  app?.unmount()
  root?.remove()
  vi.restoreAllMocks()
})

function mount(render, pinia, router) {
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render }).use(pinia).use(Quasar, quasarOptions)
  if (router) app.use(router)
  app.config.globalProperties.$q.notify = vi.fn()
  app.mount(root)
}

async function mountEditor(scope, create = false, type = 'calendar') {
  const pinia = createPinia()
  useAuthStore(pinia).isAdmin = true
  const prefix = scope === 'instance' ? 'admin' : 'settings'
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/integrations', name: `${prefix}-integrations`, component: { render: () => null } },
    { path: '/integrations/new', name: `${prefix}-integration-create`, component: Editor },
    { path: '/admin/tools', name: 'admin-tools', component: { render: () => null } },
    { path: '/integrations/:integrationId/edit', name: `${prefix}-integration-edit`, component: Editor }
  ] })
  await router.push(create ? { name: `${prefix}-integration-create`, query: { type, name: 'Neu' } } : `/integrations/${account.id}/edit`)
  let page
  mount(() => h(RouterView, null, { default: ({ Component }) => Component && h(Component, { ref: (value) => { page = value?.$?.setupState } }) }), pinia, router)
  await vi.waitUntil(() => root.querySelector('form'))
  return { router, page: () => page }
}

function button(label) {
  return [...document.querySelectorAll('button')].find((item) => item.textContent.includes(label))
}

async function fill(label, value) {
  const field = [...root.querySelectorAll('.q-field')].find((item) => item.querySelector('.q-field__label')?.textContent === label)
  const input = field.querySelector('input')
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}

it('collects only basics without creating a connection', async () => {
  const pinia = createPinia()
  const integrations = useIntegrationStore(pinia)
  integrations.selectScope('personal')
  integrations.catalog = adapters
  const proceed = vi.fn()
  mount(() => h(Dialog, { modelValue: true, scope: 'personal', onContinue: proceed }), pinia)
  await vi.waitUntil(() => document.querySelector('.integration-dialog'))
  button('Weiter').click()
  await vi.waitFor(() => expect(proceed).toHaveBeenCalledWith({ type: 'calendar', name: 'CalDAV' }))
  expect(api.post).not.toHaveBeenCalled()
})

it.each(['instance', 'personal'])('edits and tests the saved %s connection without replacing its password', async (scope) => {
  api.put.mockImplementation(async (_url, payload) => {
    accounts = [{ ...account, name: payload.name }]
    return { data: {} }
  })
  api.post.mockResolvedValue({ data: { status: 'ok' } })
  await mountEditor(scope)
  await fill('Name', 'Umbenannt')
  expect(button('Verbindung testen').disabled).toBe(true)
  button('Speichern').click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith(`/integrations/calendar/accounts/${account.id}`, expect.objectContaining({ name: 'Umbenannt' }), { params: { scope } }))
  expect(api.put.mock.calls[0][1]).not.toHaveProperty('password')
  await vi.waitUntil(() => !button('Verbindung testen').disabled)
  button('Verbindung testen').click()
  await vi.waitFor(() => expect(root.textContent).toContain('Verbindung erfolgreich'))
  expect(api.post).toHaveBeenCalledWith(`/integrations/calendar/accounts/${account.id}/test`, null, { params: { scope } })
})

it('creates a connection only after completing the page and opens its editor', async () => {
  api.post.mockImplementation(async (_url, payload) => {
    accounts.push({ ...account, id: 'new-calendar', name: payload.name })
    return { data: {} }
  })
  const { router } = await mountEditor('personal', true)
  await fill('CalDAV-Basis-URL', 'https://calendar.test')
  await fill('Benutzername', 'owner')
  await fill('Passwort', 'test-password')
  button('Speichern').click()
  await vi.waitFor(() => expect(router.currentRoute.value.params.integrationId).toBe('new-calendar'))
})

it('creates an instance-wide Jina connection without requiring an API key', async () => {
  const originalGet = api.get.getMockImplementation()
  const readers = []
  api.get.mockImplementation(async (url) => url === '/integrations/catalog'
    ? { data: { adapters: [...adapters, { integrationType: 'webfetch', adapterType: 'jina', executionLocation: 'backend', availability: 'instance_only', instanceOnly: true }] } }
    : url === '/integrations/webfetch' ? { data: { providers: readers, integrationId: null } } : originalGet(url))
  api.post.mockImplementation(async (_url, payload) => {
    readers.push({ id: 'reader', name: payload.name, integrationType: 'webfetch', providerType: 'jina', enabled: true, config: { baseUrl: payload.base_url } })
    return { data: {} }
  })
  const { router } = await mountEditor('instance', true, 'webfetch')
  await fill('Jina Reader-Basis-URL', 'https://jina.devmato.net')
  await fill('Standardzeitlimit', '60')
  ;[...root.querySelectorAll('[role="switch"]')].find(item => item.textContent.includes('Overlays standardmäßig entfernen')).click()
  button('Cookie hinzufügen').click()
  await nextTick()
  await fill('Zielhost', 'www.golem.de')
  await fill('Cookie-Name', 'consent')
  await fill('Cookie-Wert', 'test-consent')
  button('Speichern').click()
  await vi.waitFor(() => expect(router.currentRoute.value.params.integrationId).toBe('reader'))
  expect(api.post).toHaveBeenCalledWith('/integrations/webfetch/providers', { name: 'Neu', base_url: 'https://jina.devmato.net', api_key: null, enabled: true, timeout_seconds: 60, remove_overlay: true, cookies: [{ domain: 'www.golem.de', name: 'consent', value: 'test-consent' }] })
})

it('retains a failed save and allows cancelling navigation with an unsaved draft', async () => {
  api.put.mockRejectedValue(new Error('offline'))
  confirm.mockResolvedValue(false)
  const { router, page } = await mountEditor('personal')
  await fill('Name', 'Entwurf')
  button('Speichern').click()
  await vi.waitUntil(() => api.put.mock.calls.length && !page().saving)
  await router.push('/integrations')
  expect([router.currentRoute.value.params.integrationId, page().form.name, page().dirty]).toEqual([account.id, 'Entwurf', true])
})

it('retains both tab drafts and saves permissions through the shared header', async () => {
  api.put.mockResolvedValue({ data: {} })
  const { router, page } = await mountEditor('personal')
  const tab = (label) => [...root.querySelectorAll('[role="tab"]')].find((item) => item.textContent.includes(label)).click()
  await fill('Name', 'Entwurf')
  tab('Berechtigungen')
  await vi.waitUntil(() => button('Eigene Regel hinzufügen'))
  button('Eigene Regel hinzufügen').click()
  const choose = async (label, option) => {
    const field = await vi.waitUntil(() => [...document.querySelectorAll('.q-dialog .q-field')].find(item => item.querySelector('.q-field__label')?.textContent === label))
    field.querySelector('[role="combobox"]').click()
    const choice = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find(item => item.querySelector('.q-item__label')?.textContent === option))
    choice.click()
    await vi.waitUntil(() => !document.querySelector('[role="option"]'))
  }
  await choose('Aktion', 'Kalender lesen')
  await choose('Berechtigung', 'Automatisch')
  button('Übernehmen').click()
  await vi.waitUntil(() => !document.querySelector('.q-dialog'))
  tab('Einstellungen')
  await vi.waitUntil(() => router.currentRoute.value.query.tab === 'settings')
  expect(page().form.name).toBe('Entwurf')
  tab('Berechtigungen')
  await vi.waitUntil(() => router.currentRoute.value.query.tab === 'permissions')
  button('Speichern').click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledExactlyOnceWith(`/integrations/${account.id}/permissions`, expect.objectContaining({ privatePermissions: { policies: { 'calendar.read': 'auto' }, resourcePolicies: {} } }), { params: { scope: 'personal', profile_id: null } }))
})
