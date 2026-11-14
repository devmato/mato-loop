// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createVoiceClient } from '../src/features/voice/client'
import VoicePanel from '../src/features/voice/VoicePanel.vue'
import { useChatStore } from '../src/features/chat/store'
import { useVoiceStore } from '../src/features/voice/store'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }))
vi.mock('src/boot/axios', () => ({ api }))
vi.mock('quasar', () => ({ useQuasar: () => ({ screen: { lt: { sm: false } } }) }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm: async () => true }) }))

let track, stream, peer, socket, audio, negotiate, onError, onWakeLock, wakeLock, client

beforeEach(() => {
  vi.useFakeTimers()
  track = { enabled: true, stop: vi.fn() }
  stream = { getTracks: () => [track], getAudioTracks: () => [track] }
  audio = { setAttribute: vi.fn(), pause: vi.fn(), play: vi.fn().mockResolvedValue(), srcObject: null }
  vi.stubGlobal('Audio', vi.fn(function () { return audio }))
  vi.stubGlobal('isSecureContext', true)
  wakeLock = Object.assign(new EventTarget(), { released: false, release: vi.fn().mockResolvedValue() })
  vi.stubGlobal('navigator', {
    mediaDevices: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    wakeLock: { request: vi.fn().mockResolvedValue(wakeLock) }
  })
  vi.stubGlobal('RTCPeerConnection', class {
    constructor() {
      peer = this
      this.connectionState = 'new'
      this.close = vi.fn()
      this.addTrack = vi.fn()
      this.createDataChannel = () => ({ close: vi.fn() })
      this.createOffer = async () => ({ sdp: 'v=0\r\n' })
      this.setLocalDescription = async () => {}
      this.setRemoteDescription = async () => {}
    }
  })
  vi.stubGlobal('WebSocket', class {
    static OPEN = 1
    constructor() {
      socket = this
      this.readyState = 1
      this.send = vi.fn()
      this.close = vi.fn()
    }
  })
  negotiate = vi.fn().mockResolvedValue({ connectionId: 'connection', sdpAnswer: 'v=0\r\nanswer' })
  onError = vi.fn()
  onWakeLock = vi.fn()
  client = createVoiceClient({ negotiate, onPhase: vi.fn(), onControl: vi.fn(), onError, onClose: vi.fn(), onWakeLock })
})

afterEach(() => {
  client.close()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('Realtime microphone and media lifecycle', () => {
  it.each([false, true])('starts voice directly with the server default without fetching models (new chat=%s)', async (newChat) => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const chat = useChatStore(), voice = useVoiceStore()
    chat.current = newChat ? null : { id: 'chat', messages: [] }
    vi.spyOn(chat, 'createEmptySession').mockImplementation(async () => {
      chat.current = { id: 'chat', messages: [] }
      return chat.current
    })
    vi.spyOn(chat, 'refreshSession').mockResolvedValue()
    api.get.mockResolvedValue({ data: { tasks: [] } })
    api.post.mockResolvedValue({ data: { connectionId: 'connection', sdpAnswer: 'v=0\r\nanswer' } })
    const root = document.createElement('div')
    const app = createApp(VoicePanel)
    app.use(pinia)
    for (const name of ['QBanner', 'QCard', 'QCardSection', 'QBadge', 'QList', 'QItem', 'QItemSection', 'QItemLabel']) {
      app.component(name, { render() { return h('div', this.$slots.default?.()) } })
    }
    for (const name of ['QIcon', 'QSpinner', 'QBtn']) app.component(name, { render: () => null })
    try {
      const panel = app.mount(root)
      await nextTick()
      api.get.mockClear()
      await panel.start()
      expect(api.get).not.toHaveBeenCalled()
      expect(api.post).toHaveBeenCalledWith(
        '/sessions/chat/voice', { sdp_offer: 'v=0\r\n' }, { signal: expect.any(AbortSignal) }
      )
    } finally { await voice.end(); app.unmount() }
  })

  it('clears previous chat tasks before starting a new voice session', async () => {
    setActivePinia(createPinia())
    const voice = useVoiceStore()
    voice.$patch({ sessionId: 'chat-a', tasks: [{ taskId: 'task-a', sessionId: 'chat-a', runId: 'run-a', active: true }] })
    api.post.mockResolvedValue({ data: { connectionId: 'connection', sdpAnswer: 'v=0\r\nanswer' } })
    try {
      await voice.start('chat-b')
      expect(voice.tasks).toEqual([])
    } finally { await voice.end() }
  })

  it('keeps task approvals here and reloads target chat tasks after ending voice on navigation', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useChatStore().current = { id: 'chat', messages: [] }
    const voice = useVoiceStore()
    voice.$patch({
      sessionId: 'chat', phase: 'listening', wakeLockActive: false,
      tasks: [
        { taskId: 'task', sessionId: 'chat', runId: 'run', status: 'waiting_approval', active: true, requiresUi: true },
        { taskId: 'completed', sessionId: 'chat', status: 'completed', active: false }
      ]
    })
    api.post.mockResolvedValue({})
    const root = document.createElement('div')
    const app = createApp(VoicePanel)
    app.use(pinia)
    for (const name of ['QBanner', 'QCard', 'QCardSection', 'QBadge', 'QList', 'QItem', 'QItemSection', 'QItemLabel']) {
      app.component(name, { render() { return h('div', this.$slots.default?.()) } })
    }
    app.component('QIcon', { render: () => null })
    app.component('QSpinner', { render: () => null })
    app.component('QDialog', { render: () => null })
    app.component('QBtn', { props: ['label'], render() { return h('button', this.label) } })
    try {
      const panel = app.mount(root)
      expect(root.textContent).toContain('Beantworte die Freigabe oder Rückfrage hier im Chat.')
      await vi.advanceTimersByTimeAsync(5000)
      expect(root.textContent).not.toContain('Sprachauftrag · Abgeschlossen')
      const buttons = [...root.querySelectorAll('button')]
      expect(buttons.some((button) => button.textContent === 'Öffnen')).toBe(false)
      buttons.find((button) => button.textContent === 'Stoppen').click()
      await nextTick()
      expect(api.post).toHaveBeenCalledWith('/sessions/chat/runs/run/cancel')
      let finishEnd
      api.delete.mockReturnValue(new Promise((resolve) => { finishEnd = resolve }))
      api.get.mockResolvedValue({ data: { tasks: [
        { taskId: 'next-task', sessionId: 'next-chat', runId: 'next-run', status: 'running', active: true }
      ] } })
      useChatStore().current = { id: 'next-chat', messages: [] }
      await nextTick()
      finishEnd({})
      await vi.waitFor(() => expect(panel.canStartWhileRunning).toBe(true))
    } finally { app.unmount() }
  })

  it('keeps the microphone disabled until both WebRTC and backend initialization are ready', async () => {
    await client.start('session')
    expect(navigator.wakeLock.request).toHaveBeenCalledWith('screen')
    expect(onWakeLock).toHaveBeenLastCalledWith(true)
    expect(track.enabled).toBe(false)
    peer.connectionState = 'connected'
    peer.onconnectionstatechange()
    expect(track.enabled).toBe(false)
    socket.onopen()
    socket.onmessage({ data: JSON.stringify({ phase: 'listening' }) })
    expect(track.enabled).toBe(true)
    client.mute(true)
    expect(track.enabled).toBe(false)
    client.close()
    expect(track.stop).toHaveBeenCalledOnce()
    expect(socket.close).toHaveBeenCalledOnce()
    expect(peer.close).toHaveBeenCalledOnce()
    expect(wakeLock.release).toHaveBeenCalledOnce()
  })

  it('mutes the active microphone across Pinia action proxies and control updates', async () => {
    setActivePinia(createPinia())
    const voice = useVoiceStore()
    api.post.mockResolvedValue({ data: { connectionId: 'connection', sdpAnswer: 'v=0\r\nanswer' } })
    try {
      await voice.start.call(new Proxy(voice, {}), 'chat')
      peer.connectionState = 'connected'
      peer.onconnectionstatechange()
      socket.onmessage({ data: JSON.stringify({ phase: 'listening' }) })
      voice.toggleMute()
      socket.onmessage({ data: JSON.stringify({ phase: 'speaking' }) })
      expect(track.enabled).toBe(false)
    } finally { await voice.end() }
  })

  it.each(['unsupported', 'denied', 'released'])('keeps voice usable and reports an %s screen wake lock', async (state) => {
    if (state === 'unsupported') delete navigator.wakeLock
    if (state === 'denied') navigator.wakeLock.request.mockRejectedValue(new DOMException('Denied', 'NotAllowedError'))
    await client.start('session')
    if (state === 'released') wakeLock.dispatchEvent(new Event('release'))
    peer.connectionState = 'connected'
    peer.onconnectionstatechange()
    socket.onmessage({ data: JSON.stringify({ phase: 'listening' }) })
    expect(onWakeLock).toHaveBeenLastCalledWith(false)
    expect(track.enabled).toBe(true)
    expect(onError).not.toHaveBeenCalled()
  })

  it('releases a screen wake lock granted after the conversation ends', async () => {
    let resolve
    navigator.wakeLock.request.mockReturnValue(new Promise((done) => { resolve = done }))
    await client.start('session')
    client.close()
    resolve(wakeLock)
    await Promise.resolve()
    expect(wakeLock.release).toHaveBeenCalledOnce()
    expect(onWakeLock).not.toHaveBeenCalled()
  })

  it('releases a microphone permission result arriving after the conversation was closed', async () => {
    let resolve
    navigator.mediaDevices.getUserMedia.mockReturnValue(new Promise((done) => { resolve = done }))
    const pending = client.start('session')
    client.close()
    resolve(stream)
    await pending
    expect(track.stop).toHaveBeenCalledOnce()
    expect(negotiate).not.toHaveBeenCalled()
  })

  it('stops local audio on control loss or playback denial rather than reconnecting silently', async () => {
    await client.start('session')
    audio.play.mockRejectedValue(new Error('autoplay blocked'))
    peer.ontrack({ streams: [stream], track })
    await Promise.resolve()
    expect(onError).toHaveBeenCalledWith('voice.playbackFailed')
    expect(track.stop).toHaveBeenCalledOnce()
    expect(audio.srcObject).toBeNull()
  })
})
