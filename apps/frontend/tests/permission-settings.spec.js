// @vitest-environment jsdom
import { createApp, h, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar } from 'quasar'
import { quasarOptions } from './quasar-options'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), put: vi.fn() } }))
const { confirm } = vi.hoisted(() => ({ confirm: vi.fn(async () => true) }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm }) }))

import ToolPage from '../src/features/settings/ToolSettingsPage.vue'
import IntegrationPage from '../src/features/integrations/IntegrationAccessPage.vue'
import IntegrationPermissionEditor from '../src/features/integrations/IntegrationPermissionEditor.vue'
import ProfileIntegrationDefault from '../src/features/integrations/ProfileIntegrationDefault.vue'
import GeneralPage from '../src/features/settings/GeneralSettingsPage.vue'
import UserGeneralPage from '../src/features/settings/UserGeneralSettingsPage.vue'
import { api } from 'src/boot/axios'
import { useAuthStore } from '../src/stores/auth'
import { useIntegrationStore } from '../src/stores/integrations'
import { useExecutionAgentStore } from '../src/stores/execution-agents'

let app, root
const builtinId = '00000000-0000-4000-8000-000000000001'
const ownId = '00000000-0000-4000-8000-000000000005'
const branch = () => ({ policies: {}, integrationPolicies: {}, resourcePolicies: {} })
const rights = () => ({ privatePermissions: branch(), publicPermissions: branch() })
const action = (capability) => ({ capability, label: 'Schreiben', policy: 'ask', configurable: true, allowedPolicies: ['disabled', 'ask', 'auto'] })
const groups = (capability, toolName) => Object.fromEntries(['private', 'public'].map((trust) => [trust, [{ id: 'test', label: 'Rechte', actions: toolName ? [] : [action(capability)], tools: toolName ? [{ name: toolName, label: 'Shell', actions: [action(capability)] }] : [] }]]))

async function mount(Page, props, path, name, pinia = createPinia()) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/tools', name: 'settings-tools', component: Page }, { path: '/admin/tools', name: 'admin-tools', component: Page }, { path: '/admin/providers', name: 'admin-providers', component: { render: () => null } }, { path: '/integrations/:integrationId/resources', name: 'settings-integration-resources', component: Page }, { path: '/integrations', name: 'settings-integrations', component: { render: () => null } }] })
  await router.push(path)
  expect(router.currentRoute.value.name).toBe(name)
  root = document.createElement('div')
  document.body.append(root)
  let page
  app = createApp({ render: () => h(RouterView, null, {
    default: ({ Component }) => h(Component, { ...props, ref: (value) => { page = value?.$?.setupState } })
  }) })
  app.use(pinia).use(router).use(Quasar, quasarOptions)
  app.config.globalProperties.$q.notify = vi.fn()
  app.mount(root)
  return { page: () => page, pinia, router }
}
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.clearAllMocks() })

it.each([[builtinId, 'instance'], [ownId, 'personal']])('saves native loading through the existing profile override for %s', async (profileId, scope) => {
  const data = { integrationToolLoading: null, inheritedIntegrationToolLoading: 'native' }
  api.get.mockResolvedValue({ data })
  api.put.mockResolvedValue({ data: { ...data, integrationToolLoading: 'native' } })
  await mount(ProfileIntegrationDefault, { profileId, scope }, '/tools', 'settings-tools')
  await vi.waitUntil(() => root.textContent.includes('Vorgabe: Native sofort'))
  const control = await vi.waitUntil(() => [...root.querySelectorAll('.q-field')].find(field => field.querySelector('.q-field__label')?.textContent === 'Integrations-Tool-Verfügbarkeit')?.querySelector('[role="combobox"]:not([disabled])'))
  control.focus()
  control.click()
  const native = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find(item => item.querySelector('.q-item__label')?.textContent === 'Native sofort'))
  native.click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith(`/integrations/profiles/${profileId}/availability`, { integrationToolLoading: 'native' }, { params: { scope } }))
})

it('saves the public address and agent updates and links to provider and tool settings', async () => {
  const current = { publicBaseUrl: null, effectivePublicBaseUrl: 'https://mato.test', publicUrlSource: 'automatic', modelTrustEnabled: true, agentAutoUpdateEnabled: false }
  api.get.mockResolvedValue({ data: current })
  api.put.mockResolvedValue({ data: current })
  await mount(GeneralPage, {}, '/tools', 'settings-tools')
  await vi.waitUntil(() => root.querySelector('button:not([disabled])'))
  expect(root.textContent).toContain('Öffentliche Adresse')
  expect([...root.querySelectorAll('a')].map(link => link.getAttribute('href'))).toEqual(['/admin/providers?tab=general', '/admin/tools?tab=general'])
  root.querySelector('button').click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith('/general-settings', { publicBaseUrl: null, agentAutoUpdateEnabled: false }))
})

it('saves a personal language without changing instance settings', async () => {
  api.get.mockResolvedValue({ data: { preferredLanguage: null } })
  api.put.mockResolvedValue({ data: { preferredLanguage: 'de-DE' } })
  await mount(UserGeneralPage, {}, '/tools', 'settings-tools')
  const control = await vi.waitUntil(() => root.querySelector('[role="combobox"]:not([disabled])'))
  control.click()
  const german = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find(item => item.textContent.includes('Deutsch')))
  german.click()
  await vi.waitUntil(() => !root.querySelector('button').disabled)
  root.querySelector('button').click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith('/users/me/preferences', { preferredLanguage: 'de-DE' }))
})

it.each([true, false])('saves the base behavior while retaining visible global overrides (model distinction: %s)', async (modelTrustEnabled) => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const catalog = [{ id: 'processes', label: 'Prozesse', tools: ['Shell', 'Terminal'].map(label => ({ name: label, label, actions: [action('shell.write')] })) }]
  const presetOptions = [
    ['ask_all', 'Alles bestätigen'], ['balanced', 'Ausgewogen'], ['ask_changes', 'Komfortabel'],
    ['ask_delete', 'Automatisch außer Löschen'], ['auto_all', 'Vollautomatisch']
  ].map(([value, label]) => ({ value, label, baseline: [{ category: 'read', label: 'Lesen', policy: 'ask' }], additionalPermissions: value === 'balanced' ? ['Session-Dateien lesen, bearbeiten und organisieren'] : [], additionalConfirmations: ['ask_changes', 'ask_delete', 'auto_all'].includes(value) ? ['Variablen und Secrets verwenden'] : [] }))
  let current = { ...rights(), privatePermissions: { ...branch(), policies: { 'shell.write': 'disabled' } }, publicPermissions: { ...branch(), policies: { 'shell.write': 'auto' } }, personalSettingsEnabled: false, groups: [], privateGroups: catalog, publicGroups: catalog, shellPrefixes: { read: [], write: [] }, webAccess: { userAgent: 'MatoLoop/1.0', publicAddressesOnly: true }, toolPermissionPresets: { private: 'ask_changes', public: 'balanced' }, presetOptions }
  current.webfetch = { primaryProvider: 'builtin' }
  current.webfetchProviderOptions = [{ value: 'builtin', name: 'Eingebaut' }, { value: 'jina:reader', name: 'Reader' }]
  const chosen = { private: 'ask_delete', ...(modelTrustEnabled ? { public: 'balanced' } : {}) }
  api.get.mockImplementation(async url => ({ data: url === '/agent-profiles' ? { agents: [], effectiveAgents: [] } : current }))
  api.put.mockImplementation(async (_url, payload) => {
    current = { ...current, ...payload, toolPermissionPresets: { ...current.toolPermissionPresets, ...payload.toolPermissionPresets } }
    return { data: current }
  })
  const pinia = createPinia()
  useAuthStore(pinia).modelTrustEnabled = modelTrustEnabled
  const { page } = await mount(ToolPage, { admin: true }, '/admin/tools?tab=general', 'admin-tools', pinia)
  await vi.waitUntil(() => page().saved && !page().loading)
  const choose = async (label, option) => {
    root.querySelector(`[role="radio"][aria-label="${label}: ${option}"]`).click()
    await nextTick()
  }
  expect(root.querySelectorAll('[role="radio"]')).toHaveLength(modelTrustEnabled ? 10 : 5)
  root.querySelector(`[aria-label="${modelTrustEnabled ? 'Privat' : 'Alle Modelle'}: Ausgewogen erklären"]`).click()
  const dialog = await vi.waitUntil(() => document.querySelector('.q-dialog'))
  expect(dialog.textContent).toContain('BasisberechtigungenLesenNachfragen')
  expect(dialog.textContent).toContain('Zusätzlich automatischSession-Dateien lesen, bearbeiten und organisieren')
  dialog.querySelector('[aria-label="Dialog schließen"]').click()
  await vi.waitUntil(() => !document.querySelector('.q-dialog'))
  await choose(modelTrustEnabled ? 'Privat' : 'Alle Modelle', 'Automatisch außer Löschen')
  root.querySelector(`[aria-label="${modelTrustEnabled ? 'Privat' : 'Alle Modelle'}: Automatisch außer Löschen erklären"]`).click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog')?.textContent).toContain('Zusätzlich nachfragenVariablen und Secrets verwenden'))
  document.querySelector('.q-dialog [aria-label="Dialog schließen"]').click()
  await vi.waitUntil(() => !document.querySelector('.q-dialog'))
  if (modelTrustEnabled) await choose('Öffentlich', 'Ausgewogen')
  page().form.configuration.webfetch.primaryProvider = 'jina:reader'
  ;[...root.querySelectorAll('[role="switch"]')].find(item => item.textContent.includes('Nur öffentliche Zieladressen zulassen')).click()
  for (const [label, value] of [['User-Agent', 'Shared/2.0'], ['Maximale Antwortgröße', '7']]) {
    const input = [...root.querySelectorAll('.q-field')].find(field => field.querySelector('.q-field__label')?.textContent === label).querySelector('input')
    input.value = value
    input.dispatchEvent(new Event('input', { bubbles: true }))
  }
  root.querySelector('button').click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith('/tools/configuration', expect.objectContaining({ toolPermissionPresets: chosen, webAccess: { userAgent: 'Shared/2.0', maxResponseMiB: 7, publicAddressesOnly: false }, webfetch: { primaryProvider: 'jina:reader' } })))
  const gate = await vi.waitUntil(() => root.querySelector('[aria-label="Tool-Rechte für persönliche Agentenprofile anpassen erlauben"]:not(.disabled)'))
  gate.click()
  await vi.waitUntil(() => !root.querySelector('button').disabled)
  root.querySelector('button').click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledTimes(2))
  expect(api.put.mock.lastCall[1].toolPermissionPresets).toEqual(chosen)
  expect(api.put.mock.lastCall[1].privatePermissions.policies).toEqual({ 'shell.write': 'disabled' })
  await vi.waitUntil(() => !root.querySelector('.q-btn--loading'))
  ;[...root.querySelectorAll('[role="tab"]')].find(tab => tab.textContent.includes('Berechtigungen')).click()
  await vi.waitFor(() => expect([...root.querySelectorAll('.text-subtitle2')].map(item => item.textContent)).toEqual(['Shell', 'Terminal']))
  expect(root.textContent).not.toContain('Wirksam:')
  root.querySelector(`[aria-label="Schreiben: ${modelTrustEnabled ? 'Private Modelle' : 'Alle Modelle'} entfernen"]`).click()
  await vi.waitFor(() => expect(page().payload()).toEqual(expect.objectContaining({
    privatePermissions: expect.objectContaining({ policies: {} }),
    publicPermissions: expect.objectContaining({ policies: { 'shell.write': 'auto' } })
  })))
  expect(root.querySelectorAll('[aria-label$="entfernen"]')).toHaveLength(modelTrustEnabled ? 2 : 0)
})

it.each([true, false])('selects a tool and its action for owned profile rules (single action: %s)', async (singleAction) => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const catalog = [{ id: 'processes', label: 'Prozesse', tools: [{ name: 'shell_run', label: 'Shell-Befehl ausführen', actions: [...(singleAction ? [] : [{ ...action('shell.read'), label: 'Lesen' }]), action('shell.write')] }] }]
  api.get.mockImplementation(async (url) => ({ data: url === '/agent-profiles' ? {
    personalAgentsEnabled: false, personalToolPermissionsEnabled: false,
    agents: [{ id: builtinId, name: 'Allgemein', scope: 'instance', protected: true }, { id: 'mine', name: 'Mein Agent', scope: 'personal' }],
    effectiveAgents: [],
  } : { ...rights(), publicPermissions: { ...branch(), policies: { 'shell.write': 'disabled' } }, groupsByTrust: { private: catalog, public: catalog }, shellPrefixes: null, inheritedShellPrefixes: { read: [], write: [] } } }))
  api.put.mockResolvedValue({ data: {} })
  const pinia = createPinia()
  useAuthStore(pinia).isAdmin = true
  useAuthStore(pinia).modelTrustEnabled = false
  const { page } = await mount(ToolPage, {}, '/tools?tab=management', 'settings-tools', pinia)
  await vi.waitFor(() => expect(page().saved).toBeTruthy())
  expect(page().profileOptions.map((item) => item.value)).toEqual(['mine'])
  expect(root.textContent).not.toContain('Öffentliche Modelle')
  const button = (label) => [...document.querySelectorAll('button')].find((item) => item.textContent.includes(label))
  const select = async (label, option) => {
    const field = [...document.querySelectorAll('.q-dialog .q-field')].find((item) => item.querySelector('.q-field__label')?.textContent === label)
    const control = field.querySelector('[role="combobox"]')
    control.focus()
    control.click()
    const choice = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find((item) => item.querySelector('.q-item__label')?.textContent === option))
    choice.click()
    await vi.waitUntil(() => !document.querySelector('[role="option"]'))
  }
  button('Eigene Regel hinzufügen').click()
  await vi.waitUntil(() => document.querySelector('.q-dialog'))
  await select('Tool oder Bereich', 'Shell-Befehl ausführen')
  if (!singleAction) await select('Aktion', 'Schreiben')
  await select('Berechtigung', 'Automatisch')
  button('Übernehmen').click()
  await vi.waitUntil(() => !document.querySelector('.q-dialog'))
  root.querySelector('button').click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith('/tools/profiles/mine/permissions', expect.objectContaining({
    privatePermissions: expect.objectContaining({ policies: { 'shell.write': 'auto' } }),
    publicPermissions: expect.objectContaining({ policies: { 'shell.write': 'disabled' } })
  }), { params: { scope: 'personal' } }))
})

it('lets administration remove built-in overrides and save inheritance', async () => {
  api.get.mockImplementation(async (url) => ({ data: url === '/agent-profiles' ? {
    agents: [{ id: builtinId, name: 'Allgemein', scope: 'instance', protected: true }], effectiveAgents: [],
  } : { ...rights(), privatePermissions: { ...branch(), policies: { 'shell.write': 'disabled' } }, groupsByTrust: groups('shell.write'), shellPrefixes: null, inheritedShellPrefixes: { read: [], write: [] } } }))
  api.put.mockResolvedValue({ data: {} })
  const { page } = await mount(ToolPage, { admin: true }, `/tools?profileId=${builtinId}`, 'settings-tools')
  await vi.waitFor(() => expect(page().saved).toBeTruthy())
  expect(root.textContent).toContain('Deaktiviert')
  expect(root.textContent).toContain('Eigene Regel hinzufügen')
  root.querySelector('[aria-label="Schreiben: Private Modelle entfernen"]').click()
  await vi.waitFor(() => expect(page().payload().privatePermissions.policies).toEqual({}))
  await page().save()
  expect(api.put).toHaveBeenCalledWith(`/tools/profiles/${builtinId}/permissions`, expect.objectContaining({ privatePermissions: expect.objectContaining({ policies: {} }) }), { params: { scope: 'instance' } })
})

it('edits webfetch availability separately from permissions', async () => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const catalog = [{ id: 'web', label: 'Web', tools: [{ name: 'webfetch', label: 'Webseite abrufen', icon: 'language', available: true, actions: [action('network.fetch')] }] }]
  api.get.mockImplementation(async (url) => ({ data: url === '/agent-profiles' ? { agents: [] } : {
    ...rights(), privateGroups: catalog, publicGroups: catalog, shellPrefixes: { read: [], write: [] },
    groups: catalog,
    webAccess: { userAgent: 'MatoLoop/1.0', maxResponseMiB: 64 },
  } }))
  const { page } = await mount(ToolPage, { admin: true }, '/tools', 'settings-tools')
  await vi.waitFor(() => expect(page().saved).toBeTruthy())
  await page().selectSection('management')
  await nextTick()
  expect(root.querySelector('[aria-label="Webseite abrufen: verfügbar"]')).toBeNull()
  await page().selectSection('availability')
  await nextTick()
  expect(root.textContent).not.toContain('Nur öffentliche Zieladressen zulassen')
  root.querySelector('[aria-label="Webseite abrufen: verfügbar"]').click()
  await nextTick()
  expect(page().payload().toolAvailability.webfetch).toBe(false)
  expect(api.put).not.toHaveBeenCalled()
})

it.each(['Abbrechen', 'Übernehmen'])('handles shell prefix drafts with %s', async (decision) => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  api.get.mockImplementation(async (url) => ({ data: url === '/agent-profiles' ? {
    agents: [{ id: ownId, name: 'Eigener Agent', scope: 'instance', protected: false }], effectiveAgents: []
  } : { ...rights(), groupsByTrust: groups('shell.read', 'shell_run'), shellPrefixes: { read: [['git', 'status']], write: [] } } }))
  const { page } = await mount(ToolPage, { admin: true }, `/tools?profileId=${ownId}`, 'settings-tools')
  await vi.waitUntil(() => root.querySelector('button:not([disabled])') && page().saved)
  const button = (label) => [...document.querySelectorAll('button')].find((item) => item.textContent.includes(label))
  await page().selectSection('availability')
  await nextTick()
  root.querySelector('[aria-label="Shell: Shell-Präfixe bearbeiten"]').click()
  const textarea = await vi.waitUntil(() => document.querySelector('.q-dialog textarea'))
  textarea.value = 'git diff'
  textarea.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  button(decision).click()
  await vi.waitUntil(() => !document.querySelector('.q-dialog'))
  expect(page().payload().shellPrefixes).toEqual({ read: [['git', decision === 'Übernehmen' ? 'diff' : 'status']], write: [] })
})

it('adds an explicitly empty shell setting override and removes it back to inheritance', async () => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  api.get.mockImplementation(async (url) => ({ data: url === '/agent-profiles' ? {
    agents: [{ id: ownId, name: 'Eigener Agent', scope: 'instance', protected: false }], effectiveAgents: []
  } : { ...rights(), groupsByTrust: groups('shell.read', 'shell_run'), shellPrefixes: null, inheritedShellPrefixes: { read: [], write: [] } } }))
  const { page } = await mount(ToolPage, { admin: true }, `/tools?profileId=${ownId}`, 'settings-tools')
  await vi.waitUntil(() => page().saved)
  const button = (label) => [...document.querySelectorAll('button')].find((item) => item.textContent.includes(label))
  await page().selectSection('availability')
  await nextTick()
  expect(root.querySelector('[aria-label="Shell: Shell-Präfixe bearbeiten"]')).toBeNull()
  button('Eigene Einstellung hinzufügen').click()
  const choose = async (label, option) => {
    const field = [...document.querySelectorAll('.q-dialog .q-field')].find(item => item.querySelector('.q-field__label')?.textContent === label)
    field.querySelector('[role="combobox"]').focus()
    field.querySelector('[role="combobox"]').click()
    const choice = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find(item => item.querySelector('.q-item__label')?.textContent === option))
    choice.click()
    await vi.waitUntil(() => !document.querySelector('[role="option"]'))
  }
  await vi.waitUntil(() => document.querySelector('.q-dialog'))
  await choose('Tool', 'Shell')
  await choose('Einstellung', 'Shell-Präfixe')
  await vi.waitUntil(() => document.querySelector('.q-dialog textarea'))
  button('Übernehmen').click()
  await vi.waitUntil(() => !document.querySelector('.q-dialog'))
  expect(page().payload().shellPrefixes).toEqual({ read: [], write: [] })
  root.querySelector('[aria-label="Shell: Shell-Präfixe entfernen"]').click()
  await vi.waitFor(() => expect(page().payload().shellPrefixes).toBeNull())
})

it('retains the profile across tabs and removes only its own availability override', async () => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  api.get.mockImplementation(async url => ({ data: url === '/agent-profiles'
    ? { agents: [{ id: ownId, name: 'Eigener Agent', scope: 'instance', protected: false }] }
    : { ...rights(), groupsByTrust: groups('shell.read', 'shell_run'), toolAvailability: { shell_run: false }, inheritedToolAvailability: { shell_run: true }, shellPrefixes: { read: [['git', 'status']], write: [] } } }))
  const { page } = await mount(ToolPage, { admin: true }, `/tools?profileId=${ownId}`, 'settings-tools')
  await vi.waitUntil(() => page().saved)
  await page().selectSection('availability')
  await nextTick()
  root.querySelector('[aria-label="Shell: Verfügbarkeit entfernen"]').click()
  await vi.waitFor(() => expect(page().payload().toolAvailability).toEqual({}))
  await page().selectSection('management')
  expect([page().selectedId, page().payload().shellPrefixes]).toEqual([ownId, { read: [['git', 'status']], write: [] }])
})

it('loads global web providers when navigating back from profile tool settings', async () => {
  api.get.mockImplementation(async url => ({ data: url === '/agent-profiles'
    ? { agents: [{ id: ownId, name: 'Eigener Agent', scope: 'instance' }] }
    : { ...rights(), groups: [], groupsByTrust: { private: [], public: [] } } }))
  const { page, router } = await mount(ToolPage, { admin: true }, `/admin/tools?profileId=${ownId}`, 'admin-tools')
  await vi.waitUntil(() => page().saved && !page().loading)

  await router.replace({ name: 'admin-tools' })

  await vi.waitFor(() => expect([...root.querySelectorAll('.q-field__label')].filter(label => label.textContent === 'Anbieter')).toHaveLength(2))
})

it('sets profile resource availability after finding an MCP tool by technical name', async () => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const resources = [
    { key: 'tool:search', label: 'Tickets suchen', toolName: 'search_tickets', description: 'Search ticket contents.' },
    { key: 'tool:lookup', label: 'Ticket abrufen', toolName: 'get_ticket', description: 'Read a ticket and its entries.' }
  ]
  const permissions = { ...rights(), resourceAvailability: {} }
  await mount(IntegrationPermissionEditor, {
    modelValue: permissions, profileId: builtinId,
    data: { inherited: Object.fromEntries(['private', 'public'].map((trust) => [trust, { actions: [action('mcp.call')], resources }])) }
  }, '/tools', 'settings-tools')
  const button = label => [...document.querySelectorAll('button')].find(item => item.textContent.includes(label))
  button('Eigene Regel hinzufügen').click()
  const input = await vi.waitUntil(() => document.querySelector('.q-dialog input[role="combobox"]'))
  input.focus()
  input.value = 'get_ticket'
  input.dispatchEvent(new Event('input', { bubbles: true }))
  const option = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find((item) => item.textContent.includes('get_ticket')))
  option.click()
  await nextTick()
  expect(document.querySelector('.q-dialog').textContent).toContain('Read a ticket and its entries.')
  const actionField = [...document.querySelectorAll('.q-dialog .q-field')].find(item => item.querySelector('.q-field__label')?.textContent === 'Aktion')
  actionField.querySelector('[role="combobox"]').click()
  const availability = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find(item => item.querySelector('.q-item__label')?.textContent === 'Verfügbarkeit'))
  availability.click()
  await vi.waitUntil(() => !document.querySelector('[role="option"]'))
  document.querySelector('.q-dialog [role="switch"]').click()
  button('Übernehmen').click()
  await vi.waitUntil(() => !document.querySelector('.q-dialog'))
  expect([permissions.resourceAvailability, permissions.privatePermissions.resourcePolicies, permissions.publicPermissions.resourcePolicies]).toEqual([{ 'tool:lookup': false }, {}, {}])
})

it('saves a personal integration override for a global profile without a tool ceiling', async () => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const pinia = createPinia()
  const integrations = useIntegrationStore(pinia)
  integrations.catalog = [{ adapterType: 'caldav', availability: 'full', executionLocation: 'backend' }]
  integrations.calendar.accounts = [{ id: 'calendar', name: 'Mein Kalender', providerType: 'caldav' }]
  vi.spyOn(integrations, 'selectScope').mockImplementation(() => { integrations.scope = 'personal' })
  vi.spyOn(integrations, 'fetchCatalog').mockResolvedValue()
  vi.spyOn(useExecutionAgentStore(pinia), 'fetchAll').mockResolvedValue()
  api.get.mockImplementation(async () => ({ data: {
    integrationId: 'calendar', name: 'Mein Kalender', providerType: 'caldav', permissions: { ...rights(), available: null, privatePermissions: { ...branch(), resourcePolicies: { 'calendar:work': { 'calendar.write': 'ask' } } }, publicPermissions: { ...branch(), resourcePolicies: { 'calendar:work': { 'calendar.write': 'disabled' } } } },
    inheritedAvailable: true, profiles: [{ id: builtinId, name: 'Allgemein', scope: 'instance' }],
    inherited: Object.fromEntries(['private', 'public'].map((trust) => [trust, { actions: [action('calendar.write')], resources: [{ key: 'calendar:work', label: 'Arbeit', available: true, applicableCapabilities: ['calendar.write'] }] }]))
  } }))
  vi.spyOn(integrations, 'fetchAccess').mockResolvedValue({})
  api.put.mockResolvedValue({ data: {} })
  const { page } = await mount(IntegrationPage, {}, '/integrations/calendar/resources', 'settings-integration-resources', pinia)
  await vi.waitFor(() => expect(page().permissionsData).toBeTruthy())
  await vi.waitUntil(() => root.querySelector('[aria-label="Arbeit: verfügbar"]'))
  root.querySelector('[aria-label="Arbeit: verfügbar"]').click()
  await nextTick()
  expect([page().permissions.resourceAvailability['calendar:work'], page().permissions.privatePermissions.resourcePolicies]).toEqual([false, { 'calendar:work': { 'calendar.write': 'ask' } }])
  await page().selectProfile(builtinId)
  ;[...root.querySelectorAll('button')].find(item => item.textContent.includes('Tool-Ladeverhalten anpassen')).click()
  const loadingField = await vi.waitUntil(() => document.querySelector('.q-dialog [role="combobox"]'))
  loadingField.focus()
  loadingField.click()
  const immediate = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find(item => item.querySelector('.q-item__label')?.textContent === 'Sofort'))
  immediate.click()
  await vi.waitUntil(() => !document.querySelector('[role="option"]'))
  ;[...document.querySelectorAll('.q-dialog button')].find(item => item.textContent.includes('Übernehmen')).click()
  await vi.waitUntil(() => !document.querySelector('.q-dialog'))
  page().permissions.privatePermissions.policies['calendar.write'] = 'auto'
  await nextTick()
  expect(root.textContent).toContain('Automatisch')
  await page().save()
  expect(api.put).toHaveBeenCalledWith('/integrations/calendar/permissions', expect.objectContaining({ toolLoading: 'all', privatePermissions: expect.objectContaining({ policies: { 'calendar.write': 'auto' } }) }), { params: { scope: 'personal', profile_id: builtinId } })
  root.querySelector('.q-expansion-item .q-item').click()
  await nextTick()
  root.querySelector('[aria-label="Arbeit: Schreiben, Private Modelle entfernen"]').click()
  await vi.waitUntil(() => !page().permissions.privatePermissions.resourcePolicies['calendar:work'])
  await page().save()
  const saved = api.put.mock.lastCall[1]
  expect([saved.privatePermissions.resourcePolicies, saved.publicPermissions.resourcePolicies]).toEqual([{}, { 'calendar:work': { 'calendar.write': 'disabled' } }])
})
