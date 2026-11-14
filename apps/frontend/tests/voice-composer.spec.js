// @vitest-environment jsdom
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ notify: vi.fn(), copyToClipboard: vi.fn(), api: { post: vi.fn(), get: vi.fn(), patch: vi.fn(), delete: vi.fn() }, recorder: null, mobile: false, resetMessageSizes: vi.fn() }))
vi.mock('src/boot/axios', () => ({ api: mocks.api }))
vi.mock('quasar', () => ({ copyToClipboard: mocks.copyToClipboard, useQuasar: () => ({ notify: mocks.notify, platform: { is: { mobile: mocks.mobile } }, screen: { gt: { xs: !mocks.mobile, sm: !mocks.mobile }, lt: { sm: mocks.mobile } } }) }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm: async () => true }) }))
vi.mock('src/features/chat/audio-recorder', () => ({
  useAudioRecorder: (options) => { mocks.onAutoStopped = options.onAutoStopped; return mocks.recorder },
  recorderErrorMessage: () => 'Aufnahme fehlgeschlagen'
}))
vi.mock('src/features/chat/components/SessionSettingsFields.vue', () => ({ default: {
  props: ['disabled'],
  emits: ['update', 'agent-popup-hide'],
  render() {
    return h('div', [this.$slots.before?.(), h('button', {
      type: 'button',
      onClick: () => {
        this.$emit('update', { agent_profile_id: 'agent-b' })
        this.$emit('agent-popup-hide')
      }
    }, 'Agent wechseln')])
  }
} }))
vi.mock('src/features/approvals/ApprovalCard.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/chat/components/AgentActivityPanel.vue', () => ({ default: {
  inheritAttrs: false,
  props: ['subagents', 'showActiveSubagents'],
  render() {
    return h('div', {
      'data-subagents': this.subagents?.map((child) => child.id).join(','),
      'data-show-active-subagents': this.showActiveSubagents
    }, this.$slots.default?.())
  }
} }))
vi.mock('src/shared/ui/AppLogo.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/home/HomeContent.vue', () => ({ default: { render: () => h('section', { class: 'home-stub' }, 'Übersicht') } }))
vi.mock('src/features/chat/components/ComputerChoiceStrip.vue', () => ({ default: { render: () => null } }))
vi.mock('src/app/control-center/ControlCenterPanel.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/chat/components/MessageContent.vue', () => ({ default: {
  props: ['content'], render() { return h('div', this.content) }
} }))
vi.mock('src/features/chat/components/ChatFiles.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/questions/QuestionDock.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/subagents/SubagentInspector.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/voice/VoicePanel.vue', () => ({ default: { render: () => null } }))

import ChatComposer from '../src/features/chat/components/ChatComposer.vue'
import { quotedMessage } from '../src/features/chat/quote-drafts'
import ChatPage from '../src/features/chat/ChatPage.vue'
import { useChatStore } from '../src/features/chat/store'
import { useAgentProfileStore } from '../src/stores/agent-profiles'
import { useProviderStore } from '../src/stores/providers'
import { useToolSettingsStore } from '../src/stores/tool-settings'
import { useInstanceLimitsStore } from '../src/stores/instance-limits'
import { useQuestionStore } from '../src/stores/questions'
import { useControlCenterStore } from '../src/stores/control-center'

let app, root, chat, session, stopResolve, showPage, router, sentMessages, stoppedCount, composer
const file = (name = 'aufnahme.wav') => new File(['wav'], name, { type: 'audio/wav' })

async function click(label) {
  const button = [...root.querySelectorAll('button')].find((item) => (
    item.getAttribute('aria-label') === label || item.textContent === label
  ))
  expect(button, label).toBeTruthy()
  button.click()
  await nextTick()
}

beforeEach(async () => {
  mocks.mobile = false
  mocks.scrollTarget = null
  setActivePinia(createPinia())
  chat = useChatStore()
  sentMessages = []
  stoppedCount = 0
  chat.draftOwnerUserId = 'owner'
  chat.current = { id: 'session-1' }
  session = ref(chat.current)
  composer = ref(null)
  const providers = useProviderStore()
  providers.audioTranscriptionModelId = 'audio'
  vi.spyOn(providers, 'capabilityOptions', 'get').mockReturnValue(() => [{ value: 'audio' }])
  useToolSettingsStore().loaded = true
  mocks.recorder = {
    starting: ref(false), recording: ref(false), finalizing: ref(false),
    durationMs: ref(1000), limitReached: ref(false), autoStoppedFile: ref(null),
    startRecording: vi.fn(async () => { mocks.recorder.recording.value = true }),
    stopRecording: vi.fn(() => {
      mocks.recorder.recording.value = false
      mocks.recorder.finalizing.value = true
      return new Promise((resolve) => {
        stopResolve = (value) => { mocks.recorder.finalizing.value = false; resolve(value) }
      })
    }),
    cancelRecording: vi.fn(async () => {
      mocks.recorder.recording.value = false
      mocks.recorder.starting.value = false
    }),
    clearAutoStoppedFile: () => { mocks.recorder.autoStoppedFile.value = null }
  }
  vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:audio'), revokeObjectURL: vi.fn() })
  root = document.createElement('div')
  document.body.append(root)
  showPage = ref(false)
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'chat', component: ChatPage }] })
  await router.push('/?session=session-1')
  app = createApp({
    render: () => showPage.value ? h(ChatPage) : h(ChatComposer, {
      ref: composer,
      session: session.value,
      draft: chat.activeDraft,
      running: chat.running,
      queueMode: chat.messageQueueActive,
      attachments: chat.activeAttachmentDraft,
      onUpdate: (patch) => chat.updateActive(patch),
      'onUpdate:draft': (value) => chat.setDraft(value),
      onSend: (content, options) => sentMessages.push({ content, options }),
      onStop: () => { stoppedCount += 1 },
      'onAdd-attachments': (files, done, options) => {
        const result = chat.addAttachmentFiles(files, options)
        done?.(result)
      },
      'onRemove-attachment': (id) => chat.removeAttachmentFile(id)
    })
  })
  app.use(router)
  app.config.globalProperties.$q = { screen: { lt: { get sm() { return mocks.mobile } }, gt: { get xs() { return !mocks.mobile } } } }
  app.component('QBtn', defineComponent({
    props: ['label', 'icon', 'disable', 'loading'],
    setup: (props, { slots }) => () => h('button', {
      'data-icon': props.icon,
      disabled: props.disable || props.loading
    }, props.label || (props.icon === 'arrow_drop_down' ? slots.default?.() : null))
  }))
  app.component('QItem', defineComponent({
    emits: ['click'],
    setup: (_, { emit, slots }) => () => h('button', { onClick: () => emit('click') }, slots.default?.())
  }))
  for (const name of ['QList', 'QItemSection', 'QItemLabel']) {
    app.component(name, { render() { return h('div', this.$slots.default?.()) } })
  }
  app.component('QMenu', defineComponent({
    methods: { updatePosition() {} },
    emits: ['hide'],
    setup: (_, { emit, slots }) => () => h('div', { onClick: () => emit('hide') }, slots.default?.())
  }))
  app.component('QInput', {
    props: ['disable', 'modelValue'],
    emits: ['update:modelValue'],
    methods: { focus() { this.$el.focus() } },
    render() { return h('textarea', { disabled: this.disable, value: this.modelValue, onInput: event => this.$emit('update:modelValue', event.target.value) }) }
  })
  app.component('QSelect', {
    props: ['modelValue', 'options', 'disable'], emits: ['update:modelValue'],
    render() { return h('select', { value: this.modelValue ?? '', disabled: this.disable, onChange: event => this.$emit('update:modelValue', event.target.value || null) }, this.options.map(option => h('option', { value: option.value ?? '' }, option.label))) }
  })
  for (const name of ['QCard', 'QCardActions', 'QForm', 'QIcon', 'QSpace', 'QBanner', 'QTooltip', 'QSpinner', 'QChip', 'QPopupProxy', 'QToolbar', 'QToolbarTitle', 'QSeparator', 'QCardSection']) {
    app.component(name, { methods: { focus() {} }, render() { return h('div', this.$slots.default?.()) } })
  }
  app.directive('close-popup', {})
  app.component('QPage', { render() { return h('div', this.$slots.default?.()) } })
  app.component('QSplitter', { render() { return h('div', this.$slots.before?.()) } })
  app.component('QScrollArea', { methods: { getScrollTarget() { return mocks.scrollTarget } }, render() { return h('div', this.$slots.default?.()) } })
  app.component('QVirtualScroll', {
    props: ['items'],
    emits: ['virtual-scroll'],
    methods: { reset: mocks.resetMessageSizes, refresh() { this.$emit('virtual-scroll', { from: 0, direction: 'decrease' }) }, scrollTo() {} },
    render() { return h('div', this.items.map((item) => this.$slots.default?.({ item }))) }
  })
  app.component('QSkeleton', { render: () => null })
  app.component('QResizeObserver', { render: () => null })
  app.component('QDialog', { render: () => null })
  app.mount(root)
})

afterEach(() => {
  app?.unmount()
  chat.cancelQueueDrain()
  chat.clearAllAttachmentDrafts()
  root?.remove()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('voice attachment composer', () => {
  it('focuses the message input when pressing the card padding', () => {
    root.querySelector('.chat-composer__card').dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(root.querySelector('.chat-composer__input'))
  })

  it('shows the message hint only in an entirely empty composer', async () => {
    const initial = root.querySelector('.chat-composer__input').getAttribute('placeholder')
    await composer.value.insertQuote({ text: 'Selected answer', comment: '' })
    expect([initial, [...root.querySelectorAll('.chat-composer__input')].map(field => field.getAttribute('placeholder'))])
      .toEqual(['Eine Nachricht an MatoLoop senden', [null]])
  })

  it('removes a quote without changing the remaining quote or the message being sent', async () => {
    const remaining = { type: 'quote', text: 'Keep this', comment: 'My comment' }
    chat.setDraft(quotedMessage([{ type: 'quote', text: 'Remove this', comment: '' }, remaining, { type: 'text', text: 'My message' }]))
    await nextTick()
    await click('Zitat entfernen')
    const input = root.querySelector('.chat-composer__input')
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect([input.value, sentMessages])
      .toEqual(['My message', [{ content: quotedMessage([{ type: 'text', text: 'My message' }, remaining]), options: { interrupt: false } }]])
  })

  it('merges older draft text into one input and shows quotes in insertion order before attachments', async () => {
    const existing = { type: 'quote', text: 'Existing answer', comment: 'Keep this comment' }
    chat.setDraft(quotedMessage([{ type: 'text', text: 'Before' }, existing, { type: 'text', text: 'After' }]))
    chat.addAttachmentFiles([new File(['notes'], 'notes.txt', { type: 'text/plain' })])
    await nextTick()
    const input = root.querySelector('.chat-composer__input')
    await composer.value.insertQuote({ text: 'Selected answer', comment: 'Why?' })
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    const previews = [...root.querySelectorAll('.composer-attachments .composer-quote span.ellipsis, .composer-attachments strong')].map(element => element.textContent)
    expect([root.querySelectorAll('.chat-composer__input').length, input.value, previews, sentMessages[0]?.content])
      .toEqual([1, 'Before\n\nAfter', ['Existing answer', 'Selected answer', 'notes.txt'], quotedMessage([{ type: 'text', text: 'Before\n\nAfter' }, existing, { type: 'quote', text: 'Selected answer', comment: 'Why?' }])])
  })

  it('copies only the complete assistant answer, including Markdown', async () => {
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await router.push('/?project=draft')
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.home-stub')).not.toBeNull())
    const content = '**Answer**\n\n```js\nconst value = 1\n```'
    chat.current = { id: 'session-1', messages: [{ id: 'answer', role: 'assistant', content }] }
    await nextTick()
    await click('Antwort kopieren')
    expect(mocks.copyToClipboard).toHaveBeenCalledWith(content)
  })

  it('keeps the composer available while a question is pending', async () => {
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await router.push('/?project=draft')
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.home-stub')).not.toBeNull())
    chat.current = { id: 'session-1', messages: [] }
    useQuestionStore().add({ id: 'question-1', sessionId: 'session-1', questions: [] })
    await nextTick()

    expect(root.querySelector('.chat-composer__input')).not.toBeNull()
  })

  it('keeps the file dropzone active across child elements and adds dropped files to the draft', async () => {
    const files = [file('notiz.txt'), file('bild.png')]
    const transfer = { types: ['Files'], files, dropEffect: 'none' }
    const drag = (target, type) => {
      const event = new Event(type, { bubbles: true, cancelable: true })
      Object.defineProperty(event, 'dataTransfer', { value: transfer })
      target.dispatchEvent(event)
      return event
    }
    const card = root.querySelector('.chat-composer__card')
    const input = root.querySelector('textarea')
    drag(card, 'dragenter')
    drag(input, 'dragenter')
    drag(card, 'dragleave')
    await nextTick()
    expect(root.querySelector('[role="status"]')?.textContent).toContain('Dateien hier ablegen')
    expect(drag(input, 'dragover').defaultPrevented).toBe(true)
    expect(transfer.dropEffect).toBe('copy')
    expect(drag(input, 'drop').defaultPrevented).toBe(true)
    await nextTick()
    expect(chat.activeAttachmentDraft.map(attachment => attachment.file)).toEqual(files)
    expect(root.querySelector('.chat-composer__dropzone')).toBeNull()
    expect(sentMessages).toEqual([])
  })

  it.each(['unavailable', 'recording'])('does not attach dropped files while %s', async (state) => {
    if (state === 'unavailable') session.value = null
    else mocks.recorder.recording.value = true
    await nextTick()
    const card = root.querySelector('.chat-composer__card')
    for (const type of ['dragenter', 'dragover', 'drop']) {
      const event = new Event(type, { bubbles: true, cancelable: true })
      Object.defineProperty(event, 'dataTransfer', { value: { types: ['Files'], files: [file()] } })
      card.dispatchEvent(event)
      expect(event.defaultPrevented).toBe(true)
    }
    await nextTick()
    expect(chat.activeAttachmentDraft).toEqual([])
    expect(root.querySelector('.chat-composer__dropzone')).toBeNull()
  })

  it('reveals a switched session only after its transcript has been positioned', async () => {
    mocks.api.get.mockImplementation(async url => ({ data: url === '/sessions/session-1'
      ? { id: 'session-1', messages: [{ id: 'answer', role: 'assistant', content: 'Answer' }] }
      : { items: [] } }))
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.chat-transcript:not(.invisible) [data-message-id="answer"]')).not.toBeNull())
    chat.current = { id: 'session-2', messages: [{ id: 'next', role: 'user', content: 'Next' }] }
    await nextTick()
    const preparing = root.querySelector('.chat-transcript').classList.contains('invisible')
    await vi.waitFor(() => expect(root.querySelector('.chat-transcript:not(.invisible) [data-message-id="next"]')).not.toBeNull())
    expect(preparing).toBe(true)
  })

  it('keeps the final touch-scroll position when a transcript update was deferred during the gesture', async () => {
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await router.push('/?project=draft')
    const target = document.createElement('div')
    Object.defineProperties(target, {
      clientHeight: { value: 400 },
      scrollHeight: { value: 1400 }
    })
    target.getBoundingClientRect = () => ({ top: 0, bottom: 400 })
    mocks.scrollTarget = target
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.home-stub')).not.toBeNull())
    vi.useFakeTimers()
    chat.current = { id: 'session-1', messages: [{ id: 'message', role: 'user', content: 'Text' }] }
    await nextTick()
    await vi.advanceTimersByTimeAsync(32)
    root.querySelector('[data-message-id="message"]').getBoundingClientRect = () => ({
      top: 100 - target.scrollTop, bottom: 1100 - target.scrollTop
    })
    target.dispatchEvent(new Event('scroll'))
    target.dispatchEvent(new Event('touchstart'))
    target.dispatchEvent(new Event('touchmove'))
    target.scrollTop = 600
    target.dispatchEvent(new Event('scroll'))
    chat.current.messages.push({ id: 'reply', role: 'assistant', content: 'Antwort' })
    await nextTick()
    target.dispatchEvent(new Event('touchend'))
    await vi.advanceTimersByTimeAsync(100)
    target.scrollTop = 300
    target.dispatchEvent(new Event('scroll'))
    await vi.advanceTimersByTimeAsync(250)

    expect(target.scrollTop).toBe(300)
  })

  it('reveals a search result merged into the final assistant message', async () => {
    const focus = HTMLElement.prototype.focus
    const focusSpy = vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function (options) {
      if (!this.closest('[inert="true"], [inert=""]')) focus.call(this, options)
    })
    chat.outboxLoaded = true
    mocks.api.get.mockImplementation(async (url) => ({ data: url === '/sessions/session-1'
      ? { id: 'session-1', messages: [
          { id: 'user', role: 'user', content: 'Start' },
          { id: 'source', role: 'assistant', content: 'Unique plan text' },
          { id: 'final', role: 'assistant', content: 'Done' }
        ] }
      : url.endsWith('/children') ? [] : { items: [] } }))
    await router.push('/?session=session-1&message=source')
    showPage.value = true
    try {
      await vi.waitFor(() => expect(document.activeElement).toBe(root.querySelector('[data-message-id="final"].chat-message--search-target')))
    } finally { focusSpy.mockRestore() }
  })

  it('shows the saved command prompt instead of its invocation in the user message', async () => {
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await router.push('/?project=draft')
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.home-stub')).not.toBeNull())
    const prompt = 'Review the working changes.\n\nOnly parser changes.'
    chat.current = { id: 'session-1', messages: [{ id: 'command', role: 'user', content: '/git-review only parser changes', commandPrompt: prompt }] }
    await nextTick()
    expect(root.querySelector('[data-message-id="command"] .chat-message__content')?.textContent).toBe(prompt)
  })
  it.each([false, true])('selects a command with the keyboard, keeps attached quotes (%s), and sends it after arguments are entered', async withQuote => {
    const quotes = withQuote ? [{ type: 'quote', text: 'Selected answer', comment: '' }] : []
    mocks.api.post.mockResolvedValue({ data: { items: [
      { name: 'example-one', description: 'Erster Auftrag', source: 'example' },
      { name: 'example-two', description: 'Zweiter Auftrag', source: 'example' }
    ] } })
    chat.setDraft(quotedMessage([{ type: 'text', text: '/' }, ...quotes]))
    await vi.waitFor(() => expect(root.querySelectorAll('[role="option"]')).toHaveLength(2))
    const input = root.querySelector('textarea')
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await nextTick()
    expect([chat.activeDraft, sentMessages]).toEqual([quotedMessage([{ type: 'text', text: '/example-two ' }, ...quotes]), []])
    input.value = '/example-two additional arguments'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    expect(sentMessages).toEqual([{ content: quotedMessage([{ type: 'text', text: '/example-two additional arguments' }, ...quotes]), options: { interrupt: false } }])
  })
  it('invalidates message heights and remounts the composer input when the session message size changes', async () => {
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await router.push('/?project=draft')
    showPage.value = true
    await nextTick()
    chat.current = { id: 'session-1', sessionProfile: { messageSize: 'standard' }, messages: [{ id: 'message', role: 'user', content: 'Text' }] }
    await nextTick()
    mocks.resetMessageSizes.mockClear()
    const previousInput = root.querySelector('.chat-composer__input')

    chat.current.sessionProfile.messageSize = 'small'

    await vi.waitFor(() => expect(mocks.resetMessageSizes).toHaveBeenCalled())
    expect(root.querySelector('.chat-composer__input--small')).not.toBeNull()
    expect(previousInput.isConnected).toBe(false)
  })

  it('shows the first outgoing message in the transcript with its delivery state', async () => {
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await router.push('/?project=draft')
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.home-stub')).not.toBeNull())
    const entry = { id: 'pending', content: 'Erste Nachricht', attachments: [], paused: false }
    chat.queuedMessages[chat.activeQueueKey] = [entry]
    const states = []
    for (const status of ['queued', 'sending', 'failed']) {
      chat.queueSendingEntryId = status === 'sending' ? entry.id : null
      chat.activeQueuedMessages[0].paused = status === 'failed'
      await nextTick()
      const message = root.querySelector('.chat-transcript .chat-message--user')
      states.push([
        message?.querySelector('.chat-message__content')?.textContent,
        message?.querySelector('[role="status"]')?.textContent,
        message?.parentElement.classList.contains('chat-transcript__item--sending')
      ])
    }
    expect(states).toEqual([
      ['Erste Nachricht', 'Noch nicht gesendet', false],
      ['Erste Nachricht', 'Wird gesendet …', true],
      ['Erste Nachricht', 'Nicht gesendet', false]
    ])
    const pendingRow = root.querySelector('.chat-message--user')
    chat.activeQueuedMessages[0].sentMessageId = 'confirmed'
    chat.current = { id: 'created', messages: [{ id: 'confirmed', renderId: entry.id, role: 'user', content: entry.content }] }
    chat.selectedSessionId = 'created'
    chat.queuedMessages = {}
    await router.replace('/?session=created')
    await nextTick()
    expect(root.querySelector('[data-message-id="confirmed"]')).toBe(pendingRow)
  })

  it('renders host failures as message content with a red metadata retry action', async () => {
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await router.push('/?project=draft')
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.home-stub')).not.toBeNull())
    const failure = { id: 'failure', role: 'assistant', origin: 'host', kind: 'runtime_error',
      state: 'error', content: 'The agent run failed.', error: { code: 'agent.failed' }, retryAvailable: true }
    chat.current = { id: 'session-1', messages: [failure] }
    const retry = vi.spyOn(chat, 'retryFailedMessage').mockResolvedValue(null)
    await nextTick()
    await click('Anfrage erneut starten')
    const message = root.querySelector('[data-message-id="failure"]')
    expect([
      message.querySelector('.chat-message__content')?.textContent,
      message.querySelector('.chat-message__content')?.getAttribute('role'),
      message.querySelector('.chat-message__meta [aria-label="Anfrage erneut starten"]')?.getAttribute('color'),
      retry.mock.calls[0]?.[0]?.id
    ]).toEqual(['Die Agent-Anfrage ist fehlgeschlagen.', 'alert', 'negative', 'failure'])
  })

  it('keeps the previous assistant reply unchanged while submitting the next message', async () => {
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await router.push('/?project=draft')
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.home-stub')).not.toBeNull())
    chat.current = { id: 'created', messages: [
      { id: 'first', role: 'user', content: 'Frage' },
      { id: 'answer', role: 'assistant', content: 'Antwort', activeDurationMs: 1000 }
    ] }
    await nextTick()
    const previousReply = root.querySelector('[data-message-id="answer"]')
    chat.running = true
    chat.setActivity('submitting')
    await nextTick()
    const labelDuringSend = previousReply.querySelector('.chat-message__meta-agent')?.textContent
    const repliesDuringSend = root.querySelectorAll('.chat-message--assistant').length
    chat.current.messages.push({ id: 'second', role: 'user', content: 'Nächste Frage' })
    chat.setActivity('waiting_model')
    await nextTick()
    expect([root.querySelector('[data-message-id="answer"]') === previousReply, labelDuringSend, repliesDuringSend])
      .toEqual([true, 'Allgemein', 1])
    expect(root.querySelectorAll('.chat-message--assistant')).toHaveLength(2)
  })

  it('keeps sent-message undo and queue recovery visible after local cleanup fails', async () => {
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await router.push('/?project=draft')
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.home-stub')).not.toBeNull())
    const message = { id: 'sent', role: 'user', content: 'Angenommen', createdAt: new Date().toISOString() }
    chat.current = { id: 'session-1', messages: [message] }
    chat.queuedMessages['session-1'] = [{ id: 'pending', sentMessageId: 'sent', paused: true, attachments: [] }]
    const undo = vi.spyOn(chat, 'undoUserMessage').mockResolvedValue(null)
    await nextTick()
    await click('Nachricht rückgängig machen')
    expect([Boolean(root.querySelector('[aria-label="Versand fortsetzen"]')), undo.mock.calls[0]?.[0]?.id])
      .toEqual([true, 'sent'])
  })

  it.each([false, true])('keeps the project draft composer through chat transitions (mobile=%s)', async (mobile) => {
    mocks.mobile = mobile
    chat.outboxLoaded = true
    mocks.api.get.mockImplementation(async (url) => ({ data: url.startsWith('/sessions/session-')
      ? { id: url.split('/').pop(), messages: [] } : { items: [] } }))
    await router.push('/?project=draft')
    showPage.value = true
    await vi.waitFor(() => expect(root.querySelector('.chat-start-project')).not.toBeNull())
    expect(root.querySelector('.home-stub')).not.toBeNull()
    chat.setDraft('Entwurf für Home')
    await chat.addAttachmentFiles([file('entwurf.wav')])
    await nextTick()
    const input = root.querySelector('.chat-input-dock textarea')
    expect(document.activeElement).not.toBe(input)
    expect(mocks.api.post).not.toHaveBeenCalledWith('/sessions', expect.anything())

    await router.push('/?new=1')
    await vi.waitFor(() => expect(root.querySelector('h1')?.textContent).toBe('Neue Session'))
    expect(root.querySelector('.home-stub')).toBeNull()
    expect(root.querySelector('.chat-input-dock textarea')).toBe(input)
    expect(input.value).toBe('Entwurf für Home')
    await router.push('/?project=draft')
    await vi.waitFor(() => expect(root.querySelector('.chat-start-project')).not.toBeNull())

    await router.push('/?session=session-2')
    await vi.waitFor(() => expect(chat.current?.id).toBe('session-2'))
    expect(root.querySelector('.chat-start-project')).toBeNull()
    await router.push('/?project=draft')
    await vi.waitFor(() => expect(root.querySelector('.chat-input-dock textarea')?.value).toBe('Entwurf für Home'))
    expect(chat.activeAttachmentDraft[0].file.name).toBe('entwurf.wav')
  })

  it('removes a draft project with its content only after navigation succeeds', async () => {
    chat.outboxLoaded = true
    mocks.api.get.mockResolvedValue({ data: { items: [] } })
    await chat.setSessionScope('project-1')
    await router.push('/?new=1&project=project-1')
    showPage.value = true
    await vi.waitFor(() => expect(chat.current).toBeNull())
    chat.setDraft('Entwurf mit Anhang')
    chat.addAttachmentFiles([file('mitnehmen.wav')])
    await nextTick()
    const guard = vi.fn(() => false)
    const unblock = router.beforeEach(guard)
    const select = root.querySelector('#draft-project-select')
    select.value = ''
    select.dispatchEvent(new Event('change'))
    await vi.waitFor(() => expect(guard).toHaveBeenCalled())
    await vi.waitFor(() => expect(chat.sessionSettingsPending).toBe(0))
    expect([router.currentRoute.value.query.project, chat.activeDraft, chat.activeAttachmentDraft[0].file.name])
      .toEqual(['project-1', 'Entwurf mit Anhang', 'mitnehmen.wav'])
    unblock()
    select.value = ''
    select.dispatchEvent(new Event('change'))
    await vi.waitFor(() => expect(router.currentRoute.value.query).toEqual({ new: '1' }))
    await vi.waitFor(() => expect(chat.activeDraft).toBe('Entwurf mit Anhang'))
    expect(chat.activeAttachmentDraft[0].file.name).toBe('mitnehmen.wav')
  })

  it('keeps focus through running, idle and new-session agent switches and sends with Enter', async () => {
    chat.selectedSessionId = 'session-1'
    chat.current.agentProfileId = 'agent-a'
    chat.running = true
    chat.runId = 'run-1'
    chat.setDraft('Folgenachricht')
    const profiles = useAgentProfileStore()
    profiles.defaultAgentProfileId = 'agent-a'
    profiles.agents = [{ id: 'agent-a' }, { id: 'agent-b' }]
    await nextTick()
    const input = root.querySelector('.chat-composer__input')
    input.focus()
    const shortcut = (shiftKey = false, options = {}) => {
      const event = new KeyboardEvent('keydown', {
        key: shiftKey ? ':' : '.', code: 'Period', ctrlKey: true, shiftKey,
        bubbles: true, cancelable: true, ...options
      })
      input.dispatchEvent(event)
      window.dispatchEvent(new KeyboardEvent('keyup', { key: options.metaKey ? 'Meta' : 'Control' }))
      return event
    }

    expect(shortcut().defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(input)
    expect(chat.activeAgentProfileId).toBe('agent-b')
    expect(chat.current.agentProfileId).toBe('agent-a')
    expect(shortcut().defaultPrevented).toBe(true)
    expect(chat.activeAgentProfileId).toBe('agent-a')
    shortcut(false, { repeat: true })
    shortcut(false, { isComposing: true })
    expect(chat.activeAgentProfileId).toBe('agent-a')
    expect(shortcut(true).defaultPrevented).toBe(true)
    await nextTick()
    expect(root.querySelector('button[aria-label="Nachricht vormerken"]').dataset.icon).toBe('schedule_send')
    expect(sentMessages).toEqual([])
    shortcut(true, { metaKey: true, ctrlKey: false })
    await nextTick()
    expect(root.querySelector('button[aria-label="Nachricht jetzt senden"]').dataset.icon).toBe('arrow_upward')
    expect(document.activeElement).toBe(input)
    expect(sentMessages).toEqual([])
    document.body.dispatchEvent(new KeyboardEvent('keydown', { code: 'Period', ctrlKey: true, bubbles: true }))
    expect(chat.activeAgentProfileId).toBe('agent-a')

    chat.running = false
    chat.runId = null
    mocks.api.patch.mockReturnValue(new Promise(() => {}))
    await nextTick()
    shortcut()
    await nextTick()
    expect(document.activeElement).toBe(input)
    expect(input.disabled).toBe(false)
    expect(chat.activeAgentProfileId).toBe('agent-b')
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    expect(sentMessages).toEqual([{ content: 'Folgenachricht', options: { interrupt: false } }])

    chat.current = null
    session.value = chat.activeContext
    chat.setDraft('Erste Nachricht')
    mocks.api.post.mockReturnValueOnce(new Promise(() => {}))
    await nextTick()
    shortcut(false, { metaKey: true, ctrlKey: false })
    await nextTick()
    expect([chat.sessionSettingsPending, input.disabled, document.activeElement,
      root.querySelector('button[aria-label="Nachricht senden"]').disabled])
      .toEqual([1, false, input, false])
  })

  it('returns focus to the composer after selecting an agent or send mode', async () => {
    chat.selectedSessionId = 'session-1'
    chat.current.agentProfileId = 'agent-a'
    chat.running = true
    chat.runId = 'run-1'
    chat.setDraft('Weiter schreiben')
    await nextTick()
    const input = root.querySelector('.chat-composer__input')
    const agent = [...root.querySelectorAll('button')].find((item) => item.textContent === 'Agent wechseln')
    agent.focus()
    await click('Agent wechseln')
    expect(chat.activeAgentProfileId).toBe('agent-b')
    expect(document.activeElement).toBe(input)

    await click('Nach dem Lauf senden')
    expect(document.activeElement).toBe(input)
    expect(root.querySelector('button[aria-label="Nachricht vormerken"]')).toBeTruthy()
  })

  it('keeps Stop available alongside sending text or attachment drafts', async () => {
    chat.selectedSessionId = 'session-1'
    chat.running = true
    chat.runId = 'run-1'
    await nextTick()

    expect(root.querySelector('button[aria-label="Agent-Anfrage stoppen"]').dataset.icon).toBe('stop')
    expect(root.querySelector('button[aria-label="Versandart auswählen"]')).toBeNull()
    await click('Agent-Anfrage stoppen')
    expect(stoppedCount).toBe(1)
    expect(sentMessages).toEqual([])

    chat.setDraft('Kurs ändern')
    await nextTick()
    expect(root.querySelector('button[aria-label="Agent-Anfrage stoppen"]')).toBeTruthy()
    expect(root.querySelector('button[aria-label="Nachricht jetzt senden"]').dataset.icon).toBe('arrow_upward')
    expect(root.querySelector('button[aria-label="Versandart auswählen"]')).toBeTruthy()

    chat.setDraft('')
    await nextTick()
    expect(root.querySelector('button[aria-label="Agent-Anfrage stoppen"]')).toBeTruthy()
    expect(root.querySelector('button[aria-label="Versandart auswählen"]')).toBeNull()

    chat.addAttachmentFiles([file('beleg.wav')])
    await nextTick()
    await click('Nachricht jetzt senden')
    expect(sentMessages).toEqual([{ content: '', options: { interrupt: true } }])
    chat.clearAttachmentDraft('session-1')
    chat.setDraft('Kurs ändern')
    await click('Sprachnachricht aufnehmen')
    await nextTick()
    expect(root.querySelector('button[aria-label="Aufnahme beenden und senden"]')).toBeTruthy()
    expect(root.querySelector('button[aria-label="Versandart auswählen"]')).toBeTruthy()
  })

  it('switches the icon-only send button immediately and keeps the selected mode', async () => {
    chat.selectedSessionId = 'session-1'
    chat.running = true
    chat.runId = 'run-1'
    chat.setDraft('Kurs ändern')
    await nextTick()

    const mainButton = () => root.querySelector('button[aria-label="Nachricht jetzt senden"], button[aria-label="Nachricht vormerken"]')
    expect(mainButton().dataset.icon).toBe('arrow_upward')
    expect(mainButton().textContent).toBe('')
    expect(root.querySelector('button[aria-label="Versandart auswählen"]').dataset.icon).toBe('arrow_drop_down')

    await click('Nachricht jetzt senden')
    expect(sentMessages).toEqual([{ content: 'Kurs ändern', options: { interrupt: true } }])

    await click('Nach dem Lauf senden')
    expect(mainButton().dataset.icon).toBe('schedule_send')
    expect(mainButton().textContent).toBe('')
    await click('Nachricht vormerken')
    expect(sentMessages[1]).toEqual({ content: 'Kurs ändern', options: { interrupt: false } })
    expect(mainButton().dataset.icon).toBe('schedule_send')

    await click('Jetzt senden')
    expect(mainButton().dataset.icon).toBe('arrow_upward')
    await click('Nachricht jetzt senden')
    expect(sentMessages[2].options).toEqual({ interrupt: true })
  })

  it.each([['Control', 'ctrlKey'], ['Meta', 'metaKey']])('temporarily queues with %s, Enter or a send click', async (key, modifier) => {
    chat.running = true
    chat.runId = 'run-1'
    chat.setDraft('Später senden')
    await nextTick()
    const input = root.querySelector('.chat-composer__input')
    const press = () => input.dispatchEvent(new KeyboardEvent('keydown', { key, [modifier]: true, bubbles: true }))
    press()
    await nextTick()
    expect(root.querySelector('button[aria-label="Nachricht vormerken"]').dataset.icon).toBe('schedule_send')
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', [modifier]: true, bubbles: true, cancelable: true }))
    await click('Nachricht vormerken')
    expect(sentMessages.map(message => message.options.interrupt)).toEqual([false, false])

    window.dispatchEvent(new KeyboardEvent('keyup', { key }))
    await nextTick()
    expect(root.querySelector('button[aria-label="Nachricht jetzt senden"]').dataset.icon).toBe('arrow_upward')
    const newline = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true })
    input.dispatchEvent(newline)
    expect(newline.defaultPrevented).toBe(false)

    press()
    window.dispatchEvent(new Event('blur'))
    await nextTick()
    const sendButton = root.querySelector('button[aria-label="Nachricht jetzt senden"]')
    sendButton.dispatchEvent(new MouseEvent('pointerdown', { [modifier]: true, bubbles: true }))
    sendButton.click()
    await nextTick()
    expect(sentMessages.at(-1).options.interrupt).toBe(false)
  })

  it('shows a disabled composer during loading and hides it before redirecting a child session', async () => {
    chat.outboxLoaded = true
    chat.drafts.newSession.message = 'Entwurf einer neuen Session'
    let releaseChild
    let releaseRedirect
    const stopGuard = router.beforeEach((to) => to.query.subagent === 'session-1'
      ? new Promise((resolve) => { releaseRedirect = resolve }) : true)
    mocks.api.get.mockImplementation(async (url) => ({ data: url === '/sessions/session-1'
      ? await new Promise((resolve) => { releaseChild = resolve })
      : url === '/sessions/parent' ? { id: 'parent', messages: [] }
        : url.endsWith('/children') ? [] : { items: [] } }))
    showPage.value = true
    await vi.waitFor(() => expect(releaseChild).toBeTypeOf('function'))
    const input = root.querySelector('.chat-composer__input')
    expect(input && { disabled: input.disabled, value: input.value }).toEqual({ disabled: true, value: '' })
    releaseChild({ id: 'session-1', readOnly: true, parentSessionId: 'parent', messages: [] })
    await vi.waitFor(() => expect(releaseRedirect).toBeTypeOf('function'))
    expect(chat.current).toBeNull()
    expect(root.querySelector('.chat-composer')).toBeNull()
    releaseRedirect(true)
    await vi.waitFor(() => expect(router.currentRoute.value.query).toEqual({ session: 'parent', subagent: 'session-1' }))
    await vi.waitFor(() => expect(chat.current?.id).toBe('parent'))
    expect(mocks.api.post.mock.calls.some(([url]) => url.startsWith('/sessions/session-1'))).toBe(false)
    stopGuard()
  })

  it('shows active subagents on the latest assistant reply after a direct message and preserves their start history', async () => {
    const child = { id: 'child', parentSessionId: 'session-1', spawnedByRunId: 'run-1', spawnedByToolCallId: 'spawn', run: { status: 'running' } }
    const messages = [
      { id: 'user', role: 'user', content: 'Prüfe die Dateien' },
      { id: 'original', role: 'assistant', runId: 'run-1', toolCalls: [{ id: 'spawn', name: 'task', arguments: {} }] },
      { id: 'direct', role: 'user', content: 'Prüfe zuerst die Tests' },
      { id: 'latest', role: 'assistant', content: 'Ich passe die Reihenfolge an.' }
    ]
    chat.outboxLoaded = true
    mocks.api.get.mockImplementation(async (url) => ({ data: url === '/sessions/session-1'
      ? { id: 'session-1', messages }
      : url.endsWith('/children') ? [child] : { items: [] } }))
    showPage.value = true

    await vi.waitFor(() => expect([...root.querySelectorAll('.chat-message--assistant')].map((message) => ({
      id: message.dataset.messageId,
      children: message.querySelector('[data-subagents]')?.dataset.subagents,
      active: message.querySelector('[data-subagents]')?.dataset.showActiveSubagents
    }))).toEqual([
      { id: 'original', children: 'child', active: 'false' },
      { id: 'latest', children: 'child', active: 'true' }
    ]))
  })

  it('applies the latest same-session deep link when the session finishes loading', async () => {
    chat.outboxLoaded = true
    let releaseSession
    const openApp = vi.spyOn(useControlCenterStore(), 'openApp').mockResolvedValue()
    mocks.api.get.mockImplementation(async (url) => ({ data: url === '/sessions/session-1'
      ? await new Promise((resolve) => { releaseSession = resolve })
      : url.endsWith('/children') ? [] : { items: [] } }))
    showPage.value = true
    await vi.waitFor(() => expect(releaseSession).toBeTypeOf('function'))
    await router.push('/?session=session-1&message=target&controlCenter=automation')
    releaseSession({ id: 'session-1', messages: [{ id: 'target', role: 'assistant', content: 'Result' }] })

    await vi.waitFor(() => expect([
      root.querySelector('.chat-message--search-target')?.dataset.messageId,
      openApp.mock.calls
    ]).toEqual(['target', [['automation']]]))
  })

  it('keeps the mounted parent composer, draft and session when only the subagent URL changes', async () => {
    chat.outboxLoaded = true
    mocks.api.get.mockImplementation(async (url) => ({ data: url === '/sessions/session-1'
      ? { id: 'session-1', messages: [] }
      : url.endsWith('/children') ? [] : { items: [] } }))
    showPage.value = true
    await vi.waitFor(() => expect(mocks.api.get.mock.calls.some(([url]) => url === '/sessions/session-1')).toBe(true))
    await vi.waitFor(() => expect(chat.current?.id).toBe('session-1'))
    await nextTick()
    const parent = chat.current
    chat.setDraft('Unveränderter Elternentwurf')
    mocks.api.get.mockClear()
    mocks.recorder.cancelRecording.mockClear()
    await router.push('/?session=session-1&subagent=child')
    await router.push('/?session=session-1')
    expect(chat.current).toBe(parent)
    expect(chat.activeDraft).toBe('Unveränderter Elternentwurf')
    expect(mocks.api.get.mock.calls.some(([url]) => url === '/sessions/session-1')).toBe(false)
    expect(mocks.recorder.cancelRecording).not.toHaveBeenCalled()
  })

  it.each(['model', 'disabled', 'loading', 'failed'])('records with unavailable transcription: %s', async (reason) => {
    const settings = useToolSettingsStore()
    if (reason === 'model') useProviderStore().audioTranscriptionModelId = null
    if (reason === 'disabled') settings.transcribeAudioAvailable = false
    if (reason === 'loading') settings.loading = true
    if (reason === 'failed') settings.loadFailed = true
    await click('Sprachnachricht aufnehmen')
    expect(mocks.recorder.startRecording).toHaveBeenCalledOnce()
  })

  it('uses the microphone button to start and finish a compact recording', async () => {
    await click('Sprachnachricht aufnehmen')
    expect(root.querySelector('.composer-recording-status')?.textContent).toContain('0:01')

    await click('Sprachnachricht beenden')
    expect(mocks.recorder.stopRecording).toHaveBeenCalledOnce()
    expect(root.querySelector('.composer-recording-status')).toBeNull()

    stopResolve(file())
    await nextTick()
    await nextTick()
    expect(chat.activeAttachmentDraft[0].voiceMessage).toBe(true)
  })

  it.each([
    ['idle', false, false],
    ['direct', true, false],
    ['queued', true, true]
  ])('finishes and sends the voice recording in %s mode', async (_mode, running, queued) => {
    chat.selectedSessionId = 'session-1'
    chat.running = running
    if (running) chat.runId = 'run-1'
    chat.setDraft('Zusätzlicher Text')
    await nextTick()
    await click('Sprachnachricht aufnehmen')
    if (queued) await click('Nach dem Lauf senden')
    const label = queued ? 'Aufnahme beenden und vormerken' : 'Aufnahme beenden und senden'
    expect(root.querySelector(`button[aria-label="${label}"]`).disabled).toBe(false)

    await click(label)
    expect(mocks.recorder.stopRecording).toHaveBeenCalledOnce()
    expect(stoppedCount).toBe(0)
    expect(sentMessages).toEqual([])
    expect(root.querySelector(`button[aria-label="${label}"]`).disabled).toBe(true)
    stopResolve(file())

    await vi.waitFor(() => expect(sentMessages).toEqual([{
      content: 'Zusätzlicher Text', options: { interrupt: running && !queued }
    }]))
    expect(chat.activeAttachmentDraft[0].voiceMessage).toBe(true)
    expect(chat.getAudioDictation('session-1')).toBeNull()
  })

  it('retains a rejected recording without sending or clearing the text draft', async () => {
    useInstanceLimitsStore().maxAttachmentsPerMessage = 0
    chat.selectedSessionId = 'session-1'
    chat.setDraft('Bleibt erhalten')
    await nextTick()
    await click('Sprachnachricht aufnehmen')
    await click('Aufnahme beenden und senden')
    stopResolve(file('abgewiesen.wav'))

    await vi.waitFor(() => expect(chat.getAudioDictation('session-1')?.files[0].name).toBe('abgewiesen.wav'))
    expect(sentMessages).toEqual([])
    expect(chat.activeDraft).toBe('Bleibt erhalten')
    expect(chat.activeAttachmentDraft).toEqual([])
  })

  it('does not send a finished recording to a newly selected session', async () => {
    chat.selectedSessionId = 'session-1'
    await nextTick()
    await click('Sprachnachricht aufnehmen')
    await click('Aufnahme beenden und senden')
    chat.current = { id: 'session-2' }
    session.value = chat.current
    chat.selectedSessionId = 'session-2'
    await nextTick()
    stopResolve(file('alte-session.wav'))

    await vi.waitFor(() => expect(chat.attachmentDrafts.sessionMessages['session-1']?.[0]?.voiceMessage).toBe(true))
    expect(sentMessages).toEqual([])
    expect(chat.activeAttachmentDraft).toEqual([])
  })

  it.each(['recording', 'starting', 'finalizing'])('hands off actual route navigation while %s', async (state) => {
    chat.outboxLoaded = true
    mocks.api.get.mockImplementation(async (url) => ({ data: url.startsWith('/sessions/session-')
      ? { id: url.split('/').pop(), messages: [] }
      : { items: [] } }))
    showPage.value = true
    await vi.waitFor(() => expect(mocks.api.get.mock.calls.some(([url]) => url === '/sessions/session-1')).toBe(true))
    await vi.waitFor(() => expect(chat.current?.id).toBe('session-1'))
    await nextTick()
    await click('Sprachnachricht aufnehmen')
    if (state === 'starting') {
      mocks.recorder.recording.value = false
      mocks.recorder.starting.value = true
    }
    if (state === 'finalizing') await click('Sprachnachricht beenden')
    mocks.api.get.mockClear()
    await router.push('/?session=session-2')
    if (state !== 'starting') {
      expect(mocks.recorder.stopRecording).toHaveBeenCalledOnce()
      expect(chat.current.id).toBe('session-1')
      expect(mocks.api.get.mock.calls.some(([url]) => url === '/sessions/session-2')).toBe(false)
      stopResolve(file())
    }
    await vi.waitFor(() => expect(chat.current?.id).toBe('session-2'))
    expect(chat.activeAttachmentDraft).toEqual([])
    if (state === 'starting') {
      expect(mocks.recorder.cancelRecording).toHaveBeenCalled()
      expect(chat.attachmentDrafts.sessionMessages['session-1']).toBeUndefined()
    } else {
      expect(chat.attachmentDrafts.sessionMessages['session-1'][0].voiceMessage).toBe(true)
    }
  })

  it.each(['delete', 'logout'])('does not retain a late recording after explicit %s', async (action) => {
    await click('Sprachnachricht aufnehmen')
    await click('Sprachnachricht beenden')
    if (action === 'delete') {
      mocks.api.delete.mockResolvedValue({})
      await chat.deleteSession('session-1')
    } else chat.setDraftOwner(null)
    stopResolve(file())
    mocks.onAutoStopped(file('late.wav'))
    await nextTick()
    expect(chat.getAudioDictation('session-1')).toBeNull()
    expect(chat.attachmentDrafts.sessionMessages['session-1']).toBeUndefined()
  })

  it('retains multiple recordings as marked, playable attachments without draft transcription', async () => {
    for (const name of ['eins.wav', 'zwei.wav']) {
      await click('Sprachnachricht aufnehmen')
      await click('Sprachnachricht beenden')
      stopResolve(file(name))
      await nextTick()
      await nextTick()
    }
    expect(chat.activeAttachmentDraft.map((item) => item.voiceMessage)).toEqual([true, true])
    expect(root.querySelectorAll('audio[controls]')).toHaveLength(2)
    expect(root.textContent).toContain('Transkription beim Senden')
    expect(mocks.api.post).not.toHaveBeenCalled()
    await click('eins.wav entfernen')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:audio')
  })

  it('keeps rejected recordings for download and retry without overwriting earlier recordings', async () => {
    useInstanceLimitsStore().maxAttachmentsPerMessage = 0
    for (const name of ['eins.wav', 'zwei.wav']) {
      await click('Sprachnachricht aufnehmen')
      await click('Sprachnachricht beenden')
      stopResolve(file(name))
      await nextTick()
      await nextTick()
    }
    expect(chat.getAudioDictation('session-1').files).toHaveLength(2)
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await click('Herunterladen')
    expect(download).toHaveBeenCalledOnce()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:audio')
    useInstanceLimitsStore().maxAttachmentsPerMessage = 8
    await click('Anhängen')
    expect(chat.activeAttachmentDraft[0].voiceMessage).toBe(true)
    expect(chat.getAudioDictation('session-1').files.map((item) => item.name)).toEqual(['zwei.wav'])
    download.mockRestore()
  })

  it.each(['session', 'project'])('binds an asynchronous stop to the original %s scope', async (scope) => {
    if (scope === 'project') {
      chat.current = null
      session.value = {}
      chat.sessionsProjectId = 'project-1'
      await nextTick()
    }
    await click('Sprachnachricht aufnehmen')
    await click('Sprachnachricht beenden')
    if (scope === 'project') chat.sessionsProjectId = 'project-2'
    else {
      chat.current = { id: 'session-2' }
      session.value = chat.current
    }
    await nextTick()
    stopResolve(file())
    await nextTick()
    await nextTick()
    const attachments = scope === 'project'
      ? chat.attachmentDrafts.newSessions['project-1']
      : chat.attachmentDrafts.sessionMessages['session-1']
    expect(attachments[0].voiceMessage).toBe(true)
    expect(chat.activeAttachmentDraft).toEqual([])
  })

  it('attaches automatically stopped recordings and preserves late stops after unmount', async () => {
    await click('Sprachnachricht aufnehmen')
    mocks.recorder.recording.value = false
    mocks.onAutoStopped(file('auto.wav'))
    await nextTick()
    expect(chat.activeAttachmentDraft[0].voiceMessage).toBe(true)
    await click('Sprachnachricht aufnehmen')
    await click('Sprachnachricht beenden')
    app.unmount()
    stopResolve(file('late.wav'))
    await nextTick()
    expect(chat.getAudioDictation('session-1').files[0].name).toBe('late.wav')
    mocks.onAutoStopped(file('late-auto.wav'))
    expect(chat.getAudioDictation('session-1').files.map((item) => item.name)).toEqual(['late.wav', 'late-auto.wav'])
  })
})
