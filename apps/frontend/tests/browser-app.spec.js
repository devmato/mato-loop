// @vitest-environment jsdom
import { createApp, markRaw, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { Quasar, QBtn, QCard, QCardActions, QCardSection, QDialog, QIcon, QInput, QItem, QItemLabel,
  QItemSection, QLinearProgress, QList, QMenu, QSelect, QSeparator, QSpace, QTab, QTabs, QToolbar, QTooltip, ClosePopup, Ripple, Dialog } from 'quasar'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import BrowserApp from '../src/features/browser/BrowserApp.vue'
import { useBrowserStore } from '../src/stores/browser'
import { useIntegrationStore } from '../src/stores/integrations'
import { useControlCenterStore } from '../src/stores/control-center'

const { api, chat } = vi.hoisted(() => ({ api: { get: vi.fn() }, chat: { current: null } }))
vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api }))
vi.mock('src/features/chat/store', () => ({ useChatStore: () => chat }))
vi.mock('stores/execution-agents', () => ({ useExecutionAgentStore: () => ({ byId: () => ({
  id: 'agent-1', connected: true, homeDirectory: '/home/user',
  operations: ['integration.browser.execute', 'integration.browser.view', 'filesystem.list', 'filesystem.import']
}) }) }))
vi.mock('src/utils/notifications', () => ({ notifyError: vi.fn() }))
let app, root, browser, pinia
const originalScrollIntoView = HTMLElement.prototype.scrollIntoView
beforeEach(async () => {
  vi.clearAllMocks()
  pinia = createPinia()
  setActivePinia(pinia)
  chat.current = { id: 'session-1', preferredExecutionAgentId: 'agent-1' }
  browser = useBrowserStore()
  vi.spyOn(browser, 'activate').mockImplementation(() => {})
  vi.spyOn(browser, 'mountView').mockImplementation(async (host) => { browser.viewParent = host; browser.viewVisible = true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  vi.spyOn(Element.prototype, 'getClientRects').mockReturnValue([{ width: 1000, height: 700 }])
  HTMLElement.prototype.scrollIntoView = vi.fn()
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const integrations = useIntegrationStore()
  integrations.catalog = [{ adapterType: 'browser', availability: 'full', personalOnly: true }]
  integrations.browser = { instances: [{ id: 'browser-1', name: 'Browser', enabled: true, executionAgentId: 'agent-1' }] }
  useControlCenterStore().syncSession({ id: 'session-1', controlCenter: { tabs: [{ appId: 'browser' }], activeAppId: 'browser', open: true } })
})
afterEach(() => { app?.unmount(); root?.remove(); browser.release(); HTMLElement.prototype.scrollIntoView = originalScrollIntoView; vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function mount(width = 1400, dark = false) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'chat', component: { render: () => null } }] })
  await router.push('/?session=session-1')
  root = document.createElement('div')
  document.body.append(root)
  app = createApp(BrowserApp)
  app.use(pinia).use(router).use(Quasar, { components: { QBtn, QCard, QCardActions, QCardSection, QDialog, QIcon, QInput, QItem, QItemLabel,
    QItemSection, QLinearProgress, QList, QMenu, QSelect, QSeparator, QSpace, QTab, QTabs, QToolbar, QTooltip }, directives: { ClosePopup, Ripple }, plugins: { Dialog } })
  app.config.globalProperties.$q.screen.lt.sm = width < 600
  app.config.globalProperties.$q.dark.set(dark)
  await useControlCenterStore().setOpen(true)
  app.mount(root)
  browser.$patch({ sessionId: 'session-1', displayedSessionId: 'session-1', integrationId: 'browser-1', agentId: 'agent-1',
    browserId: 'runtime-1', viewId: 'view-1', status: 'open', viewStatus: 'open', viewVisible: true,
    navigation: { tabs: [{ id: 'tab-1', url: 'https://example.test/', title: 'Example' }], activeTabId: 'tab-1', canGoBack: false, canGoForward: false } })
  browser.viewer = markRaw({ releaseInput: vi.fn(), disconnect: vi.fn(), send: vi.fn(), blur: vi.fn() })
  await nextTick()
  await nextTick()
}

it.each([[1400, false], [390, true]])('keeps website navigation available in the unified viewer (%s)', async (width, dark) => {
  await mount(width, dark)
  expect(root.querySelector('[aria-label="Webadresse"]').value).toBe('https://example.test/')
  expect(browser.activate).toHaveBeenCalledWith(expect.any(String), expect.any(Object), true)
  expect(root.textContent).not.toContain('Browser läuft im Hintergrund')
  expect(root.textContent).not.toContain('Browseroberfläche anzeigen')
})

it('offers a reconnect action in the existing empty state after retries fail', async () => {
  await mount()
  browser.connectionFailed()
  await nextTick()
  const reconnect = vi.spyOn(browser, 'connect').mockImplementation(() => {})
  root.querySelector('.app-state-message button').click()
  expect(reconnect).toHaveBeenCalledOnce()
})

it('opens saved downloads while the agent controls the browser', async () => {
  await mount()
  browser.busy = true
  browser.downloads = [{ id: 'download', status: 'saved', path: 'result.txt', filename: 'result.txt' }]
  await nextTick()
  root.querySelector('[aria-label="Downloads"]').click()
  await vi.waitFor(() => expect(document.querySelector('[aria-label="result.txt herunterladen"]')?.disabled).toBe(false))
  browser.busy = false
  api.get.mockResolvedValueOnce({ data: { entries: [{ kind: 'file', name: 'available.txt', path: 'available.txt' }] } })
  await nextTick()
  document.querySelector('[aria-label="Dateien aktualisieren"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog').textContent).toContain('available.txt'))
})

it('renders a website prompt as text and returns only the explicit human answer', async () => {
  await mount()
  browser.dialog = { type: 'prompt', message: '<script>untrusted</script>', defaultValue: 'Answer' }
  await vi.waitFor(() => expect(document.querySelector('.q-dialog')).not.toBeNull())
  expect(document.querySelector('.q-dialog script')).toBeNull()
  const answer = document.querySelector('.q-dialog input')
  answer.value = 'Changed'
  answer.dispatchEvent(new Event('input', { bubbles: true }))
  browser.dialog = { ...browser.dialog }
  await nextTick()
  const ok = [...document.querySelectorAll('.q-dialog button')].find((button) => button.textContent === 'OK')
  ok.click()
  expect(browser.viewer.send).toHaveBeenCalledWith({ kind: 'dialog', accept: true, text: 'Changed' })
})

it.each(['Abbrechen', 'Schließen'])('confirms closing the shared browser (%s)', async (decision) => {
  await mount()
  const release = vi.spyOn(browser, 'release').mockImplementation(() => {})
  root.querySelector('[aria-label="Weitere Browseraktionen"]').click()
  const close = () => [...document.querySelectorAll('.q-item')].find((item) => item.textContent.includes('Browser schließen'))
  await vi.waitFor(() => expect(close()).toBeDefined())
  close().click()
  const decide = () => [...document.querySelectorAll('.q-dialog button')].find((button) => button.textContent === decision)
  await vi.waitFor(() => expect(decide()).toBeDefined())
  decide().click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog')).toBeNull())
  expect(release.mock.calls).toEqual(decision === 'Schließen' ? [[{ close: true }]] : [])
})

it('keeps browser shortcuts out of unrelated fields, toolbar controls, hidden views and model attachment', async () => {
  await mount()
  const navigate = vi.spyOn(browser, 'navigate')
  const address = root.querySelector('input[aria-label="Webadresse"]')
  const other = document.createElement('input')
  root.querySelector('.browser-app').append(other)
  const key = (target, letter) => {
    const event = new KeyboardEvent('keydown', { key: letter, code: `Key${letter.toUpperCase()}`, ctrlKey: true, bubbles: true, cancelable: true })
    target.dispatchEvent(event)
    return event
  }
  for (const target of [other, root.querySelector('[aria-label="Weitere Browseraktionen"]')]) {
    target.focus()
    for (const letter of ['t', 'w']) expect(key(target, letter).defaultPrevented).toBe(false)
  }
  address.focus()
  for (const blocked of ['busy', 'hidden']) {
    browser.busy = blocked === 'busy'
    await useControlCenterStore().setOpen(blocked !== 'hidden')
    await nextTick()
    for (const letter of ['t', 'w']) expect(key(address, letter).defaultPrevented).toBe(false)
  }
  expect(navigate).not.toHaveBeenCalled()
})

it.each(['ctrlKey', 'metaKey'])('focuses the address field with %s+L from the remote textarea', async (modifier) => {
  await mount()
  browser.socket = markRaw({ readyState: 1, send: vi.fn(), close: vi.fn() })
  await nextTick()
  const input = document.createElement('textarea')
  browser.viewParent.append(input)
  browser.viewer.input = input
  input.focus()
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'l', code: 'KeyL', [modifier]: true, bubbles: true, cancelable: true }))
  await nextTick()
  expect(document.activeElement).toBe(root.querySelector('[aria-label="Webadresse"]'))
})

it.each([
  ['Tab', { ctrlKey: true }, 'select', { tabId: 'tab-2' }],
  ['Tab', { ctrlKey: true, shiftKey: true }, 'select', { tabId: 'tab-3' }],
  ['PageUp', { ctrlKey: true }, 'select', { tabId: 'tab-3' }],
  ['PageDown', { ctrlKey: true }, 'select', { tabId: 'tab-2' }],
  ['F5', {}, 'reload'], ['r', { metaKey: true }, 'reload'],
  ['t', { metaKey: true }, 'new'], ['w', { metaKey: true }, 'close']
])('routes %s (%j) through browser navigation', async (key, modifiers, action, args) => {
  await mount()
  browser.socket = markRaw({ readyState: 1, send: vi.fn(), close: vi.fn() })
  browser.navigation.tabs.push({ id: 'tab-2' }, { id: 'tab-3' })
  const navigate = vi.spyOn(browser, 'navigate').mockImplementation(() => {})
  const address = root.querySelector('[aria-label="Webadresse"]')
  address.focus()
  address.dispatchEvent(new KeyboardEvent('keydown', { key, ...modifiers, bubbles: true, cancelable: true }))
  expect(navigate.mock.calls).toEqual([args ? [action, args] : [action]])
})

it('shows agent activity only during browser actions in the existing tab strip', async () => {
  await mount()
  browser.busy = true
  await nextTick()
  const indicator = root.querySelector('[aria-label="Webseiten"] [role="status"] span')
  const states = [indicator.classList.contains('invisible')]
  browser.busy = false
  await nextTick()
  expect([...states, indicator.classList.contains('invisible')]).toEqual([false, true])
})

it('copies the remote selection directly when the Clipboard API is absent', async () => {
  await mount()
  vi.stubGlobal('navigator', { clipboard: undefined })
  const canvas = document.createElement('canvas')
  canvas.tabIndex = 0
  browser.viewParent.append(canvas)
  browser.viewer.input = canvas
  vi.spyOn(browser, 'copyClipboard').mockResolvedValue({ text: 'Grüße 🌍' })
  let copied
  document.execCommand = vi.fn(() => {
    copied = document.activeElement.value
    return true
  })
  try {
    canvas.focus()
    canvas.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', ctrlKey: true, bubbles: true, cancelable: true }))
    await vi.waitFor(() => expect(copied).toBe('Grüße 🌍'))
  } finally {
    delete document.execCommand
  }
})

it('selects files with Space and enters directories with Enter', async () => {
  await mount()
  api.get.mockResolvedValueOnce({ data: { entries: [
    { kind: 'directory', name: 'Dokumente', path: 'docs' },
    { kind: 'file', name: 'bericht.txt', path: 'bericht.txt', size: 4 }
  ] } })
  root.querySelector('[aria-label="Datei hochladen"]').click()
  await vi.waitFor(() => expect(document.body.textContent).toContain('bericht.txt'))
  const entries = [...document.querySelectorAll('[aria-label="Dateien und Ordner"] [role="button"]')]
  const directory = entries.find((entry) => entry.textContent.includes('Dokumente'))
  const file = entries.find((entry) => entry.textContent.includes('bericht.txt'))
  expect(file.tabIndex).toBe(0)
  file.focus()
  file.dispatchEvent(new KeyboardEvent('keyup', { key: ' ', keyCode: 32, bubbles: true, cancelable: true }))
  await nextTick()
  expect(browser.pickerSelected?.path).toBe('bericht.txt')
  expect(file.getAttribute('aria-pressed')).toBe('true')
  api.get.mockResolvedValueOnce({ data: { entries: [] } })
  directory.focus()
  directory.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', keyCode: 13, bubbles: true, cancelable: true }))
  await vi.waitFor(() => expect(browser.pickerDirectories.session).toBe('docs'))
  expect(api.get).toHaveBeenLastCalledWith('/workspace/entries', {
    params: { scope: 'session', session_id: 'session-1', root_identity: 'session-1', path: 'docs' },
    signal: expect.any(AbortSignal), suppressErrorToast: true
  })
})
