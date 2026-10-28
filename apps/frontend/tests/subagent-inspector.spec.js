// @vitest-environment jsdom
import { computed, createApp, defineComponent, h, nextTick, ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { Quasar } from 'quasar'
import { quasarOptions } from './quasar-options'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }))
vi.mock('src/features/chat/components/MessageContent.vue', () => ({ default: defineComponent({ props: ['content'], setup: (props) => () => h('div', props.content) }) }))
vi.mock('src/features/files/components/FileViewerPanel.vue', () => ({ default: { render: () => null } }))

import SubagentInspector from '../src/features/subagents/SubagentInspector.vue'
import SubagentRows from '../src/features/subagents/SubagentRows.vue'
import AgentActivityPanel from '../src/features/chat/components/AgentActivityPanel.vue'
import FileChanges from '../src/features/chat/components/FileChanges.vue'
import ApprovalCard from '../src/features/approvals/ApprovalCard.vue'
import QuestionDock from '../src/features/questions/QuestionDock.vue'
import SubagentRequestLink from '../src/features/subagents/SubagentRequestLink.vue'
import { useSubagentStore } from '../src/stores/subagents'
import { useApprovalStore } from '../src/stores/approvals'
import { useQuestionStore } from '../src/stores/questions'
import { useChatStore } from '../src/features/chat/store'
import { useAgentProfileStore } from '../src/stores/agent-profiles'
import { buildChatTranscript } from '../src/utils/chat-transcript'
import { agentRunPresentation, mergeAgentActivity } from '../src/utils/agent-activity'
import { api } from 'src/boot/axios'

const { components } = quasarOptions
let app, root, store
function mountApp() {
  if (!app.config.globalProperties.$router) {
    app.use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] }))
  }
  return app.mount(root)
}
afterEach(() => {
  app?.unmount()
  root?.remove()
  store?.close()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('subagent inspector navigation and presentation', () => {
  it('summarizes repeated and deleted file operations without output cards', () => {
    const sessionId = '550e8400-e29b-41d4-a716-446655440000'
    const action = {
      root: 'session', rootIdentity: sessionId, path: 'temporary.txt', filename: 'temporary.txt', kind: 'file', sizeBytes: 4,
      downloadUrl: `/api/workspace/files/download?path=temporary.txt&scope=session&session_id=${sessionId}&root_identity=${sessionId}`
    }
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(FileChanges, {
      actions: ['create', 'edit', 'edit', 'delete'].map((operation) => ({ ...action, operation }))
    }) })
    app.use(createPinia()).use(Quasar, { components })
    mountApp()
    expect({
      rows: root.querySelectorAll('.q-list .q-item').length,
      caption: root.querySelector('.q-list .q-item__section--side:last-child')?.textContent.trim(),
      clickable: root.querySelector('.q-list .q-item--clickable')
    }).toEqual({ rows: 1, caption: 'Erstellt · Geändert · Gelöscht', clickable: null })
  })

  it('keeps generated media visible alongside a provided reference when history is collapsed', async () => {
    const sessionId = '550e8400-e29b-41d4-a716-446655440000'
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: {
          entries: [
            {
              key: 'image', type: 'tool',
              tool: { id: 'image', name: 'generate_image', arguments: { prompt: 'Tiger' } },
              result: { attachments: [{ id: 'tiger', kind: 'image', presentation: 'result', filename: 'tiger.png', mimeType: 'image/png', fileRootId: sessionId, filePath: 'tiger.png' }] }
            },
            {
              key: 'provided', type: 'tool', tool: { id: 'provided', name: 'provide_file' },
              result: { fileActions: [{
                operation: 'provide', kind: 'file', root: 'session', rootIdentity: sessionId, path: 'tiger.png', filename: 'tiger.png', sizeBytes: 12,
                downloadUrl: `/api/workspace/files/provided/${sessionId}/550e8400-e29b-41d4-a716-446655440001/0/download`
              }] }
            },
            {
              key: 'research', type: 'tool',
              tool: { id: 'research', name: 'webfetch', arguments: { url: 'https://example.com' } },
              result: { attachments: [{ id: 'reference', kind: 'image', presentation: 'internal', filename: 'reference.png', mimeType: 'image/png' }] }
            }
          ]
        }
      })
    })
    app.use(createPinia()).use(Quasar, { components })
    mountApp()

    expect(root.querySelector('.work-history__timeline')).toBeNull()
    expect(root.querySelectorAll('.work-history__outputs')).toHaveLength(2)
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect(root.querySelector('.work-history__timeline')).not.toBeNull()
    expect(root.querySelectorAll('.work-history__outputs')).toHaveLength(2)
  })

  it.each(['tool', 'reasoning', 'context-summary'])('opens %s details independently of history and preserves them during regrouping', async (type) => {
    const detail = 'Vollständiger verständlicher Detailinhalt. '.repeat(5)
    const entry = ref(type === 'tool'
      ? { key: 'current', type, tool: { id: 'current', name: 'web_search', arguments: { query: detail, internal_flag: 'NEVER_RENDER' } } }
      : type === 'reasoning'
        ? { key: 'current', type, live: true, reasoning: { kind: 'content', text: detail } }
        : { key: 'current', type, current: true, text: detail, presentation: { title: 'Kontext verdichten' } })
    const activity = ref({ phase: type === 'tool' ? 'processing_tool' : type === 'reasoning' ? 'waiting_model' : 'compacting_context', toolCallId: 'current', modelRequest: 1 })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
    await router.push('/')
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(AgentActivityPanel, {
      running: true,
      activity: { ...activity.value, ...(type === 'context-summary' ? { startedAt: entry.value.key } : {}) },
      contextSummary: type === 'context-summary' ? { content: detail } : null,
      history: { entries: [{ key: 'old', type: 'tool', result: {}, tool: { id: 'old', name: 'web_search', arguments: { query: 'Älterer Aufruf' } } }, ...(type === 'context-summary' ? [] : [entry.value])] }
    }) })
    app.use(createPinia()).use(router).use(Quasar, { components })
    mountApp()
    const row = root.querySelector('.activity-row')
    expect(row.getAttribute('aria-expanded')).toBe('false')
    expect(row.getAttribute('tabindex')).toBe('0')
    expect(row.querySelector('.work-history__detail-indicator').textContent).toBe('chevron_right')
    expect(root.querySelector('.work-history__toggle .q-icon').textContent).toBe('history')
    row.click()
    await nextTick()
    expect(row.querySelector('.work-history__detail-indicator').textContent).toBe('expand_more')
    expect(root.querySelector('.work-history__toggle').getAttribute('aria-expanded')).toBe('false')
    expect(root.textContent).not.toContain('Älterer Aufruf')
    expect(document.getElementById(row.getAttribute('aria-controls')).textContent).toContain(detail.trim())
    for (let i = 0; i < 2; i++) {
      root.querySelector('.work-history__toggle').click()
      await nextTick()
      expect(root.querySelector('.activity-row[aria-expanded="true"]')).not.toBeNull()
      expect(root.querySelector('.work-history__reasoning-detail').textContent).toContain(detail.trim())
    }
    if (type === 'tool') {
      entry.value = { ...entry.value, completed: true }
      activity.value = { phase: 'waiting_model', modelRequest: 2 }
      await nextTick()
      expect(root.querySelector('.activity-shimmer').textContent).toBe('Websuche')
      root.querySelector('.work-history__toggle').click()
      await nextTick()
      expect(root.querySelector('.tool-call__header[aria-expanded="true"]')).not.toBeNull()
      root.querySelector('.work-history__toggle').click()
      await nextTick()
      entry.value = { ...entry.value, key: 'next', completed: false, tool: { ...entry.value.tool, id: 'next' } }
      activity.value = { phase: 'processing_tool', toolCallId: 'next' }
    } else {
      entry.value = { ...entry.value, key: 'next' }
      activity.value = { ...activity.value, modelRequest: 2 }
    }
    await nextTick()
    expect(root.querySelector('.activity-row').getAttribute('aria-expanded')).toBe('false')
    expect(root.textContent).not.toContain('NEVER_RENDER')
    expect(root.textContent).not.toContain('Technische Argumente')
  })

  it('shows expanded shell commands without a repeated field label', async () => {
    const commands = ['printf "first\\n"', 'sudo -n true\nprintf "done\\n"']
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(AgentActivityPanel, {
      history: { entries: commands.map((command, index) => ({
        key: `shell-${index}`, type: 'tool', result: {},
        tool: { id: `shell-${index}`, name: 'shell.run', arguments: { command }, toolDisplay: { group: 'process', icon: 'terminal', label: 'Shell-Befehl ausführen' } }
      })) }
    }) })
    app.use(createPinia()).use(Quasar, { components })
    mountApp()

    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect([...root.querySelectorAll('.tool-call__summary')].map((node) => node.textContent))
      .toEqual(commands.map((command) => command.replace(/\s+/g, ' ')))
    root.querySelectorAll('.activity-row')[1].click()
    await nextTick()

    const shell = root.querySelector('.activity-row__shell-command')
    expect(shell.textContent).toBe(commands[1])
    expect(root.querySelector('.work-history__reasoning-detail').textContent).not.toContain('Shell-Befehl\n')
  })

  it.each([false, true])('shows status indicators without removing readable labels (reduced motion=%s)', async (reducedMotion) => {
    vi.stubGlobal('matchMedia', () => ({ matches: reducedMotion }))
    const states = [
      ['queued', 'schedule', 'Wartet'], ['running', 'play_circle_outline', 'Arbeitet'],
      ['waiting_approval', 'pending_actions', 'Freigabe erforderlich'],
      ['waiting_input', 'help_outline', 'Antwort erforderlich'],
      ['completed', 'check_circle', 'Abgeschlossen'], ['failed', 'error_outline', 'Fehlgeschlagen'],
      ['cancelled', 'cancel', 'Abgebrochen']
    ]
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'chat', component: { render: () => null } }] })
    await router.push('/?session=parent')
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(SubagentRows, { children: states.map(([status]) => ({ id: status, title: `Auftrag ${status}`, parentSessionId: 'parent', run: { status } })) }) })
    app.use(router).use(Quasar, { components })
    mountApp()
    const rows = [...root.querySelectorAll('a')]
    for (const [index, [status, icon, label]] of states.entries()) {
      expect(rows[index].textContent).toContain(`Auftrag ${status}`)
      expect(rows[index].textContent).toContain(label)
      const animated = !reducedMotion && ['queued', 'running'].includes(status)
      expect(Boolean(rows[index].querySelector('.q-spinner'))).toBe(animated)
      if (!animated) expect(rows[index].querySelector('.q-icon').textContent).toBe(icon)
    }
    expect(root.querySelectorAll('.q-spinner')).toHaveLength(reducedMotion ? 0 : 2)
  })

  it.each([[1280, false], [1280, true], [390, false], [390, true]])('renders a passive Quasar inspector and returns to the mounted parent (width=%s, dark=%s)', async (width, dark) => {
    vi.stubGlobal('scrollTo', vi.fn())
    vi.stubGlobal('innerWidth', width)
    vi.stubGlobal('EventSource', class { addEventListener() {} close() {} })
    const pinia = createPinia()
    setActivePinia(pinia)
    useAgentProfileStore().agents = [{ id: 'agent', name: 'Allgemein' }]
    store = useSubagentStore()
    const child = { id: 'child', parentSessionId: 'parent', title: 'Dateien untersuchen', run: { id: 'run', status: 'waiting_approval' } }
    api.get.mockImplementation(async (url) => ({ data: url.endsWith('/children') ? [child]
      : url.includes('/runs/') ? child.run
        : url.endsWith('/messages') ? { items: [{ id: 'old', role: 'user', content: 'Alter Auftrag' }], nextCursor: null }
          : { ...child, readOnly: true, nextMessageCursor: 'older', messages: [
            { id: 'task', role: 'user', content: 'Auftrag lesen' },
            { id: 'note', role: 'assistant', agentProfileId: 'agent', content: 'Ich prüfe die Datei.', toolCalls: [] },
            { id: 'tool', role: 'assistant', agentProfileId: 'agent', toolCalls: [{ id: 'read', name: 'read', arguments: {} }] },
            { id: 'answer', role: 'assistant', agentProfileId: 'agent', content: 'Ergebnis lesen', activeDurationMs: 57_000 }
          ] } }))
    await store.loadChildren('parent')
    const request = { id: 'request', sessionId: 'parent', runId: 'run', title: 'Datei freigeben', details: { toolCallId: 'read' }, availableDecisions: ['allow_once', 'deny'], payload: { subagentSessionId: 'child', subagentTitle: child.title } }
    useApprovalStore().add(request)
    useQuestionStore().add({ ...request, id: 'question', questions: [{ id: 'q', question: 'Welche Datei?' }] })
    const Parent = defineComponent({
      setup: () => {
        const route = router.currentRoute
        return () => h('main', [
          h('textarea', { 'aria-label': 'Elternentwurf', value: 'Mein Entwurf' }),
          h('div', { id: 'parent-scroll', style: 'overflow: auto' }, 'Elternverlauf'),
          h(AgentActivityPanel, { history: { entries: [] }, subagents: [child] }),
          h(SubagentRequestLink, { request }),
          route.value.query.subagent ? h(SubagentInspector, { parentId: 'parent', childId: route.value.query.subagent }) : null
        ])
      }
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'chat', component: Parent }] })
    await router.push('/?session=parent&project=project')
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(RouterView) })
    app.use(pinia)
    app.use(router)
    app.use(Quasar, { config: { dark }, components })
    app.config.globalProperties.$q.dark.set(dark)
    mountApp()
    const parentScroll = document.getElementById('parent-scroll')
    parentScroll.scrollTop = 123
    const row = [...root.querySelectorAll('a')].find((link) => link.textContent.includes('Freigabe erforderlich'))
    expect(row.textContent).toContain(child.title)
    expect(row.getAttribute('href')).toContain('session=parent')
    expect(row.getAttribute('href')).toContain('subagent=child')
    row.focus()
    row.click()
    await vi.waitFor(() => expect(store.current?.id).toBe('child'))
    await nextTick()
    const inspector = document.querySelector('.subagent-inspector')
    expect(inspector).not.toBeNull()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(inspector.textContent).toContain('Nur Lesezugriff')
    expect(inspector.textContent).toContain('Ergebnis lesen')
    const historyHeader = inspector.querySelector('.work-history__toggle')
    expect(historyHeader).not.toBeNull()
    expect([...inspector.querySelectorAll('.work-history__agent-header .chat-message__label span')].map((node) => node.textContent))
      .toEqual(['Subagent', '·', 'Allgemein'])
    expect(historyHeader.closest('.work-history__header-actions').previousElementSibling.classList)
      .toContain('work-history__agent-header')
    historyHeader.click()
    await nextTick()
    expect(inspector.textContent).toContain('Ich prüfe die Datei.')
    expect(inspector.textContent).toContain('Datei freigeben')
    expect(inspector.textContent).toContain('Welche Datei?')
    expect(inspector.querySelector('.work-history')).not.toBeNull()
    expect(inspector.querySelector('.approval-card, .question-dock')).toBeNull()
    expect(inspector.querySelector('textarea, input, [contenteditable="true"]')).toBeNull()
    expect(inspector.textContent).not.toMatch(/Für diesen Lauf erlauben|Fertig|Ablehnen|Erneut versuchen|Einstellungen/)
    const olderButton = [...inspector.querySelectorAll('button')].find((button) => button.textContent.includes('Ältere Nachrichten laden'))
    olderButton.click()
    await vi.waitFor(() => expect(inspector.textContent).toContain('Alter Auftrag'))
    expect(api.post).not.toHaveBeenCalled()
    expect(api.patch).not.toHaveBeenCalled()
    expect(api.delete).not.toHaveBeenCalled()
    expect(inspector.querySelector('.chat-message__meta [role="timer"]')).toBeNull()
    store.run = { id: 'run', status: 'completed' }
    await nextTick()
    expect(inspector.querySelector('.chat-message__meta [role="timer"]')).toBeNull()
    expect(inspector.querySelector('.work-history [role="timer"]').textContent).toContain('57 s')
    inspector.querySelector('a').click()
    await vi.waitFor(() => expect(router.currentRoute.value.query.subagent).toBeUndefined())
    expect(router.currentRoute.value.query).toEqual({ session: 'parent', project: 'project' })
    expect(document.getElementById('parent-scroll')).toBe(parentScroll)
    expect(parentScroll.scrollTop).toBe(123)
    expect(root.querySelector('textarea').value).toBe('Mein Entwurf')
    expect(document.activeElement).toBe(row)
    expect(store.current).toBeNull()
    expect(api.get.mock.calls.filter(([url]) => url.endsWith('/children'))).toHaveLength(1)
  })

  it('keeps compact history open when a run completes', async () => {
    const pinia = createPinia()
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
    await router.push('/')
    const running = ref(true)
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      setup: () => () => h(AgentActivityPanel, {
        history: {
          entries: [{
            key: 'commentary', type: 'commentary',
            message: { content: 'Bisheriger Kommentar' }
          }]
        },
        running: running.value,
        activity: { phase: 'streaming' },
        warnings: [{ code: 'privacy.placeholderRestorationIncomplete', modelRequest: 1 }]
      })
    })
    app.use(pinia).use(router).use(Quasar, { components })
    mountApp()

    const historyHeader = root.querySelector('.work-history__toggle')
    expect(root.textContent).not.toContain('Bisheriger Kommentar')
    historyHeader.click()
    await nextTick()
    expect(root.textContent).toContain('Bisheriger Kommentar')
    running.value = false
    await nextTick()
    expect(root.textContent).toContain('Bisheriger Kommentar')
  })

  it('omits aggregate status counts and places live duration before the history toggle', async () => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        running: true,
        expanded: true,
        activity: { phase: 'streaming', activeDurationMs: 296_000 },
        history: {
          activeDurationMs: 296_000,
          entries: [{
            key: 'failed', type: 'tool', tool: { id: 'failed', name: 'build_skill' },
            result: { content: '{"error":{"code":"tool.executionFailed"}}' }
          }, {
            key: 'interrupted', type: 'tool', tool: { id: 'interrupted', name: 'edit_skill' },
            result: { content: '{"error":{"code":"tool.executionInterrupted"}}' }
          }]
        }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    const error = root.querySelector('.work-history__message-count--error')
    const warning = root.querySelector('.work-history__message-count--warning')
    const duration = root.querySelector('[role="timer"]')
    const toggle = root.querySelector('.work-history__toggle')
    expect(duration.textContent).toContain('4 min 56 s')
    expect(error).toBeNull()
    expect(warning).toBeNull()
    expect(duration.compareDocumentPosition(toggle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('keeps the answer header and reveals duration on expansion when no activity entries exist', async () => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: [], activeDurationMs: 4_000 }
      }, { header: () => h('div', 'codemato · Allgemein') })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.querySelector('.work-history__agent-header').textContent).toContain('codemato · Allgemein')
    expect(root.querySelector('.work-history__toggle')).not.toBeNull()
    expect(root.querySelector('[role="timer"]')).toBeNull()
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect(root.querySelector('[role="timer"]').textContent).toBe('4 s')
  })

  it('shows initial activity and reveals live duration through the persistent history toggle', async () => {
    const panelKey = ref('live')
    const historyExpanded = ref(false)
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(AgentActivityPanel, {
      key: panelKey.value,
      history: { entries: [] }, running: true,
      activity: { phase: 'waiting_model', activeDurationMs: 38_000 },
      expanded: historyExpanded.value,
      onHistoryExpanded: (value) => { historyExpanded.value = value }
    }) })
    app.use(createPinia()).use(Quasar, { components })
    mountApp()

    expect(root.querySelector('.work-history--preview')).not.toBeNull()
    expect(root.querySelector('.activity-shimmer').textContent).toBe('Verarbeite Anfrage')
    expect(root.querySelector('.work-history__meta-section [role="timer"]')).toBeNull()
    expect(root.querySelector('.work-history__header')).toBeNull()
    expect(root.querySelector('.work-history__toggle').getAttribute('aria-expanded')).toBe('false')
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect(historyExpanded.value).toBe(true)
    expect(root.querySelector('.work-history__meta-section [role="timer"]').textContent).toBe('38 s')
    panelKey.value = 'canonical'
    await nextTick()
    expect(root.querySelector('.work-history__toggle').getAttribute('aria-expanded')).toBe('true')
  })

  it('keeps live status visible while tool messages are being persisted', async () => {
    const activity = ref({ phase: 'waiting_model', modelRequest: 1 })
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(AgentActivityPanel, {
      history: { entries: [{
        key: 'completed', type: 'tool', result: {},
        tool: { id: 'completed', name: 'web_search', arguments: { query: 'Vorherige Suche' } }
      }] },
      running: true,
      activity: activity.value,
      presentation: agentRunPresentation({ running: true, activity: activity.value })
    }) })
    app.use(createPinia()).use(Quasar, { components })
    mountApp()

    expect(root.querySelector('.work-history--preview')).not.toBeNull()
    expect(root.textContent).toContain('Verarbeite Anfrage')
    expect(root.querySelector('.work-history__activity-line .q-icon').textContent).toBe('settings')
    expect(root.textContent).not.toContain('Verlauf')
    expect(root.textContent).not.toContain('Vorherige Suche')
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect(root.querySelector('.work-history__toggle').getAttribute('aria-expanded')).toBe('true')
    expect(root.textContent).toContain('Vorherige Suche')

    activity.value = { phase: 'processing_tool', tool: 'read_file', toolCallId: 'pending' }
    await nextTick()

    expect(root.querySelector('.work-history--preview')).toBeNull()
    expect(root.querySelector('.activity-shimmer').textContent).toContain('Datei lesen')
    expect(root.textContent).not.toContain('Verlauf')
    expect(root.textContent).toContain('Vorherige Suche')
    expect(root.querySelector('.work-history__toggle').getAttribute('aria-expanded')).toBe('true')
    const row = root.querySelector('.activity-shimmer').closest('.tool-call__header')
    activity.value = mergeAgentActivity(activity.value, { phase: 'preparing_context', modelRequest: 2 })
    await nextTick()
    expect(root.querySelector('.activity-shimmer').closest('.tool-call__header')).toBe(row)
    expect(root.querySelector('.activity-shimmer').textContent).toContain('Datei lesen')

    activity.value = mergeAgentActivity(activity.value, { phase: 'waiting_model', modelRequest: 2 })
    await nextTick()
    expect(root.querySelector('.activity-shimmer').closest('.tool-call__header')).toBe(row)
    expect(root.textContent).toContain('Vorherige Suche')
    expect(root.querySelector('.work-history__header')).toBeNull()
  })

  it.each([true, false])('uses the shared tool row (running=%s)', async (running) => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: [{
          key: 'tool', type: 'tool', active: false,
          tool: {
            id: 'call', name: 'custom_browser_tool', arguments: { url: 'https://example.com/path' },
            toolDisplay: { group: 'web', icon: 'language', label: 'Browser prüfen', summary: 'example.com/path' }
          }
        }] },
        running,
        activity: { phase: 'processing_tool', toolCallId: 'call' }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    if (!running) {
      expect(root.querySelector('.tool-call__header')).toBeNull()
      root.querySelector('.work-history__toggle').click()
      await nextTick()
    }
    const row = root.querySelector('.tool-call__header')
    expect(row.querySelector('.q-icon').textContent).toBe('language')
    expect(row.querySelector('.q-icon').classList.contains('work-history__activity-title--past'))
      .toBe(!running)
    expect(row.querySelector('.tool-call__title').textContent.trim()).toBe('Browser prüfen')
    expect(row.querySelector('.tool-call__summary').textContent).toBe('example.com/path')
    expect(row.querySelector('.tool-call__title').classList.contains('activity-shimmer'))
      .toBe(running)
  })

  it('previews only the current call and groups all calls when expanded', async () => {
    const pinia = createPinia()
    const entries = ref([{
      key: 'first', type: 'tool', result: {},
      tool: { id: 'first', name: 'web_search', arguments: { query: 'Erste Suche' } }
    }, {
      key: 'second', type: 'tool', result: {},
      tool: { id: 'second', name: 'web_search', arguments: { query: 'Zweite Suche' } }
    }, {
      key: 'active', type: 'tool', active: true,
      tool: { id: 'active', name: 'web_search', arguments: { query: 'Aktuelle Suche' } }
    }])
    const activity = ref({ phase: 'processing_tool', toolCallId: 'active' })
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: entries.value },
        running: true,
        activity: activity.value
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.querySelector('.work-history__header')).toBeNull()
    expect(root.querySelector('.work-history__tool-icons')).toBeNull()
    expect(root.querySelector('.work-history__tool-group')).toBeNull()
    expect(root.querySelectorAll('.tool-call__header')).toHaveLength(1)
    expect(root.querySelector('.tool-call__summary').textContent).toBe('Aktuelle Suche')
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    const group = root.querySelector('.work-history__tool-group')
    expect(root.querySelector('.work-history__header')).toBeNull()
    expect(group.querySelector('.work-history__activity-title').textContent.trim()).toBe('Websuche')
    expect(group.querySelector('.work-history__activity-summary').textContent.trim()).toBe('· 3')
    expect([...group.querySelectorAll('.tool-call__summary')].map((node) => node.textContent))
      .toEqual(['Erste Suche', 'Zweite Suche', 'Aktuelle Suche'])
    expect(group.querySelector('.work-history__tool-group-items')).not.toBeNull()
    expect(group.querySelector('.work-history__activity-title').classList).toContain('activity-shimmer')
    expect(root.querySelectorAll('.activity-shimmer')).toHaveLength(1)
    expect(root.querySelector('.tool-call__title')).toBeNull()

    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect(root.querySelector('.work-history__tool-group')).toBeNull()
    expect(root.querySelectorAll('.tool-call__header')).toHaveLength(1)

    entries.value = entries.value.map((entry) => entry.key === 'active'
      ? { ...entry, active: false, completed: true }
      : entry)
    activity.value = { phase: 'waiting_model', modelRequest: 2 }
    await nextTick()

    expect(root.querySelector('.work-history__tool-group')).toBeNull()
    expect(root.textContent).not.toContain('Erste Suche')
    expect(root.querySelector('.activity-shimmer').textContent).toContain('Websuche')
    expect(root.querySelector('.tool-call__summary').textContent).toBe('Aktuelle Suche')
    expect(root.querySelector('.work-history__header')).toBeNull()

    entries.value = [...entries.value, {
      key: 'reasoning', type: 'reasoning', live: true,
      reasoning: { kind: 'content', text: 'Ergebnisse prüfen' }
    }]
    await nextTick()

    expect(root.querySelector('.work-history__tool-group')).toBeNull()
    expect(root.textContent).not.toContain('Erste Suche')
    expect(root.querySelector('.work-history__activity-title').textContent).toBe('Nachdenken')
  })

  it('omits empty detail rows from a tool group', async () => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { activeDurationMs: 4_000, entries: ['first', 'second'].map((id) => ({
          key: id, type: 'tool', result: {},
          tool: {
            id, name: 'edit_skill', arguments: {},
            toolDisplay: { group: 'agent', icon: 'smart_toy', label: 'Skill bearbeiten' }
          }
        })) }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.querySelector('[role="timer"]')).toBeNull()
    root.querySelector('.work-history__toggle').click()
    await nextTick()

    expect(root.querySelector('.work-history__header')).toBeNull()
    expect(root.querySelector('[role="timer"]').textContent).toBe('4 s')
    expect(root.querySelector('.work-history__activity-title').textContent.trim()).toBe('Skill bearbeiten')
    expect(root.querySelector('.work-history__tool-group .q-icon').classList)
      .toContain('work-history__activity-title--past')
    expect(root.querySelector('.work-history__activity-summary').textContent.trim()).toBe('· 2')
    expect(root.querySelectorAll('.tool-call--detail')).toHaveLength(0)
    expect(root.textContent).not.toContain('Aufruf')
  })

  it('keeps the same latest row while expanding older history', async () => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        running: true,
        activity: { phase: 'processing_tool', toolCallId: 'latest' },
        history: { entries: [{
          key: 'old', type: 'commentary', message: { content: 'Älterer Schritt' }
        }, {
          key: 'latest', type: 'tool',
          tool: { id: 'latest', name: 'web_search', arguments: { query: 'Neuester Schritt' } }
        }] }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    const latest = root.querySelector('.tool-call__header')
    expect(root.textContent).not.toContain('Älterer Schritt')
    root.querySelector('.work-history__toggle').click()
    await nextTick()

    expect(root.textContent).toContain('Älterer Schritt')
    expect(root.querySelector('.work-history__header')).toBeNull()
    expect(root.querySelector('.tool-call__header')).toBe(latest)
    expect(root.querySelectorAll('.tool-call__header')).toHaveLength(1)
  })

  it.each([
    [1280, false], [1280, true], [390, false], [390, true]
  ])('shows the current tool batch in the collapsed preview (width=%s, dark=%s)', async (width, dark) => {
    vi.stubGlobal('innerWidth', width)
    const messages = ref([
      { id: 'user', role: 'user', content: 'Prüfe die Dateien' },
      { id: 'previous', role: 'assistant', toolCalls: [{ id: 'old', name: 'web_search', arguments: { query: 'Alt' } }] },
      { id: 'old-result', role: 'tool', toolCallId: 'old', content: '{}' },
      {
        id: 'batch', role: 'assistant', toolCalls: [
          { id: 'first', name: 'read', arguments: { path: 'eins.md' } },
          { id: 'second', name: 'read', arguments: { path: 'zwei.md' } },
          { id: 'third', name: 'grep', arguments: { pattern: 'Text' } }
        ]
      }
    ])
    const activity = ref({ phase: 'processing_tool', toolCallId: 'first' })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
    await router.push('/')
    const history = computed(() => buildChatTranscript(messages.value, [], {
      running: true, toolExecutionActive: activity.value.phase === 'processing_tool',
      activeToolCallId: activity.value.toolCallId
    }).messages.find((message) => message.workHistory).workHistory)
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(AgentActivityPanel, {
      history: history.value, running: true, activity: activity.value
    }) })
    app.use(createPinia()).use(router).use(Quasar, { components, config: { dark } })
    mountApp()

    expect(root.querySelectorAll('.work-history__tool-group')).toHaveLength(1)
    expect(root.querySelector('.work-history__tool-group-header').textContent).toContain('· 2')
    expect([...root.querySelectorAll('.tool-call__summary')].map((item) => item.textContent))
      .toEqual(['eins.md', 'zwei.md', 'Text'])
    expect(root.querySelectorAll('.tool-call--running')).toHaveLength(1)
    expect(root.textContent).not.toContain('Alt')

    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect(root.textContent).toContain('Alt')
    expect(root.textContent).not.toContain('zwei.md')
    expect(root.textContent).not.toContain('Text')
    root.querySelector('.work-history__toggle').click()
    messages.value.push({ id: 'first-result', role: 'tool', toolCallId: 'first', content: '{}' })
    activity.value = { phase: 'processing_tool', toolCallId: 'second' }
    await nextTick()
    expect(root.querySelectorAll('.work-history__tool-group')).toHaveLength(1)
    expect(root.querySelectorAll('.tool-call--running')).toHaveLength(1)
    expect(root.querySelector('.tool-call--running .tool-call__summary').textContent).toBe('zwei.md')
  })

  it.each([
    [1280, false], [1280, true], [390, false], [390, true]
  ])('keeps commentary with its tools until output starts (width=%s, dark=%s)', async (width, dark) => {
    vi.stubGlobal('innerWidth', width)
    const pinia = createPinia()
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
    await router.push('/')
    const running = ref(true)
    const activity = ref({ phase: 'processing_tool', toolCallId: 'search' })
    const approvals = ref([])
    const messages = ref([
      { id: 'user', role: 'user', content: 'Bitte prüfen' },
      { id: 'old', role: 'assistant', content: 'Älterer Schritt', toolCalls: [] },
      {
        id: 'step', role: 'assistant', content: 'Ich verifiziere das kurz über eine Online-Suche:',
        toolCalls: [
          { id: 'search', name: 'web_search', arguments: { query: 'Aktuelle Nachricht' } },
          { id: 'fetch', name: 'webfetch', arguments: { url: 'https://example.com' } }
        ]
      }
    ])
    const history = computed(() => buildChatTranscript(messages.value, approvals.value, {
      running: running.value,
      toolExecutionActive: activity.value.phase === 'processing_tool',
      activeToolCallId: activity.value.toolCallId
    }).messages.find((message) => message.workHistory).workHistory)
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(AgentActivityPanel, {
      history: history.value, running: running.value, activity: activity.value
    }) })
    app.use(pinia).use(router).use(Quasar, { components, config: { dark } })
    mountApp()

    const intro = 'Ich verifiziere das kurz über eine Online-Suche:'
    expect(root.textContent).toContain(intro)
    expect(root.textContent).toContain('Aktuelle Nachricht')
    expect(root.textContent).not.toContain('Älterer Schritt')
    const row = root.querySelector('.tool-call__header')
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect(root.textContent).toContain('Älterer Schritt')
    expect(root.querySelector('.tool-call__header')).toBe(row)
    root.querySelector('.work-history__toggle').click()
    await nextTick()

    messages.value.push({ role: 'tool', toolCallId: 'search', content: '{}' })
    activity.value = { phase: 'processing_tool', toolCallId: 'fetch' }
    await nextTick()
    expect(root.textContent).toContain(intro)
    expect(root.querySelector('.tool-call--running .tool-call__title').textContent).toContain('Webseite abrufen')
    const fetchRow = root.querySelector('.tool-call--running')

    messages.value.push({ role: 'tool', toolCallId: 'fetch', content: '{}' })
    await nextTick()
    expect(root.querySelectorAll('.tool-call__header')[1]).toBe(fetchRow)
    expect(root.querySelector('.activity-shimmer').textContent).toContain('Webseite abrufen')
    activity.value = { phase: 'waiting_model', modelRequest: 2 }
    await nextTick()
    expect(root.querySelector('.activity-shimmer').textContent).toContain('Webseite abrufen')
    expect(root.querySelector('.tool-call__header')).toBe(fetchRow)
    expect(root.querySelector('.work-history__header')).toBeNull()
    expect(root.textContent).toContain(intro)

    messages.value.push({
      id: 'write', role: 'assistant', content: '',
      toolCalls: [{ id: 'write', name: 'write_file', arguments: { path: 'result.txt', content: 'ok' } }]
    })
    approvals.value.push({
      id: 'approval', title: 'Datei schreiben?', riskLevel: 'medium', resourceType: 'path',
      resourceScope: '/result.txt', availableDecisions: ['allow_once', 'deny'], details: { toolCallId: 'write' }
    })
    activity.value = { phase: 'waiting_approval', toolCallId: 'write' }
    await nextTick()
    expect(root.textContent).toContain(intro)
    expect(root.textContent).toContain('Datei schreiben?')

    approvals.value = []
    activity.value = { phase: 'streaming' }
    await nextTick()
    expect(root.textContent).not.toContain(intro)
    expect(root.querySelector('.tool-call__header')).toBeNull()

    root.querySelector('.work-history__toggle').click()
    await nextTick()
    const text = root.textContent
    expect(text.indexOf(intro)).toBeLessThan(text.indexOf('Aktuelle Nachricht'))
    expect(text.indexOf('Aktuelle Nachricht')).toBeLessThan(text.indexOf('example.com'))
    expect(root.querySelectorAll('.tool-call__header')).toHaveLength(2)

    messages.value.push({ id: 'final', role: 'assistant', content: 'Hier ist die Antwort.' })
    running.value = false
    activity.value = { phase: 'idle' }
    await nextTick()
    expect(root.textContent).toContain(intro)
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect(root.querySelector('.work-history__timeline')).toBeNull()
    expect(root.querySelector('.tool-call__header')).toBeNull()
  })

  it('shows status icons only on each grouped tool', async () => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: ['https://example.com/first', 'https://example.com/second'].map((url, index) => ({
          key: `fetch-${index}`, type: 'tool',
          tool: { id: `fetch-${index}`, name: 'webfetch', arguments: { url } },
          result: { content: `{"error":{"code":"${index ? 'tool.executionInterrupted' : 'tool.executionFailed'}"}}` }
        })) }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.querySelector('.tool-call--detail')).toBeNull()
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    const details = [...root.querySelectorAll('.tool-call--detail')]
    expect(details).toHaveLength(2)
    expect(details.map((row) => row.querySelector('.tool-call__state .q-icon').textContent))
      .toEqual(['error', 'warning'])
    expect(root.querySelector('.work-history__message-count--error')).toBeNull()
    expect(root.querySelector('.work-history__message-count--warning')).toBeNull()
    expect(root.querySelectorAll('.tool-call__message')).toHaveLength(0)
    details[0].click()
    await nextTick()
    expect(root.querySelectorAll('.tool-call__message')).toHaveLength(1)
    expect(root.querySelector('.tool-call__message').textContent).toContain('Die Tool-Aktion ist fehlgeschlagen.')
    expect(details[1].getAttribute('aria-expanded')).toBe('false')
  })

  it('renders tool calls as static rows without technical details', async () => {
    const pinia = createPinia()
    const entry = ref({
      key: 'tool', type: 'tool', active: true,
      tool: { id: 'call', name: 'browser_navigate', arguments: { url: 'https://example.com' } }
    })
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: [entry.value] },
        running: true,
        activity: { phase: 'processing_tool', toolCallId: 'call' }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.textContent).not.toContain('Technische Argumente')
    expect(root.querySelector('.tool-call .q-expansion-item')).toBeNull()
    expect(root.querySelector('.tool-call__title').classList).toContain('activity-shimmer')
    entry.value = {
      ...entry.value,
      result: { content: '{"error":{"code":"tool.executionFailed"}}' }
    }
    await nextTick()
    expect(root.textContent).not.toContain('Technische Argumente')
    expect(root.querySelector('.tool-call__title').classList).toContain('activity-shimmer')
    expect(root.textContent).not.toContain('Fehlgeschlagen')
    expect(root.querySelector('.tool-call__status .q-icon').textContent).toBe('error')
    expect(root.querySelector('.tool-call__status .q-icon').getAttribute('aria-label')).toBe('Fehlgeschlagen')
    expect(root.textContent).not.toContain('Die Tool-Aktion ist fehlgeschlagen.')
    root.querySelector('.tool-call__header').click()
    await nextTick()
    expect(root.textContent).toContain('Die Tool-Aktion ist fehlgeschlagen.')
    expect(root.textContent).not.toContain('tool.executionFailed')
    entry.value = {
      ...entry.value,
      result: { content: '{"error":{"code":"tool.executionInterrupted"}}' }
    }
    await nextTick()
    expect(root.textContent).not.toContain('Unterbrochen')
    expect(root.querySelector('.tool-call__status .q-icon').textContent).toBe('warning')
    expect(root.querySelector('.tool-call__status .q-icon').getAttribute('aria-label')).toBe('Unterbrochen')
    expect(root.textContent).toContain('Die Tool-Aktion wurde unterbrochen.')
    expect(root.textContent).not.toContain('tool.executionInterrupted')
  })

  it('shows real reasoning between tools', async () => {
    const pinia = createPinia()
    const activity = ref({ phase: 'waiting_model', modelRequest: 2 })
    const first = {
      key: 'first', type: 'tool', result: { content: '{}' },
      tool: { id: 'first', name: 'web_search', arguments: { query: 'Lokale KI' } }
    }
    const entries = ref([first])
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: entries.value }, running: true, activity: activity.value,
        reasoning: { kind: 'content', text: 'Ergebnisse prüfen' }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.textContent).toContain('Nachdenken')
    expect(root.querySelector('.activity-shimmer')).not.toBeNull()
    expect(root.querySelector('.work-history__activity-line .q-icon').textContent).toBe('psychology')
    entries.value = [first, {
      key: 'second', type: 'tool', active: true,
      tool: { id: 'second', name: 'session_read', arguments: {} }
    }]
    activity.value = { phase: 'processing_tool', toolCallId: 'second' }
    await nextTick()
    const activeTitle = root.querySelector('.activity-shimmer')
    expect(activeTitle.textContent).toContain('Session lesen')
  })

  it.each([
    ['reasoning', { phase: 'waiting_model' }, { key: 'reasoning', type: 'reasoning', live: true, reasoning: { kind: 'content', text: 'Prüfen' } }],
    ['compaction', { phase: 'compacting_context' }, { key: 'summary', type: 'context-summary', current: true, text: 'Kontext', presentation: { title: 'Gesprächskontext wird verdichtet' } }]
  ])('keeps current %s subtly collapsible', async (_kind, activity, entry) => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: entry.type === 'context-summary' ? [] : [entry] }, running: true, activity,
        contextSummary: entry.type === 'context-summary' ? { content: entry.text } : null
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()
    await nextTick()

    const reasoningHeader = root.querySelector('.work-history__reasoning .q-item')
    expect(reasoningHeader.getAttribute('aria-expanded')).toBe('false')
    expect(root.querySelector('.work-history__activity-title').classList).toContain('activity-shimmer')
    reasoningHeader.click()
    await nextTick()
    expect(reasoningHeader.getAttribute('aria-expanded')).toBe('true')
  })

  it('keeps past commentary in history without marking it active', async () => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        running: true, activity: { phase: 'waiting_model', modelRequest: 2 },
        history: { entries: [{ key: 'note', type: 'commentary', message: { content: 'Zwischenstand' } }] }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.textContent).not.toContain('Zwischenstand')
    expect(root.querySelector('.activity-shimmer').textContent).toBe('Verarbeite Anfrage')
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    expect(root.textContent).toContain('Zwischenstand')
    expect(root.querySelector('.activity-shimmer').textContent).toBe('Verarbeite Anfrage')
  })

  it('shows the current reasoning summary beside its label and removes it afterwards', async () => {
    const pinia = createPinia()
    const activity = ref({ phase: 'waiting_model' })
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: [] }, running: true,
        activity: activity.value,
        reasoning: { kind: 'summary', text: '**Ergebnisse prüfen**' }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.querySelector('.work-history__activity-title').textContent).toBe('Nachdenken')
    expect(root.querySelector('.work-history__activity-title').classList).toContain('activity-shimmer')
    expect(root.querySelector('.work-history__activity-summary').textContent.trim()).toBe('Ergebnisse prüfen')
    activity.value = { phase: 'processing_tool' }
    await nextTick()
    expect(root.querySelector('.work-history__reasoning')).toBeNull()
  })

  it('does not offer an empty reasoning expander', () => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: [] }, running: true,
        activity: { phase: 'waiting_model' }, reasoning: { kind: 'content', text: '' }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.textContent).toContain('Nachdenken')
    expect(root.querySelector('.work-history__reasoning')).toBeNull()
    expect(root.querySelector('.work-history__reasoning-static')).not.toBeNull()
  })

  it.each(['waiting_approval', 'waiting_input'])('pauses fallback activity during %s', (phase) => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: [] }, running: true,
        activity: { phase }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.textContent).toContain('erforderlich')
    expect(root.querySelector('.activity-shimmer')).toBeNull()
  })

  it('shows the concrete active tool ahead of warnings', () => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        history: { entries: [
          {
            key: 'read', type: 'tool', active: true,
            tool: { id: 'read', name: 'read_file', arguments: { path: 'README.md' } }
          },
          {
            key: 'warning', type: 'warning', live: true,
            presentation: { title: 'Hinweis', summary: 'Details' }
          }
        ] },
        running: true,
        activity: { phase: 'processing_tool', toolCallId: 'read' }
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.textContent).not.toContain('Kontext wird gesammelt')
    expect(root.querySelector('.tool-call__title').textContent).toContain('Datei lesen')
    expect(root.querySelector('.tool-call__title').classList).toContain('activity-shimmer')
  })

  it('keeps completed reasoning and compaction details in history', async () => {
    const pinia = createPinia()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(AgentActivityPanel, {
        running: true,
        history: { entries: [{
          key: 'reasoning', type: 'reasoning',
          reasoning: { kind: 'content', text: 'Öffentliche Überlegung' }
        }, {
          key: 'summary', type: 'context-summary', text: 'Goal\nVollständiger Kontext',
          presentation: { title: 'Gesprächskontext verdichtet', summary: null, duration: null }
        }] },
        activity: { phase: 'waiting_model', modelRequest: 2 },
        presentation: agentRunPresentation({
          running: true, activity: { phase: 'waiting_model', modelRequest: 2 }
        })
      })
    })
    app.use(pinia).use(Quasar, { components })
    mountApp()

    expect(root.textContent).not.toContain('Verarbeite Tool-Ergebnisse')
    expect(root.textContent).not.toContain('Öffentliche Überlegung')
    root.querySelector('.work-history__toggle').click()
    await nextTick()
    const detailRows = root.querySelectorAll('.work-history__reasoning .q-item')
    expect(detailRows).toHaveLength(2)
    expect([...root.querySelectorAll('.work-history__activity-title')].map((node) => node.textContent.trim()))
      .toEqual(['Nachdenken', 'Gesprächskontext verdichtet', 'Verarbeite Anfrage'])
    detailRows[0].click()
    await nextTick()
    expect(root.textContent).toContain('Öffentliche Überlegung')
    detailRows[1].click()
    await nextTick()
    expect(root.textContent).toContain('Goal\nVollständiger Kontext')
  })

  it('keeps timeline tool supplements beside their tool without aggregate duplicates', async () => {
    const pinia = createPinia()
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'chat', component: { render: () => null } }] })
    await router.push('/?session=parent')
    const readOnly = ref(false)
    const approval = {
      id: 'approval', sessionId: 'parent', runId: 'run', requestType: 'agent_action',
      title: 'Datei schreiben?', riskLevel: 'medium', resourceType: 'path', resourceScope: '/result.txt',
      availableDecisions: ['allow_once', 'deny'], details: { toolCallId: 'call' }
    }
    const child = {
      id: 'child', parentSessionId: 'parent', title: 'Ergebnis prüfen',
      spawnedByRunId: 'run', spawnedByToolCallId: 'call', run: { status: 'running' }
    }
    const fileAction = {
      operation: 'provide', kind: 'file', root: 'session', rootIdentity: '550e8400-e29b-41d4-a716-446655440009', path: 'result.txt',
      filename: 'result.txt', sizeBytes: 7,
      downloadUrl: '/api/workspace/files/provided/550e8400-e29b-41d4-a716-446655440000/550e8400-e29b-41d4-a716-446655440001/0/download'
    }
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      setup: () => () => h(AgentActivityPanel, {
        readOnly: readOnly.value,
        subagents: [child],
        history: { entries: [
          { key: 'before', type: 'commentary', message: { content: 'Vor dem Tool' } },
          {
            key: 'tool', type: 'tool', runId: 'run', approval,
            tool: { id: 'call', name: 'read_file', arguments: { path: 'result.txt' } },
            result: { fileActions: [fileAction] }
          },
          { key: 'after', type: 'commentary', message: { content: 'Nach dem Tool' } }
        ] }
      })
    })
    app.use(pinia).use(router).use(Quasar, { components })
    mountApp()

    root.querySelector('.work-history__toggle').click()
    await nextTick()
    const after = [...root.querySelectorAll('div')].find((element) => element.textContent === 'Nach dem Tool')
    const approvalCard = root.querySelector('.approval-card')
    const subagentList = root.querySelector('[aria-label="Subagenten"]')
    const output = root.querySelector('.work-history__outputs')
    expect({
      approval: Boolean(approvalCard),
      subagent: Boolean(subagentList),
      output: Boolean(output),
      after: Boolean(after)
    }).toEqual({ approval: true, subagent: true, output: true, after: true })
    for (const supplement of [approvalCard, subagentList, output]) {
      expect(supplement.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
    expect(root.querySelectorAll('.approval-card')).toHaveLength(1)
    expect(root.querySelectorAll('.work-history__outputs')).toHaveLength(1)

    readOnly.value = true
    await nextTick()
    expect(root.querySelector('.approval-card')).toBeNull()
    expect(root.querySelector('[aria-label="result.txt ansehen"]')).toBeNull()
    expect(root.querySelector('[aria-label="result.txt herunterladen"]')).not.toBeNull()
  })

  it('shows simultaneous child approval cards and questions in the main session without changing parent activity', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    store = useSubagentStore()
    const approvals = useApprovalStore()
    const questions = useQuestionStore()
    const chat = useChatStore()
    chat.current = { id: 'parent', messages: [] }
    chat.runId = 'parent-run'
    chat.running = true
    chat.activity = { phase: 'processing_tool', tool: 'task' }
    const pending = ['one', 'two'].map((id) => ({
      id, sessionId: 'parent', runId: `run-${id}`, requestType: 'agent_action',
      title: `Datei ${id} lesen`, riskLevel: 'low', resourceType: 'path', resourceScope: `/${id}`,
      availableDecisions: ['allow_once', 'deny'], details: {},
      payload: { subagentSessionId: `child-${id}`, subagentTitle: `Auftrag ${id}` }
    }))
    const pendingQuestions = pending.map((request) => ({ ...request, id: `question-${request.id}`, questions: [{ id: 'answer', type: 'free_text', question: `Datei ${request.id}?` }] }))
    api.get.mockImplementation(async (url) => ({ data: url === '/approvals/pending' ? pending : pendingQuestions }))
    api.post.mockResolvedValue({ data: {} })
    await approvals.fetchPending()
    await questions.fetchPending()
    const Parent = defineComponent({ setup: () => {
      const transcript = computed(() => buildChatTranscript([], approvals.forSession('parent'), { running: chat.running, questions: questions.forSession('parent') }))
      return () => h('main', [
        h('p', { role: 'status' }, agentRunPresentation({ runId: chat.runId, running: chat.running, activity: chat.activity, transcript: transcript.value }).label),
        ...transcript.value.unmatchedApprovals.map((approval) => h(ApprovalCard, { key: approval.id, approval })),
        ...questions.forSession('parent').slice(0, 1).map((request) => h(QuestionDock, { key: request.id, request }))
      ])
    } })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'chat', component: Parent }] })
    await router.push('/?session=parent')
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({ render: () => h(RouterView) })
    app.use(pinia).use(router).use(Quasar, { components })
    mountApp()
    expect(root.querySelector('[role="status"]').textContent).toBe('Subagenten beauftragen')
    expect(root.querySelectorAll('.approval-card')).toHaveLength(2)
    expect(root.querySelectorAll('.question-dock')).toHaveLength(1)
    for (const id of ['one', 'two']) {
      expect(root.querySelector(`a[href="/?session=parent&subagent=child-${id}"]`).textContent).toContain(`Auftrag ${id}`)
    }
    const cards = root.querySelectorAll('.approval-card')
    const allow = [...cards[0].querySelectorAll('button')].find((button) => button.textContent.includes('Für diesen Lauf erlauben'))
    allow.click()
    await vi.waitFor(() => expect(root.querySelectorAll('.approval-card')).toHaveLength(1))
    expect(root.querySelector('.approval-card').textContent).toContain('Auftrag two')
    expect(api.post).toHaveBeenCalledWith('/approvals/one/decisions', { decision: 'allow_once' })
    const answer = [...root.querySelector('.question-dock').querySelectorAll('button')].find((button) => button.textContent === 'Fertig')
    answer.click()
    await vi.waitFor(() => expect(root.querySelector('.question-dock').textContent).toContain('Auftrag two'))
    expect(api.post).toHaveBeenCalledWith('/questions/question-one/answers', { answers: { answer: [] } })
    expect(chat.running).toBe(true)
    expect(chat.runId).toBe('parent-run')
    expect(root.querySelector('[role="status"]').textContent).toBe('Subagenten beauftragen')
  })
})
