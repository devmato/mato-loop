// @vitest-environment jsdom
import { createApp, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { Quasar, QBadge, QBtn, QIcon, QItem, QItemSection, QList, QMenu, QSeparator, QTab, QTabs, QTooltip, ClosePopup, Ripple, Screen } from 'quasar'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }))
const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm }) }))
vi.mock('src/app/control-center/apps', async () => {
  const { h } = await import('vue')
  const { controlCenterIntegrationManifest, controlCenterGlobalSectionManifest } = await import('src/app/control-center/manifest')
  const withViews = (items) => items.map((item) => ({
    ...item,
    component: { render: () => h('div', { 'data-app': item.id }, item.label) },
    footerComponent: item.id === 'browser' ? { render: () => h('div', { 'data-footer': 'browser' }) } : null
  }))
  const controlCenterIntegrations = withViews(controlCenterIntegrationManifest)
  const controlCenterGlobalSections = withViews(controlCenterGlobalSectionManifest)
  return {
    controlCenterIntegrations,
    controlCenterGlobalSections,
    controlCenterContentsById: new Map([...controlCenterIntegrations, ...controlCenterGlobalSections].map((item) => [item.id, item]))
  }
})

import ControlCenterPanel from '../src/app/control-center/ControlCenterPanel.vue'
import { useControlCenterStore } from '../src/stores/control-center'
import { useApprovalStore } from '../src/stores/approvals'
import { useQuestionStore } from '../src/stores/questions'
import { useInAppNotificationStore } from '../src/stores/in-app-notifications'
import { useTerminalStore } from '../src/stores/terminal'
import { useBrowserStore } from '../src/stores/browser'
import { useChatStore } from '../src/features/chat/store'
import { useMailStore } from '../src/stores/mail'
import { FakeWebSocket } from './terminal-websocket'
import { api } from 'src/boot/axios'

const components = { QBadge, QBtn, QIcon, QItem, QItemSection, QList, QMenu, QSeparator, QTab, QTabs, QTooltip }
const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
let app, root, pinia, controlCenter
let mailAccounts

beforeEach(() => {
  vi.resetAllMocks()
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  vi.stubGlobal('innerWidth', 1440)
  pinia = createPinia()
  setActivePinia(pinia)
  controlCenter = useControlCenterStore()
  controlCenter.resetDraft()
  mailAccounts = useMailStore()
  mailAccounts.accountsLoaded = true
})

afterEach(() => {
  app?.unmount()
  root?.remove()
  if (originalScrollIntoView) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', originalScrollIntoView)
  else delete HTMLElement.prototype.scrollIntoView
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  expect(api.get).not.toHaveBeenCalled()
  expect(api.post).not.toHaveBeenCalled()
  expect(api.patch).not.toHaveBeenCalled()
})

async function mountPanel() {
  root = document.createElement('div')
  document.body.append(root)
  app = createApp(ControlCenterPanel)
  app.use(pinia).use(Quasar, { components, directives: { ClosePopup, Ripple } })
  app.mount(root)
  window.dispatchEvent(new Event('resize'))
  await nextTick()
}

async function draftApp(appId) {
  await controlCenter.save({
    tabs: controlCenter.tabs.some((tab) => tab.appId === appId)
      ? controlCenter.tabs
      : [...controlCenter.tabs, { appId }],
    activeAppId: appId,
    open: true
  })
}

function notificationButton() {
  return root.querySelector('[aria-label^="Benachrichtigungen öffnen"]')
}

describe('control center panel', () => {
  it('discovers mail accounts when opening the panel, not before', async () => {
    mailAccounts.accountsLoaded = false
    api.get.mockResolvedValue({ data: [{ id: 'mail-1' }] })
    try {
      await mountPanel()
      await vi.waitFor(() => expect(mailAccounts.accounts.map(account => account.id)).toEqual(['mail-1']))
    } finally {
      api.get.mockClear()
    }
  })

  it('keeps session cancel available in the mobile panel while the browser is busy', async () => {
    await draftApp('browser')
    const chat = useChatStore()
    chat.running = true
    useBrowserStore().busy = true
    const stop = vi.spyOn(chat, 'stop').mockImplementation(() => {})
    await mountPanel()
    app.config.globalProperties.$q.screen.lt.md = true
    await nextTick()
    root.querySelector('[aria-label="Agent-Anfrage stoppen"]').click()
    expect(stop).toHaveBeenCalledOnce()
  })

  it('shows the Browser footer only while its viewer is connected', async () => {
    controlCenter.syncSession({ id: 'session-1', controlCenter: { tabs: [{ appId: 'browser' }], activeAppId: 'browser', open: true } })
    const browser = useBrowserStore()
    await mountPanel()
    expect(root.querySelector('[aria-label="App-Informationen und Steuerelemente"]')).toBeNull()
    Object.assign(browser, {
      sessionId: 'session-1', displayedSessionId: 'session-1', status: 'open', viewStatus: 'open'
    })
    await nextTick()
    expect(root.querySelector('[data-footer="browser"]')).not.toBeNull()
  })

  it('closes an app without confirmation', async () => {
    await draftApp('terminal')
    confirm.mockResolvedValueOnce(false)
    await mountPanel()
    root.querySelector('#control-center-tab-terminal').parentElement.querySelector('.app-closable-tab__close').click()
    await nextTick()
    expect(root.querySelector('#control-center-tab-terminal')).toBeNull()
  })

  it.each(['success', 'failure'])('never closes the current session shell after an older tab save settles (%s)', async (outcome) => {
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const terminal = useTerminalStore()
    terminal.connect({ sessionId: 'session-1', agentId: 'agent-1' })
    const old = FakeWebSocket.instances.at(-1)
    old.open()
    old.snapshot()
    controlCenter.syncSession({ id: 'session-1', controlCenter: { tabs: [{ appId: 'terminal' }] } })
    let finish, fail
    vi.spyOn(controlCenter, 'removeIntegration').mockImplementationOnce(() => new Promise((resolve, reject) => {
      finish = resolve
      fail = reject
    }))
    await mountPanel()
    root.querySelector('#control-center-tab-terminal').parentElement.querySelector('.app-closable-tab__close').click()
    await nextTick()
    terminal.connect({ sessionId: 'session-2', agentId: 'agent-1' })
    const current = FakeWebSocket.instances.at(-1)
    current.open()
    current.snapshot()
    controlCenter.syncSession({ id: 'session-2', controlCenter: { tabs: [{ appId: 'terminal' }] } })
    if (outcome === 'success') finish({ tabs: [] })
    else fail(new Error('save failed'))
    await nextTick()
    expect(terminal.socket).toBe(current)
    expect(terminal.ready).toBe(true)
    expect(current.close).not.toHaveBeenCalled()
    expect(current.send).not.toHaveBeenCalled()
    expect(old.send).not.toHaveBeenCalled()
    terminal.release()
  })

  it('counts only unread notices even when approvals and questions remain pending', async () => {
    const approvals = useApprovalStore()
    const questions = useQuestionStore()
    const notifications = useInAppNotificationStore()
    approvals.pending = [{ id: 'approval' }]
    questions.pending = [{ id: 'question' }]
    notifications.unreadCount = 3
    await mountPanel()

    const button = notificationButton()
    expect(button.querySelector('.q-badge').textContent).toBe('3')
    expect(button.getAttribute('aria-label')).toBe('Benachrichtigungen öffnen, 3 ungelesen')

    notifications.unreadCount = 0
    await nextTick()
    expect(button.querySelector('.q-badge')).toBeNull()
    expect(button.getAttribute('aria-label')).toBe('Benachrichtigungen öffnen, 0 ungelesen')
    expect(approvals.pending).toHaveLength(1)
    expect(questions.pending).toHaveLength(1)

  })

  it.each(['files', 'computer-files', 'file-viewer'])('keeps %s mounted but hidden while global notifications are shown', async (appId) => {
    if (appId === 'file-viewer') {
      await controlCenter.openFile({ scope: 'session', rootIdentity: 'draft', path: 'notes.md' })
    } else {
      await draftApp(appId)
    }
    const activeTabId = controlCenter.activeAppId
    await mountPanel()
    await vi.waitFor(() => expect(root.querySelector(`[data-app="${appId}"]`)).not.toBeNull())
    const content = root.querySelector(`[data-app="${appId}"]`)
    expect(content.style.display).not.toBe('none')
    expect(root.querySelector('[data-app="notifications"]')).toBeNull()

    notificationButton().click()
    await vi.waitFor(() => expect(root.querySelector('[data-app="notifications"]')).not.toBeNull())
    const notifications = root.querySelector('[data-app="notifications"]')
    expect(controlCenter.activeGlobalSectionId).toBe('notifications')
    expect(controlCenter.activeAppId).toBe(activeTabId)
    expect(root.querySelector(`[data-app="${appId}"]`)).toBe(content)
    expect(content.style.display).toBe('none')
    expect(notifications.style.display).not.toBe('none')

    vi.spyOn(controlCenter, 'activate').mockImplementation(async (tabId) => {
      controlCenter.activeGlobalSectionId = null
      controlCenter.activeAppId = tabId
    })
    root.querySelector(`[id="control-center-tab-${activeTabId}"]`).click()
    await nextTick()
    expect(controlCenter.activeGlobalSectionId).toBeNull()
    expect(controlCenter.activeContentId).toBe(activeTabId)
    expect(root.querySelector(`[data-app="${appId}"]`)).toBe(content)
    expect(content.style.display).not.toBe('none')
    expect(notifications.style.display).toBe('none')
  })

  it('uses a dense size-20 panel close button without col-auto and closes the draft', async () => {
    await controlCenter.setOpen(true)
    await mountPanel()
    const close = root.querySelector('[aria-label="Control Center schließen"]')
    expect(close.classList.contains('q-btn--dense')).toBe(true)
    expect(close.style.fontSize).toBe('20px')
    expect(close.classList.contains('col-auto')).toBe(false)
    close.click()
    await nextTick()
    expect(controlCenter.open).toBe(false)
  })

  it('navigates shared tab shells without consuming close-button keys or selecting on focus', async () => {
    await draftApp('agent-coding')
    await draftApp('mail')
    await mountPanel()
    const tabs = [...root.querySelectorAll('.app-closable-tab__tab')]
    expect(tabs).toHaveLength(3)
    tabs[0].focus()

    for (const [key, index] of [['ArrowLeft', 0], ['ArrowRight', 1], ['End', 2], ['ArrowRight', 2], ['ArrowLeft', 1], ['Home', 0]]) {
      const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
      document.activeElement.dispatchEvent(event)
      await nextTick()
      expect(event.defaultPrevented).toBe(true)
      expect(document.activeElement).toBe(tabs[index])
      expect(controlCenter.activeAppId).toBe('mail')
    }

    const close = tabs[0].parentElement.querySelector('.app-closable-tab__close')
    expect(tabs[0].contains(close)).toBe(false)
    close.focus()
    for (const key of ['ArrowRight', 'Home', 'End']) {
      const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
      close.dispatchEvent(event)
      await nextTick()
      expect(event.defaultPrevented).toBe(false)
      expect(document.activeElement).toBe(close)
    }
    expect(controlCenter.tabs).toHaveLength(4)
  })

  it('forwards drag events and drop markers on the shared shell without selecting it', async () => {
    await draftApp('mail')
    const reorder = vi.spyOn(controlCenter, 'reorderIntegration').mockResolvedValue()
    await mountPanel()
    const source = root.querySelector('#control-center-tab-files').parentElement
    const target = root.querySelector('#control-center-tab-mail').parentElement
    expect(source.draggable).toBe(true)
    expect(target.classList.contains('app-closable-tab--active')).toBe(true)
    vi.spyOn(target, 'getBoundingClientRect').mockReturnValue({ left: 0, width: 240 })
    const dataTransfer = { setData: vi.fn() }
    const drag = (element, type, clientX = 0) => element.dispatchEvent(Object.assign(
      new Event(type, { bubbles: true, cancelable: true }), { dataTransfer, clientX }
    ))
    drag(source, 'dragstart')
    expect(dataTransfer.setData).toHaveBeenCalledWith('text/plain', 'files')
    await nextTick()
    expect(source.classList.contains('control-center__tab--dragging')).toBe(true)
    drag(target, 'dragover', 10)
    await nextTick()
    expect(target.classList.contains('control-center__tab--drop-before')).toBe(true)
    drag(target, 'dragover', 230)
    await nextTick()
    expect(target.classList.contains('control-center__tab--drop-before')).toBe(false)
    expect(target.classList.contains('control-center__tab--drop-after')).toBe(true)
    drag(target, 'drop', 230)
    await nextTick()
    expect(reorder).toHaveBeenCalledExactlyOnceWith('files', 'mail', true)
    expect(source.classList.contains('control-center__tab--dragging')).toBe(false)
    expect(target.classList.contains('control-center__tab--drop-after')).toBe(false)
    expect(controlCenter.activeAppId).toBe('mail')
  })

  it('activates a tab through the real desktop overflow menu', async () => {
    await draftApp('agent-coding')
    await controlCenter.openApp('notifications')
    await mountPanel()
    await vi.waitFor(() => expect(Screen.gt.sm).toBe(true))

    // jsdom can trigger this menu; its CSS visibility is checked in a real browser.
    root.querySelector('[aria-label="Geöffnete Tabs anzeigen"]').click()
    await vi.waitFor(() => expect(document.querySelector('.q-menu')).not.toBeNull())
    const item = [...document.querySelectorAll('.q-menu .q-item')].find((item) => item.textContent.includes('Ablage'))
    expect(item).toBeDefined()
    vi.spyOn(controlCenter, 'activate').mockImplementation(async (tabId) => {
      controlCenter.activeGlobalSectionId = null
      controlCenter.activeAppId = tabId
    })
    item.click()
    await nextTick()
    expect(controlCenter.activeAppId).toBe('files')
    expect(controlCenter.activeGlobalSectionId).toBeNull()
    expect(root.querySelector('#control-center-tab-files').getAttribute('aria-selected')).toBe('true')
  })
})
