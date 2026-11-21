// @vitest-environment jsdom
import { createApp, h, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Quasar } from 'quasar'
import { quasarOptions } from './quasar-options'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))
const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm }) }))

import Dialog from '../src/features/mcp/McpServerDialog.vue'
import Sessions from '../src/features/mcp/McpSessionsDialog.vue'
import Settings from '../src/features/mcp/McpSettingsPage.vue'
import { useMcpServerStore } from '../src/stores/mcp-servers'
import { api } from 'src/boot/axios'
import { absolutePathRule } from '../src/features/mcp/stdio-config'

let app, root, pinia, page
const local = {
  id: 'local-1', name: 'Lokale Tools', transport: 'stdio', scope: 'personal',
  executionAgentId: 'computer-1', executable: '/usr/bin/node', args: ['/opt/mcp/server.js'],
  workingDirectory: '/opt/mcp', idleTimeoutSeconds: 90, environment: { LANG: 'de_DE.UTF-8' },
  environmentSecretNames: ['API_TOKEN'], enabled: true
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  pinia = createPinia()
  useMcpServerStore(pinia).selectScope('personal')
  api.get.mockResolvedValue({ data: { items: [{ id: 'computer-1', alias: 'Mein Computer', connected: true }] } })
  api.post.mockResolvedValue({ data: local })
  api.put.mockResolvedValue({ data: local })
})
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks() })

async function mount(component, server = null) {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: Settings },
    { path: '/:serverId/tools', name: 'settings-mcp-tools', component: { render: () => null } }
  ] })
  await router.push('/')
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(component, { modelValue: true, server, ref: (value) => { page = value?.$?.setupState } }) })
    .use(pinia).use(router).use(Quasar, quasarOptions)
  app.config.globalProperties.$q.notify = vi.fn()
  app.mount(root)
  await vi.waitUntil(() => document.querySelector(component === Settings ? '.settings-list-item' : '.integration-dialog'))
  await nextTick()
}
function button(label) { return [...document.querySelectorAll('button')].find((item) => item.textContent.includes(label)) }
async function fill(label, value) {
  const field = [...document.querySelectorAll('.q-field')].find((item) => item.querySelector('.q-field__label')?.textContent === label)
  const input = field.querySelector('input, textarea')
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}

it('creates a local server without HTTP fields and rejects non-string arguments', async () => {
  await mount(Dialog)
  page.transport = 'stdio'
  page.localForm.executionAgentId = 'computer-1'
  await nextTick()
  await fill('Name', 'Lokale Tools')
  await fill('Ausführbare Datei', '/usr/bin/node')
  await fill('Arbeitsverzeichnis', '/opt/mcp')
  await fill('Argumente (JSON-Array)', '[123]')
  button('MCP-Server hinzufügen').click()
  await vi.waitUntil(() => document.body.textContent.includes('Gib ein JSON-Array'))
  expect(api.post).not.toHaveBeenCalled()
  await fill('Argumente (JSON-Array)', '["/opt/mcp/server.js", "with spaces"]')
  button('Geheimnis hinzufügen oder ersetzen').click()
  await nextTick()
  await fill('Variablenname', 'API_TOKEN')
  await fill('Geheimer Wert', 'test-secret')
  button('MCP-Server hinzufügen').click()
  await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/mcp/servers', {
    name: 'Lokale Tools', enabled: true, transport: 'stdio', executionAgentId: 'computer-1',
    executable: '/usr/bin/node', args: ['/opt/mcp/server.js', 'with spaces'], workingDirectory: '/opt/mcp',
    idleTimeoutSeconds: 1800, environment: {}, environmentSecrets: { API_TOKEN: 'test-secret' }, removeEnvironmentSecrets: []
  }, { params: { scope: 'personal' } }))
})

it('edits a local server while preserving its exact timeout and encrypted secrets by default', async () => {
  await mount(Dialog, local)
  await fill('Name', 'Umbenannt')
  button('Änderungen speichern').click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith('/mcp/servers/local-1', expect.objectContaining({
    name: 'Umbenannt', idleTimeoutSeconds: 90, environment: { LANG: 'de_DE.UTF-8' }, environmentSecrets: {}, removeEnvironmentSecrets: []
  }), { params: { scope: 'personal' } }))
})

it('rejects UNC paths and submits canonical Windows executable and cwd without changing arguments', async () => {
  await mount(Dialog, local)
  await fill('Ausführbare Datei', String.raw`\\computer\share\node.exe`)
  button('Änderungen speichern').click()
  await vi.waitUntil(() => document.body.textContent.includes('UNC-Pfade werden nicht unterstützt'))
  expect(api.put).not.toHaveBeenCalled()
  await fill('Ausführbare Datei', String.raw`c:\Program Files\node.exe`)
  await fill('Arbeitsverzeichnis', 'd:/mcp')
  const args = [String.raw`c:\mcp\server.js`, '--path=unchanged']
  await fill('Argumente (JSON-Array)', JSON.stringify(args))
  button('Änderungen speichern').click()
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith('/mcp/servers/local-1', expect.objectContaining({
    executable: 'C:/Program Files/node.exe', workingDirectory: 'D:/mcp', args
  }), { params: { scope: 'personal' } }))
})

it.each(['/work/../mcp', '/work//mcp', 'C:/mcp/', 'C:/NUL.txt', 'C:/bad?', '/work\u0080', '//computer/share'])(
  'rejects noncanonical computer path %s', (path) => { expect(absolutePathRule(path)).not.toBe(true) }
)

it.each(['/', 'C:/', '/opt/mcp', String.raw`c:\mcp`, 'd:/mcp'])(
  'accepts supported absolute computer path %s', (path) => { expect(absolutePathRule(path)).toBe(true) }
)

it('only ends idle processes after destructive confirmation', async () => {
  const session = { connectionId: 'local-1', sessionId: 'chat-1', id: 'process-1', idleTimeoutSeconds: 1800 }
  api.get.mockResolvedValueOnce({ data: { sessions: [{ ...session, busy: true }] } })
    .mockResolvedValueOnce({ data: { sessions: [{ ...session, busy: false }] } })
    .mockResolvedValueOnce({ data: { sessions: [] } })
  api.post.mockResolvedValueOnce({ data: { closed: true } })
  confirm.mockResolvedValueOnce(false).mockResolvedValueOnce(true)
  await mount(Sessions, local)
  await vi.waitUntil(() => button('Beenden'))
  expect(button('Beenden').disabled).toBe(true)
  button('Aktualisieren').click()
  await vi.waitUntil(() => button('Beenden') && !button('Beenden').disabled)
  button('Beenden').click()
  await vi.waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.stringContaining('Nicht gespeicherter Zustand geht verloren'), 'MCP-Sitzung beenden', { okLabel: 'Beenden' }))
  expect(api.post).not.toHaveBeenCalled()
  button('Beenden').click()
  await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/mcp/servers/local-1/sessions/chat-1/close', { processId: 'process-1' }, { params: { scope: 'personal' } }))
  await vi.waitUntil(() => document.body.textContent.includes('Keine aktiven Sitzungen'))
})

it('allows only scoped deletion after a local server computer was removed', async () => {
  const revoked = { ...local, available: true, deletedComputer: true }
  api.get.mockImplementation(async (url) => ({ data: url === '/mcp/settings'
    ? { personalAllowed: true }
    : url === '/mcp/servers/visible' ? { servers: [revoked] } : { items: [] } }))
  api.delete.mockResolvedValue({})
  confirm.mockResolvedValue(true)
  await mount(Settings)
  document.querySelector('[aria-label="Aktionen für Lokale Tools"]').click()
  await vi.waitUntil(() => document.querySelector('.q-menu .q-item'))
  const action = (label) => [...document.querySelectorAll('.q-menu .q-item')].find((item) => item.textContent.includes(label))
  const servers = useMcpServerStore(pinia)
  servers.personalAllowed = false
  await nextTick()
  expect(action('Löschen').getAttribute('aria-disabled')).toBe('true')
  servers.personalAllowed = true
  await nextTick()
  expect({
    hint: root.textContent.includes('Computer entfernt'),
    toggle: root.querySelector('[role="switch"]').getAttribute('aria-disabled'),
    permissions: root.querySelector('[aria-label="Lokale Tools: Tools und Berechtigungen anzeigen"]').getAttribute('aria-disabled'),
    sessions: action('Aktive Sitzungen').getAttribute('aria-disabled'),
    refresh: action('Tools aktualisieren').getAttribute('aria-disabled'),
    edit: action('Bearbeiten').getAttribute('aria-disabled'),
    remove: action('Löschen').getAttribute('aria-disabled')
  }).toEqual({ hint: true, toggle: 'true', permissions: 'true', sessions: 'true', refresh: 'true', edit: 'true', remove: null })
  action('Löschen').click()
  await vi.waitFor(() => expect(api.delete).toHaveBeenCalledWith('/mcp/servers/local-1', { params: { scope: 'personal' } }))
})
