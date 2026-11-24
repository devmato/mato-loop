import { defineStore } from 'pinia'
import { markRaw } from 'vue'
import { Dark, Notify } from 'quasar'
import { api } from 'src/boot/axios'
import { browserSocketUrl, clipboardFits } from 'src/features/browser/helpers'
import { parentPath } from 'src/features/files/state'
import { notifyError } from 'src/utils/notifications'
import { BrowserView } from './view'

const retryableConnectionErrors = new Set(['browser.unavailable', 'executionAgent.unavailable', 'executionAgent.replaced', 'executionAgent.timeout'])

export const useBrowserStore = defineStore('browser', {
  state: () => ({
    sessionId: null, agentId: null, integrationId: null, browserId: null, viewId: null,
    displayedSessionId: null, displayGeneration: 0,
    requestController: null, stoppedContext: null, connectionError: null, restoreIncomplete: false,
    socket: null, generation: 0, status: 'closed', busy: false,
    viewer: null, dialog: null, viewHost: null, viewParent: null, viewGeneration: 0, viewStatus: 'closed',
    downloads: [], uploadId: null, transferring: false, scaleViewport: true,
    pickerScope: 'session', pickerDirectories: { session: '.', filesystem: '/' }, pickerEntries: [],
    pickerHasMore: false, pickerNextOffset: null, pickerSelected: null, pickerLoading: false,
    pickerError: null, pickerContext: null, pickerController: null, pickerGeneration: 0,
    activationKey: null, activationAttempted: false, viewVisible: false,
    reconnectEligible: false, reconnectTarget: null, reconnectTimer: null, reconnectAttempts: 0,
    resizeTarget: null, resizeInFlight: null, resizeLast: null, resizeTimer: null,
    navigation: null, navigationPending: false, navigationAction: null
  }),
  getters: {
    ownsDisplay: (state) => Boolean(state.sessionId && state.sessionId === state.displayedSessionId),
    displayStatus: (state) => state.ownsDisplay ? state.status : 'closed',
    displayNavigation: (state) => state.ownsDisplay ? state.navigation : null,
    ready: (state) => state.ownsDisplay && state.status === 'open' && state.viewStatus === 'open',
    manual: (state) => state.ready && state.viewVisible
      && !state.busy && !state.transferring && !state.navigationPending,
    canRespondDialog: (state) => state.ownsDisplay && state.status === 'open'
      && state.viewVisible && !state.busy && !state.transferring && !state.navigationPending,
    canNavigate: (state) => state.manual && state.navigation !== null && state.socket?.readyState === 1,
    activeTab: (state) => state.displayNavigation?.tabs.find((tab) => tab.id === state.navigation.activeTabId) ?? null
  },
  actions: {
    activate(key, context, eligible) {
      this.reconnectEligible = Boolean(key && eligible)
      if (!this.reconnectEligible) {
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
      }
      if (context && this.stoppedContext === JSON.stringify([context.integrationId, context.sessionId, context.agentId])) return
      if (key !== this.activationKey) {
        this.activationKey = key
        this.activationAttempted = false
      }
      if (!key || !eligible) return
      if (this.reconnectTarget && this.sessionId === context.sessionId
        && this.agentId === context.agentId && this.integrationId === context.integrationId) {
        this.scheduleReconnect()
        return
      }
      if (this.activationAttempted) return
      this.activationAttempted = true
      if (this.socket && this.sessionId === context.sessionId
        && this.agentId === context.agentId && this.integrationId === context.integrationId) return
      this.connect(context)
    },
    connect({ integrationId, sessionId, agentId }, { resume = false, eligible = this.reconnectEligible } = {}) {
      if (!integrationId || !sessionId || !agentId || sessionId !== this.displayedSessionId) return
      if (!resume && this.status !== 'closed' && this.integrationId === integrationId
        && this.sessionId === sessionId && this.agentId === agentId) return
      const expectedBrowserId = resume ? this.browserId : null
      if (!resume) this.release()
      const generation = this.generation
      const socket = markRaw(new WebSocket(browserSocketUrl(integrationId, '', {
        sessionId, colorScheme: Dark.isActive ? 'dark' : 'light',
        ...(expectedBrowserId ? { expectedBrowserId } : {})
      })))
      Object.assign(this, { integrationId, sessionId, agentId, status: 'connecting', socket, stoppedContext: null, connectionError: null, reconnectEligible: eligible })
      const current = () => this.generation === generation && this.socket === socket
      socket.onmessage = (event) => {
        if (!current() || typeof event.data !== 'string') return
        let message
        try { message = JSON.parse(event.data) } catch { return }
        if (message.type === 'error') {
          if (message.operation === 'navigation') {
            const requested = this.navigationPending && this.navigationAction !== 'state'
            this.navigationPending = false
            this.updateViewOnly()
            if (requested && this.ownsDisplay && this.viewVisible) notifyError({ error: message.error })
            return
          }
          if (message.operation === 'resize') {
            this.resizeInFlight = null
            this.scheduleResize()
            return
          }
          if (message.operation === 'view' && this.browserId
            && ['browser.notConnected', 'browser.unavailable', 'browser.busy'].includes(message.error?.code)) {
            this.reconnect('view')
            return
          }
          if (retryableConnectionErrors.has(message.error?.code) || (message.operation === 'runtime' && message.error?.code === 'browser.notConnected')) this.reconnect('control')
          else this.connectionFailed(message.error)
        } else if (message.type === 'navigation') {
          if (!this.browserId || message.browserId !== this.browserId) return
          const { tabs, activeTabId, canGoBack, canGoForward, loading } = message
          this.navigation = { tabs, activeTabId, canGoBack, canGoForward, loading }
          // Unsolicited observations must not acknowledge an in-flight action.
          if (message.completed === true) this.navigationPending = false
          this.updateViewOnly()
        } else if (message.type === 'resized') {
          if (message.browserId !== this.browserId || !this.resizeInFlight) return
          this.resizeInFlight = null
          this.scheduleResize()
        } else if (message.type === 'state' && message.browserId && message.viewId) {
          const replaced = this.browserId !== message.browserId
          const recovered = message.restoredTabs > 0 || message.restoreIncomplete === true
          if (this.browserId && replaced && !(resume && recovered)) {
            this.connectionFailed({ code: 'browser.bindingChanged' })
            return
          }
          if (this.viewId && this.viewId !== message.viewId) {
            this.connectionFailed({ code: 'browser.bindingChanged' })
            return
          }
          if (replaced) {
            this.restoreIncomplete = message.restoreIncomplete === true
            this.downloads = []
          }
          this.browserId = message.browserId
          this.viewId = message.viewId
          this.status = 'open'
          this.busy = message.busy === true
          this.downloads = message.downloads || this.downloads
          if (replaced && message.restoredTabs > 0 && !this.restoreIncomplete && this.ownsDisplay && this.reconnectEligible && !document.hidden) {
            Notify.create({ type: 'info', message: 'Browser-Tabs wiederhergestellt.' })
          }
          this.updateViewOnly()
          this.scheduleResize()
        }
      }
      socket.onerror = () => {
        if (!current()) return
        socket.close()
      }
      socket.onclose = (event) => {
        if (!current()) return
        if (![4401, 4403].includes(event?.code)) this.reconnect('control')
        else {
          this.connectionFailed()
        }
      }
    },
    connectionFailed(error = { code: 'browser.unavailable' }) {
      this.release({ stop: true })
      this.connectionError = error
    },
    reconnect(target) {
      this.clearView({ keepImage: true })
      this.reconnectTarget = this.reconnectTarget === 'control' ? 'control' : target
      if (this.reconnectTarget === 'control') {
        const socket = this.socket
        this.socket = null
        socket?.close()
        this.viewId = null
        this.navigation = null
        this.navigationPending = false
        this.resizeInFlight = null
        this.resizeLast = null
        this.status = 'connecting'
      }
      this.scheduleReconnect()
    },
    scheduleReconnect() {
      if (!this.reconnectTarget || this.reconnectTimer || !this.reconnectEligible || !this.ownsDisplay || document.hidden) return
      if (this.reconnectAttempts >= 3) {
        this.connectionFailed()
        return
      }
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null
        if (!this.reconnectEligible || !this.ownsDisplay || document.hidden) return
        const target = this.reconnectTarget
        this.reconnectTarget = null
        this.reconnectAttempts += 1
        if (target === 'control') this.connect(this, { resume: true })
        else void this.mountView(this.viewParent)
      }, (this.reconnectAttempts + 1) * 1000)
    },
    setDisplayedSession(sessionId) {
      sessionId ||= null
      if (sessionId === this.displayedSessionId) return
      this.parkView()
      this.displayedSessionId = sessionId
    },
    parkView() {
      this.displayGeneration += 1
      this.viewVisible = false
      this.reconnectEligible = false
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
      this.viewer?.releaseInput()
      this.updateViewOnly()
      this.viewer?.blur()
      this.viewHost?.remove()
      this.requestController?.abort()
      this.requestController = null
      this.resetPicker()
      this.transferring = false
    },
    clearView({ keepImage = false } = {}) {
      // Invalidate callbacks and requests before disconnecting the old stream.
      this.viewGeneration += 1
      this.requestController?.abort()
      this.requestController = null
      this.resetPicker()
      this.transferring = false
      this.viewer?.releaseInput()
      this.viewStatus = 'closed'
      this.updateViewOnly()
      const viewer = this.viewer
      this.viewer = null
      this.dialog = null
      viewer?.disconnect()
      if (!keepImage) {
        this.viewHost?.remove()
        this.viewHost = null
      }
    },
    async mountView(host) {
      if (!host || !this.ownsDisplay) return
      this.viewParent = markRaw(host)
      const restoring = !this.viewVisible || this.viewHost?.parentElement !== host
      this.viewVisible = true
      if (this.viewHost && this.viewHost.parentElement !== host) host.replaceChildren(this.viewHost)
      this.updateViewOnly()
      if (this.reconnectTarget || this.status !== 'open' || !this.viewId) return
      if (this.viewer) {
        if (restoring) this.viewer.fit = this.scaleViewport
        if (this.viewStatus === 'open' && !this.navigation) this.navigate('state')
        return
      }
      if (this.viewStatus === 'connecting' && this.viewer === null) return
      const generation = this.generation
      const viewGeneration = ++this.viewGeneration
      this.viewStatus = 'connecting'
      try {
        const viewHost = markRaw(document.createElement('div'))
        viewHost.className = 'fit relative-position'
        const viewer = markRaw(new BrowserView(viewHost, browserSocketUrl(this.integrationId, '/view', {
          sessionId: this.sessionId, browserId: this.browserId, viewId: this.viewId
        })))
        this.viewer = viewer
        viewer.fit = this.scaleViewport
        this.updateViewOnly()
        const current = () => generation === this.generation && viewGeneration === this.viewGeneration && this.viewer === viewer
        viewer.addEventListener('state', () => {
          if (!current()) return
          this.dialog = viewer.state.dialog
          if (!viewer.hasCurrentFrame) this.viewStatus = 'connecting'
          this.updateViewOnly()
        })
        viewer.addEventListener('connect', () => {
          if (!current()) return
          this.navigate('state')
        })
        viewer.addEventListener('ready', () => {
          if (!current()) return
          const restoreFocus = this.viewHost?.contains(document.activeElement)
          this.viewHost = viewHost
          this.reconnectAttempts = 0
          this.viewStatus = 'open'
          if (this.viewVisible && this.ownsDisplay && viewHost.parentElement !== this.viewParent) {
            this.viewParent.replaceChildren(viewHost)
          }
          this.updateViewOnly()
          if (restoreFocus && this.manual) viewer.focus()
        })
        viewer.addEventListener('disconnect', () => {
          if (!current()) return
          this.reconnect('view')
        })
      } catch {
        this.reconnect('view')
      }
    },
    toggleViewportScale() {
      if (!this.ownsDisplay) return
      this.scaleViewport = !this.scaleViewport
      if (this.viewer) this.viewer.fit = this.scaleViewport
    },
    updateViewOnly() {
      if (!this.viewer) return
      if (!this.manual) this.viewer.releaseInput()
      this.viewer.readOnly = !this.manual
      this.viewer.dialogEnabled = Boolean(this.dialog) && this.canRespondDialog
      this.viewer.watchFrame?.(this.ownsDisplay && this.viewVisible && !document.hidden)
    },
    resize(width, height) {
      if (!this.ownsDisplay || !this.viewVisible || !Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return
      this.resizeTarget = {
        width: Math.min(3840, Math.max(320, Math.floor(width))),
        height: Math.min(2160, Math.max(240, Math.floor(height)))
      }
      this.scheduleResize()
    },
    scheduleResize() {
      clearTimeout(this.resizeTimer)
      this.resizeTimer = null
      if (!this.resizeTarget || this.resizeInFlight) return
      this.resizeTimer = setTimeout(() => {
        this.resizeTimer = null
        const box = this.viewParent?.getClientRects()[0]
        if (!this.ownsDisplay || !this.viewVisible || document.hidden || !(box?.width > 0 && box?.height > 0)
          || this.status !== 'open' || this.socket?.readyState !== 1 || this.resizeInFlight) return
        const target = this.resizeTarget
        if (!target || (target.width === this.resizeLast?.width && target.height === this.resizeLast?.height)) return
        // Remember failed targets too: only a new size or explicit retry may resend them.
        this.resizeLast = target
        this.resizeInFlight = target
        this.socket.send(JSON.stringify({ type: 'resize', ...target }))
      }, 200)
    },
    navigate(action, { tabId, url } = {}) {
      if (!this.ownsDisplay || this.status !== 'open' || this.socket?.readyState !== 1 || this.navigationPending) return false
      if (action !== 'state') {
        if (!this.canNavigate) return false
        if (['select', 'close', 'navigate', 'back', 'forward', 'reload', 'stop'].includes(action)) {
          tabId ??= this.navigation.activeTabId
          if (!this.navigation.tabs.some((tab) => tab.id === tabId)) return false
        }
        if (action === 'new' && this.navigation.tabs.length >= 32) return false
      }
      this.navigationPending = true
      this.navigationAction = action
      this.updateViewOnly()
      this.socket.send(JSON.stringify({ type: 'navigation', action, tabId, url }))
      return true
    },
    copyClipboard() {
      return this.transferClipboard(undefined, { copy: true })
    },
    async request(callback) {
      if (!this.ownsDisplay) return null
      this.requestController ??= markRaw(new AbortController())
      const { signal } = this.requestController
      const generation = this.generation
      const viewGeneration = this.viewGeneration
      const displayGeneration = this.displayGeneration
      const current = () => !signal.aborted && generation === this.generation && viewGeneration === this.viewGeneration
        && displayGeneration === this.displayGeneration
      try {
        const { data } = await callback({
          params: { browserId: this.browserId, sessionId: this.sessionId, viewId: this.viewId },
          signal, suppressErrorToast: true
        })
        return current() ? data : null
      } catch (error) {
        if (!current()) return null
        notifyError(error)
        throw error
      }
    },
    async transferClipboard(text, { copy = false } = {}) {
      if (!this.manual || (copy ? text !== undefined : !clipboardFits(text))) return null
      const generation = this.generation
      const viewGeneration = this.viewGeneration
      const displayGeneration = this.displayGeneration
      const path = `/browsers/${this.integrationId}/clipboard`
      this.transferring = true
      this.updateViewOnly()
      try {
        const data = await this.request((config) => copy
          ? api.post(`${path}/copy`, null, config)
          : api.put(path, { text }, config))
        if (!data || generation !== this.generation || viewGeneration !== this.viewGeneration || displayGeneration !== this.displayGeneration
          || !this.ready || this.busy) return null
        if (!clipboardFits(data.text)) {
          notifyError({}, 'Der Browsertext muss kleiner als 16 KiB sein. Bitte eine kleinere Auswahl kopieren.')
          return null
        }
        return data
      } finally {
        if (generation === this.generation && viewGeneration === this.viewGeneration && displayGeneration === this.displayGeneration) {
          this.transferring = false
          this.updateViewOnly()
        }
      }
    },
    async transfer(action) {
      if (!this.manual || (action === 'choose' && !this.uploadId)) return null
      const generation = this.generation
      const viewGeneration = this.viewGeneration
      const displayGeneration = this.displayGeneration
      const path = `/browsers/${this.integrationId}`
      this.transferring = true
      this.updateViewOnly()
      try {
        if (action === 'choose') {
          const data = await this.request((config) => api.post(`${path}/upload/${encodeURIComponent(this.uploadId)}/choose`, null, config))
          if (!data || generation !== this.generation || viewGeneration !== this.viewGeneration || displayGeneration !== this.displayGeneration) return null
          if (data.ok) this.uploadId = null
          return data
        }
        const data = await this.request((config) => api.get(`${path}/downloads`, config))
        if (!data || generation !== this.generation || viewGeneration !== this.viewGeneration || displayGeneration !== this.displayGeneration) return null
        this.downloads = data.downloads
        return data
      } finally {
        if (generation === this.generation && viewGeneration === this.viewGeneration && displayGeneration === this.displayGeneration) {
          this.transferring = false
          this.updateViewOnly()
        }
      }
    },
    resetPicker() {
      this.pickerController?.abort()
      this.pickerController = null
      this.pickerGeneration += 1
      this.pickerEntries = []
      this.pickerHasMore = false
      this.pickerNextOffset = null
      this.pickerSelected = null
      this.pickerLoading = false
      this.pickerError = null
      this.pickerContext = null
    },
    openPicker(context) {
      if (!this.ownsDisplay || context?.sessionId !== this.sessionId) return false
      this.resetPicker()
      this.pickerContext = { ...context, currentDirectory: context.currentDirectory || context.homeDirectory || '/' }
      this.pickerDirectories = {
        session: '.',
        filesystem: context.currentDirectory || context.homeDirectory || '/'
      }
      this.pickerScope = 'session'
      void this.loadPicker()
      return true
    },
    pickerParams(extra = {}, read = true) {
      const filesystem = this.pickerScope === 'filesystem'
      return {
        scope: this.pickerScope,
        session_id: this.sessionId,
        ...(filesystem ? {
          execution_agent_id: this.pickerContext.executionAgentId,
          current_directory: this.pickerContext.currentDirectory
        } : read && this.pickerContext.rootIdentity ? { root_identity: this.pickerContext.rootIdentity } : {}),
        ...extra
      }
    },
    async setPickerScope(scope) {
      if (!this.manual || !['session', 'filesystem'].includes(scope) || !this.pickerContext
        || (scope === 'filesystem' && !this.pickerContext.filesystemList)) return false
      this.pickerScope = scope
      return this.loadPicker()
    },
    openPickerDirectory(path) {
      if (typeof path !== 'string' || (this.pickerScope === 'filesystem' && !path.startsWith('/'))) return false
      this.pickerDirectories[this.pickerScope] = path
      return this.loadPicker()
    },
    pickerParent() {
      return parentPath(this.pickerDirectories[this.pickerScope])
    },
    async loadPicker(append = false) {
      if (!this.manual || !this.pickerContext || (this.pickerScope === 'filesystem' && !this.pickerContext.filesystemList)) return false
      this.pickerController?.abort()
      const controller = markRaw(new AbortController())
      this.pickerController = controller
      const generation = ++this.pickerGeneration
      const browserGeneration = this.generation
      const viewGeneration = this.viewGeneration
      const displayGeneration = this.displayGeneration
      const directory = this.pickerDirectories[this.pickerScope]
      this.pickerLoading = true
      this.pickerError = null
      try {
        const { data } = await api.get('/workspace/entries', {
          params: this.pickerParams({ path: directory, ...(append ? { offset: this.pickerNextOffset } : {}) }),
          signal: controller.signal,
          suppressErrorToast: true
        })
        if (controller.signal.aborted || generation !== this.pickerGeneration || browserGeneration !== this.generation
          || viewGeneration !== this.viewGeneration || displayGeneration !== this.displayGeneration) return false
        const entries = Array.isArray(data.entries) ? data.entries : []
        this.pickerEntries = append ? [...this.pickerEntries, ...entries] : entries
        this.pickerHasMore = data.hasMore === true
        this.pickerNextOffset = data.nextOffset ?? null
        this.pickerSelected = null
        return true
      } catch (error) {
        if (!controller.signal.aborted && generation === this.pickerGeneration && browserGeneration === this.generation
          && viewGeneration === this.viewGeneration && displayGeneration === this.displayGeneration) this.pickerError = error
        return false
      } finally {
        if (generation === this.pickerGeneration) this.pickerLoading = false
        if (this.pickerController === controller) this.pickerController = null
      }
    },
    async chooseSource(source) {
      if (!this.manual || !this.pickerContext || !['session', 'filesystem'].includes(source?.scope)
        || typeof source.path !== 'string' || !source.path
        || (source.scope === 'filesystem' && !source.path.startsWith('/'))) return null
      const generation = this.generation
      const viewGeneration = this.viewGeneration
      const displayGeneration = this.displayGeneration
      this.transferring = true
      this.updateViewOnly()
      try {
        const data = await this.request((config) => api.post(`/browsers/${this.integrationId}/upload-source`, {
          source: { scope: source.scope, path: source.path }
        }, config))
        if (!data || generation !== this.generation || viewGeneration !== this.viewGeneration || displayGeneration !== this.displayGeneration) return null
        this.uploadId = data.chosen ? null : data.uploadId
        return data
      } finally {
        if (generation === this.generation && viewGeneration === this.viewGeneration && displayGeneration === this.displayGeneration) {
          this.transferring = false
          this.updateViewOnly()
        }
      }
    },
    async uploadPickerFile(file) {
      if (!file || !this.manual || !this.pickerContext
        || (this.pickerScope === 'filesystem' && !this.pickerContext.filesystemImport)) return null
      const scope = this.pickerScope
      const directory = this.pickerDirectories[scope]
      const generation = this.generation
      const viewGeneration = this.viewGeneration
      const displayGeneration = this.displayGeneration
      const pickerGeneration = this.pickerGeneration
      this.transferring = true
      this.updateViewOnly()
      try {
        const form = new FormData()
        form.append('file', file, file.name)
        const uploaded = await this.request((config) => api.post('/workspace/files', form, {
          signal: config.signal,
          suppressErrorToast: true,
          params: this.pickerParams({ parent: directory }, false)
        }))
        if (!uploaded?.entry || generation !== this.generation || viewGeneration !== this.viewGeneration
          || displayGeneration !== this.displayGeneration || pickerGeneration !== this.pickerGeneration) return null
        const data = await this.request((config) => api.post(`/browsers/${this.integrationId}/upload-source`, {
          source: { scope, path: uploaded.entry.path }
        }, config))
        if (!data || generation !== this.generation || viewGeneration !== this.viewGeneration
          || displayGeneration !== this.displayGeneration || pickerGeneration !== this.pickerGeneration) return null
        this.pickerSelected = uploaded.entry
        this.pickerEntries = [...this.pickerEntries.filter((entry) => entry.path !== uploaded.entry.path), uploaded.entry]
        this.uploadId = data.chosen ? null : data.uploadId
        return data
      } finally {
        if (generation === this.generation && viewGeneration === this.viewGeneration && displayGeneration === this.displayGeneration) {
          this.transferring = false
          this.updateViewOnly()
        }
      }
    },
    async download(file) {
      if (this.transferring || !this.ownsDisplay || (file.status !== 'saved' && !this.manual)) return
      const generation = this.generation
      const viewGeneration = this.viewGeneration
      const displayGeneration = this.displayGeneration
      this.transferring = true
      this.updateViewOnly()
      try {
        const data = await this.request((config) => file.status === 'saved'
          ? api.get('/workspace/files/download', { ...config, responseType: 'blob',
            params: { scope: 'session', session_id: this.sessionId, path: file.path } })
          : api.get(`/browsers/${this.integrationId}/download/${encodeURIComponent(file.id)}`, {
            ...config, responseType: 'blob'
          }))
        if (!data || generation !== this.generation || viewGeneration !== this.viewGeneration || displayGeneration !== this.displayGeneration) return
        const url = URL.createObjectURL(data)
        const link = document.createElement('a')
        link.href = url
        link.download = file.filename
        link.click()
        setTimeout(() => URL.revokeObjectURL(url), 1000)
      } finally {
        if (generation === this.generation && viewGeneration === this.viewGeneration && displayGeneration === this.displayGeneration) {
          this.transferring = false
          this.updateViewOnly()
        }
      }
    },
    syncContext({ sessionId, agentId, available, tabPresent }) {
      if (!this.sessionId) return
      if (sessionId === this.sessionId && (this.agentId !== agentId || !tabPresent)) {
        this.release()
        this.activationAttempted = false
      } else if (!available) {
        this.reconnectEligible = false
        this.reconnect('control')
      }
    },
    release({ close = false, stop = false } = {}) {
      if (close && !this.ownsDisplay) return
      const retained = { displayedSessionId: this.displayedSessionId, displayGeneration: this.displayGeneration + 1 }
      const socket = this.socket
      const generation = this.generation + 1
      const { activationKey, activationAttempted } = this
      const stoppedContext = close || stop ? JSON.stringify([this.integrationId, this.sessionId, this.agentId]) : this.stoppedContext
      this.generation = generation
      this.clearView()
      const viewGeneration = this.viewGeneration
      clearTimeout(this.resizeTimer)
      clearTimeout(this.reconnectTimer)
      this.$reset()
      Object.assign(this, retained, { activationKey, activationAttempted: close || activationAttempted, stoppedContext })
      this.generation = generation
      this.viewGeneration = viewGeneration
      if (socket?.readyState === 1) {
        if (close) socket.send(JSON.stringify({ type: 'close' }))
        socket.send(JSON.stringify({ type: 'release' }))
      }
      socket?.close()
    }
  }
})
