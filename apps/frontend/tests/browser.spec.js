// @vitest-environment jsdom
import { createApp, h, nextTick, onMounted } from 'vue'
import { createPinia, getActivePinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useBrowserStore } from '../src/stores/browser'
import { useBrowserSetupStore } from '../src/stores/browser-setup'
import { browserNavigationUrl, browserSocketUrl, clipboardFits } from '../src/features/browser/helpers'
import { FakeWebSocket } from './terminal-websocket'
import BrowserSetupDialog from '../src/features/browser/BrowserSetupDialog.vue'

const { api, notifyError, viewers, terminals } = vi.hoisted(() => ({
  api: { get: vi.fn(), put: vi.fn(), post: vi.fn() }, notifyError: vi.fn(), viewers: [], terminals: []
}))
vi.mock('src/boot/axios', () => ({ api }))
vi.mock('src/utils/notifications', () => ({ notifyError }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm: vi.fn() }) }))
vi.mock('@xterm/xterm', () => ({ Terminal: class {
  static strings = {}
  constructor(options) { this.options = options; terminals.push(this) }
  loadAddon() {}
  open() {}
  onData(callback) { this.input = callback }
  dispose() {}
  reset() {}
  write(_text, callback) { callback?.() }
} }))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }))
vi.mock('../src/features/browser/view', () => ({ BrowserView: class extends EventTarget {
  constructor(host, url) {
    super()
    this.url = url
    this.releaseInput = vi.fn()
    this.disconnect = vi.fn()
    this.blur = vi.fn()
    this.input = document.createElement('textarea')
    host.append(this.input)
    viewers.push(this)
  }
} }))

const context = { sessionId: 'session-1', agentId: 'agent-1', integrationId: 'browser-1' }
const state = { type: 'state', browserId: 'runtime-1', viewId: 'view-1', busy: false }
const navigation = { type: 'navigation', browserId: 'runtime-1', tabs: [
  { id: 'tab-1', title: 'Example', url: 'https://example.test/', loading: false }
], activeTabId: 'tab-1', canGoBack: false, canGoForward: false, loading: false }
let browser, socket
beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  FakeWebSocket.instances = []
  viewers.length = 0
  terminals.length = 0
  vi.stubGlobal('WebSocket', FakeWebSocket)
  browser = useBrowserStore()
  browser.setDisplayedSession(context.sessionId)
})
afterEach(() => { browser.release(); useBrowserSetupStore().release(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('keeps Windows setup output read-only while installation is running', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const root = document.createElement('div')
  document.body.append(root)
  const app = createApp({ render: () => h(BrowserSetupDialog, {
    modelValue: true, integrationId: 'browser-1', interactive: false
  }) }).use(getActivePinia())
  const container = { setup: (_props, { slots }) => () => h('div', slots.default?.()) }
  for (const name of ['QCard', 'QCardSection', 'QBtn', 'QBanner', 'QCardActions']) app.component(name, container)
  app.component('QDialog', { setup: (_props, { slots, emit }) => {
    onMounted(() => emit('show'))
    return () => h('div', slots.default?.())
  } })
  app.directive('close-popup', {})
  app.config.globalProperties.$q = { screen: { lt: { sm: false } } }
  try {
    app.mount(root)
    const setup = useBrowserSetupStore()
    setup.socket.open()
    setup.socket.message({ type: 'state', state: 'installing' })
    await nextTick()
    terminals[0].input('ignored')
    expect({
      disabled: terminals[0].options.disableStdin,
      input: setup.socket.send.mock.calls.map(([raw]) => JSON.parse(raw)).filter(message => message.type === 'input')
    }).toEqual({ disabled: true, input: [] })
  } finally { app.unmount(); root.remove() }
})

async function open() {
  browser.connect(context)
  browser.activate(JSON.stringify(context), context, true)
  socket = browser.socket
  socket.open()
  socket.message(state)
  const host = document.createElement('div')
  vi.spyOn(host, 'getClientRects').mockReturnValue([{ width: 1000, height: 700 }])
  await browser.mountView(host)
  viewers.at(-1).dispatchEvent(new Event('connect'))
  viewers.at(-1).dispatchEvent(new Event('ready'))
  socket.message({ ...navigation, completed: true })
  socket.send.mockClear()
  return viewers.at(-1)
}

function transferConfig() {
  return { params: { browserId: state.browserId, sessionId: context.sessionId, viewId: state.viewId },
    signal: expect.any(AbortSignal), suppressErrorToast: true }
}

it('binds a viewer without a model attachment handshake', async () => {
  const viewer = await open()
  expect(viewer.url).toContain('sessionId=session-1&browserId=runtime-1&viewId=view-1')
  expect(viewer.readOnly).toBe(false)
  expect(socket.send).not.toHaveBeenCalled()
})

it.each(['view', 'control'])('restores the same browser after a %s disconnect without replaying navigation', async (target) => {
  vi.useFakeTimers()
  const viewer = await open()
  browser.navigate('reload')
  socket.send.mockClear()
  if (target === 'view') viewer.dispatchEvent(new Event('disconnect'))
  else { socket.close(); socket.onclose({ code: 1006 }) }
  await vi.advanceTimersByTimeAsync(1000)
  if (target === 'control') {
    socket = browser.socket
    expect(socket.url).toContain('expectedBrowserId=runtime-1')
    socket.open()
    socket.message({ ...state, viewId: 'view-2' })
    await browser.mountView(browser.viewParent)
  }
  viewers.at(-1).dispatchEvent(new Event('connect'))
  viewers.at(-1).dispatchEvent(new Event('ready'))
  socket.message({ ...navigation, completed: true })
  expect({ ready: browser.ready, browserId: browser.browserId, attempts: browser.reconnectAttempts, commands: socket.send.mock.calls.map(([raw]) => JSON.parse(raw).action) })
    .toEqual({ ready: true, browserId: 'runtime-1', attempts: 0, commands: target === 'control' ? ['state'] : [] })
})

it('limits reconnects that receive state but never display an image', async () => {
  vi.useFakeTimers()
  const viewer = await open()
  viewer.dispatchEvent(new Event('disconnect'))
  for (const delay of [1000, 2000, 3000]) {
    await vi.advanceTimersByTimeAsync(delay)
    viewers.at(-1).dispatchEvent(new Event('connect'))
    viewers.at(-1).dispatchEvent(new Event('disconnect'))
  }
  browser.activate(null, null, false)
  browser.activate(JSON.stringify(context), context, true)
  await vi.advanceTimersByTimeAsync(10000)
  expect({ status: browser.status, attempts: viewers.length, notices: notifyError.mock.calls.length })
    .toEqual({ status: 'closed', attempts: 4, notices: 1 })
})

it('pauses retries while parked and resumes them when the view becomes eligible', async () => {
  vi.useFakeTimers()
  const viewer = await open()
  viewer.dispatchEvent(new Event('disconnect'))
  browser.parkView()
  await vi.advanceTimersByTimeAsync(10000)
  expect(viewers).toHaveLength(1)
  browser.activate(JSON.stringify(context), context, true)
  await vi.advanceTimersByTimeAsync(1000)
  expect(viewers).toHaveLength(2)
})

it.each(['close', 'session'])('cancels pending retries on %s changes', async (change) => {
  vi.useFakeTimers()
  const viewer = await open()
  viewer.dispatchEvent(new Event('disconnect'))
  if (change === 'close') browser.release({ close: true })
  else {
    browser.setDisplayedSession('session-2')
    browser.activate('session-2', { ...context, sessionId: 'session-2' }, true)
  }
  await vi.advanceTimersByTimeAsync(10000)
  expect(viewers).toHaveLength(1)
})

it('does not reopen a browser after the backend refuses its previous runtime', async () => {
  vi.useFakeTimers()
  await open()
  socket.close()
  socket.onclose({ code: 1006 })
  await vi.advanceTimersByTimeAsync(1000)
  browser.socket.message({ type: 'error', error: { code: 'browser.notConnected' } })
  browser.activate(null, null, false)
  browser.activate(JSON.stringify(context), context, true)
  await vi.advanceTimersByTimeAsync(10000)
  expect(FakeWebSocket.instances).toHaveLength(2)
})

it('retains a parked viewer on remount but never exposes another chat as its own', async () => {
  const viewer = await open(true)
  const host = browser.viewHost
  browser.parkView()
  await browser.mountView(document.createElement('div'))
  expect(browser.viewHost).toBe(host)
  expect(viewers).toHaveLength(1)
  browser.setDisplayedSession('session-2')
  expect(browser.manual).toBe(false)
  expect(browser.displayNavigation).toBeNull()
  expect(viewer.readOnly).toBe(true)
})

it('opens a separate chat binding and ignores retired controls and stream events', async () => {
  const oldViewer = await open(true)
  const oldSocket = socket
  browser.setDisplayedSession('session-2')
  browser.activate('session-2', { ...context, sessionId: 'session-2' }, true)
  const next = browser.socket
  next.open()
  next.message({ ...state, browserId: 'runtime-2', viewId: 'view-2' })
  expect(browser.sessionId).toBe('session-2')
  expect(browser.browserId).toBe('runtime-2')
  oldSocket.message(state)
  oldViewer.dispatchEvent(new Event('disconnect'))
  expect(browser.status).toBe('open')
  expect(browser.browserId).toBe('runtime-2')
})

it('keeps observations distinct from navigation acknowledgments and applies input locks', async () => {
  const viewer = await open()
  expect(browser.navigate('new')).toBe(true)
  socket.message({ ...navigation, completed: false })
  expect(browser.navigationPending).toBe(true)
  expect(viewer.readOnly).toBe(true)
  socket.message({ ...navigation, completed: true })
  expect(browser.manual).toBe(true)
  socket.message({ ...state, busy: true })
  expect(viewer.readOnly).toBe(true)
  expect(browser.navigate('new')).toBe(false)
  socket.message(state)
  expect(viewer.readOnly).toBe(false)
})

it('restores manual input after an agent action without replacing the viewer', async () => {
  const viewer = await open(true)
  socket.message({ ...state, busy: true })
  socket.message({ ...state, busy: false })
  expect(browser.manual && browser.viewer === viewer).toBe(true)
})

it('coalesces bounded resize requests without replacing the live viewer', async () => {
  vi.useFakeTimers()
  const viewer = await open()
  browser.resize(1000, 700)
  browser.resize(390, 600)
  await vi.advanceTimersByTimeAsync(250)
  expect(socket.send).toHaveBeenCalledExactlyOnceWith(JSON.stringify({ type: 'resize', width: 390, height: 600 }))
  expect(browser.viewer).toBe(viewer)
})

it.each(['put', 'post'])('drops stale clipboard %s responses after a chat change', async (method) => {
  await open()
  let resolve
  api[method].mockReturnValue(new Promise((done) => { resolve = done }))
  const result = method === 'post' ? browser.copyClipboard() : browser.transferClipboard('Grüße 🌍')
  browser.setDisplayedSession('session-2')
  resolve({ data: { text: 'private' } })
  expect(await result).toBeNull()
  expect(notifyError).not.toHaveBeenCalled()
})

it('blocks every manual file/clipboard request during agent actions', async () => {
  await open()
  browser.busy = true
  expect(await browser.transferClipboard('secret')).toBeNull()
  expect(await browser.copyClipboard()).toBeNull()
  expect(await browser.transfer('downloads')).toBeNull()
  expect(api.put).not.toHaveBeenCalled()
  expect(api.post).not.toHaveBeenCalled()
})

it('binds clipboard and file operations to the current login view even with model access', async () => {
  await open(true)
  api.put.mockResolvedValue({ data: { text: 'Grüße 🌍' } })
  await browser.transferClipboard('Grüße 🌍')
  expect(api.put).toHaveBeenCalledWith('/browsers/browser-1/clipboard', { text: 'Grüße 🌍' }, {
    params: { browserId: 'runtime-1', sessionId: 'session-1', viewId: 'view-1' }, signal: expect.any(AbortSignal), suppressErrorToast: true
  })
  api.get.mockResolvedValue({ data: { downloads: [] } })
  await browser.transfer('downloads')
  expect(api.get.mock.calls.at(-1)[1].params.viewId).toBe('view-1')
})

it('keeps setup independent of chat/browser authority and never starts installation implicitly', () => {
  const setup = useBrowserSetupStore()
  setup.connect('browser-1')
  const connection = FakeWebSocket.instances.at(-1)
  connection.open()
  expect(connection.send).not.toHaveBeenCalledWith(expect.stringContaining('start'))
  browser.release()
  expect(setup.socket).toBe(connection)
})


it('downloads saved browser results even while the agent controls the browser', async () => {
  await open()
  browser.busy = true
  vi.useFakeTimers()
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:download'), revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  api.get.mockResolvedValue({ data: new Blob(['result']) })
  await browser.download({ id: 'download-1', status: 'saved', path: 'result.txt', filename: 'result.txt' })
  expect(api.get).toHaveBeenCalledWith('/workspace/files/download', {
    signal: expect.any(AbortSignal), suppressErrorToast: true, responseType: 'blob',
    params: { scope: 'session', session_id: 'session-1', path: 'result.txt' }
  })
})

it('validates navigation and UTF-8 transfer boundaries without accepting arbitrary endpoints', () => {
  expect(browserNavigationUrl('example.com')).toBe('https://example.com/')
  expect(browserNavigationUrl('javascript:alert(1)')).toBeNull()
  expect(browserNavigationUrl('https://user:secret@example.test')).toBeNull()
  expect(clipboardFits('ä'.repeat(8192))).toBe(false)
  expect(browserSocketUrl('browser-1', '/view', context)).toContain('/api/browsers/browser-1/view?')
})

it('automatically chooses pending file fields and offers an explicit retry without model arguments', async () => {
  await open()
  browser.pickerContext = { sessionId: 'session-1' }
  api.post.mockResolvedValueOnce({ data: { uploadId: 'used', chosen: true } })
  await browser.chooseSource({ scope: 'session', path: 'example.txt' })
  expect(browser.uploadId).toBeNull()
  expect(await browser.transfer('choose')).toBeNull()
  expect(api.post).toHaveBeenCalledTimes(1)
  api.post.mockResolvedValueOnce({ data: { uploadId: 'staged', chosen: false } })
  await browser.chooseSource({ scope: 'session', path: 'example.txt' })
  expect(browser.uploadId).toBe('staged')
  api.post.mockRejectedValueOnce(new Error('No pending file chooser'))
  await expect(browser.transfer('choose')).rejects.toThrow('No pending file chooser')
  expect(browser.uploadId).toBe('staged')
  expect(browser.manual).toBe(true)
  api.post.mockResolvedValueOnce({ data: { ok: true } })
  await browser.transfer('choose')
  expect(api.post).toHaveBeenLastCalledWith('/browsers/browser-1/upload/staged/choose', null, transferConfig())
  expect(browser.uploadId).toBeNull()
})

it('browses both isolated workspace roots and fences late picker results', async () => {
  await open()
  const picker = {
    sessionId: 'session-1', rootIdentity: 'session-1', executionAgentId: 'agent-1',
    currentDirectory: '/work', filesystemList: true, filesystemImport: true
  }
  api.get.mockResolvedValueOnce({ data: { entries: [{ kind: 'directory', name: 'docs', path: 'docs' }] } })
  expect(browser.openPicker(picker)).toBe(true)
  await vi.waitFor(() => expect(browser.pickerEntries).toHaveLength(1))
  expect(api.get).toHaveBeenLastCalledWith('/workspace/entries', {
    params: { scope: 'session', session_id: 'session-1', root_identity: 'session-1', path: '.' },
    signal: expect.any(AbortSignal), suppressErrorToast: true
  })
  api.get.mockResolvedValueOnce({ data: { entries: [{ kind: 'file', name: 'local.txt', path: '/work/local.txt' }] } })
  await browser.setPickerScope('filesystem')
  expect(browser.pickerEntries[0].path).toBe('/work/local.txt')
  expect(api.get).toHaveBeenLastCalledWith('/workspace/entries', {
    params: { scope: 'filesystem', session_id: 'session-1', execution_agent_id: 'agent-1', current_directory: '/work', path: '/work' },
    signal: expect.any(AbortSignal), suppressErrorToast: true
  })
  let finish
  api.get.mockImplementationOnce((path, config) => new Promise((resolve) => { finish = resolve; expect(config.signal.aborted).toBe(false) }))
  const pending = browser.openPickerDirectory('/work/next')
  browser.resetPicker()
  finish({ data: { entries: [{ kind: 'file', name: 'late', path: '/work/next/late' }] } })
  expect(await pending).toBe(false)
  expect(browser.pickerEntries).toEqual([])
})

it('ignores a late choose response instead of clearing a new connection upload', async () => {
  await open()
  browser.uploadId = 'old'
  let finish
  api.post.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const pending = browser.transfer('choose')
  expect(browser.manual).toBe(false)
  browser.release()
  await open()
  browser.uploadId = 'new'
  finish({ data: { ok: true } })
  expect(await pending).toBeNull()
  expect(browser.uploadId).toBe('new')
})
