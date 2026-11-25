// @vitest-environment jsdom
import { createApp, h, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Quasar, QAvatar, QBanner, QBtn, QCard, QCardActions, QCardSection, QCheckbox, QChip, QDialog, QExpansionItem, QForm, QIcon, QInput, QItem, QItemLabel, QItemSection, QList, QScrollArea, QSelect, QSeparator, QSkeleton, QSpace, QSpinner, QSpinnerGrid, QToggle, QTooltip } from 'quasar'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }))
vi.mock('src/features/chat/components/ChatComposer.vue', () => ({ default: { methods: { focus() {} }, render: () => null } }))
vi.mock('src/app/control-center/ControlCenterPanel.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/chat/components/MessageContent.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/chat/components/ChatFiles.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/home/HomeContent.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/chat/components/ComputerChoiceStrip.vue', () => ({ default: { render: () => null } }))

import ChatPage from '../src/features/chat/ChatPage.vue'
import { api } from 'src/boot/axios'
import { useApprovalStore } from '../src/stores/approvals'
import { useChatStore } from '../src/features/chat/store'
import { useControlCenterStore } from '../src/stores/control-center'
import { useWorkspaceStore } from '../src/stores/workspace'
import { useExecutionAgentStore } from '../src/stores/execution-agents'

let app, root, chat
const initialRevision = 'a'.repeat(64)
const parentRevision = 'b'.repeat(64)
const source = '{"name":"Dateien prüfen","instructions":"Nur lesen"}'

async function mountPage({ actionKind = 'build_skill', rootIdentity, reply, location = '/?session=parent&project=parent-project', chatReply } = {}) {
  vi.stubGlobal('scrollTo', vi.fn())
  const pinia = createPinia()
  setActivePinia(pinia)
  chat = useChatStore()
  chat.outboxLoaded = true
  const approvals = useApprovalStore()
  const workspace = useWorkspaceStore()
  const controlCenter = useControlCenterStore()
  const approval = {
    id: 'skill-approval', sessionId: 'parent', runId: 'child-run', actionKind,
    requestType: 'agent_action', riskLevel: 'low', title: 'Skill speichern',
    availableDecisions: ['allow_once', 'deny'],
    payload: { subagentSessionId: 'child', subagentTitle: 'Skill erstellen' },
    details: {
      skillPreview: { name: 'Dateien prüfen', description: 'Dateien lesen', instructions: 'Nur lesen' },
      draftFile: { root: 'session', path: 'skill-draft.json', filename: 'skill-draft.json', initialRevision, fileRevision: initialRevision, ...(rootIdentity ? { rootIdentity } : {}) }
    }
  }
  api.post.mockImplementation(async (url) => url === '/assistant/chat'
    ? chatReply ? await chatReply() : { data: { sessionId: 'personal' } }
    : { data: {} })
  api.get.mockImplementation(async (url) => {
    if (url === '/workspace/files/preview') return reply ? await reply() : { data: { kind: 'text', content: source, revision: initialRevision } }
    if (url === '/sessions/parent') return { data: { id: 'parent', projectId: 'parent-project', messages: [] } }
    if (url === '/sessions/personal') return { data: { id: 'personal', projectId: null, messages: [] } }
    if (url === '/approvals/pending') return { data: [approval] }
    if (url === '/mail/accounts') return { data: [{ id: 'mail-1' }] }
    if (url === '/questions/pending' || url.endsWith('/children')) return { data: [] }
    if (url === '/execution-agents' || url === '/agent-profiles') return { data: [] }
    return { data: { items: [] } }
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'chat', component: ChatPage }] })
  await router.push(location)
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(ChatPage) })
  app.use(pinia).use(router).use(Quasar, { components: { QAvatar, QBanner, QBtn, QCard, QCardActions, QCardSection, QCheckbox, QChip, QDialog, QExpansionItem, QForm, QIcon, QInput, QItem, QItemLabel, QItemSection, QList, QScrollArea, QSelect, QSeparator, QSkeleton, QSpace, QSpinner, QSpinnerGrid, QToggle, QTooltip } })
  app.component('QPage', { render() { return h('div', this.$slots.default?.()) } })
  app.component('QSplitter', { render() { return h('div', this.$slots.before?.()) } })
  app.component('QVirtualScroll', { render() { return h('div', this.$slots.after?.()) } })
  app.mount(root)
  if (location.includes('session=parent')) {
    await vi.waitFor(() => expect(root.querySelector('.approval-card')).not.toBeNull())
  } else await nextTick()
  workspace.sessionId = 'parent'
  workspace.rootIdentity = 'parent'
  workspace.scope = 'session'
  workspace.viewedEntry = { path: 'skill-draft.json' }
  workspace.editorActive = true
  workspace.editorDirty = true
  workspace.editorDraft = 'Unsaved parent draft'
  workspace.filePreviews = { 'skill-draft.json': { content: 'Parent file', revision: parentRevision } }
  await nextTick()
  return {
    approval, approvals, workspace, router,
    preview: vi.spyOn(approvals, 'previewSkillDraft'),
    openFile: vi.spyOn(controlCenter, 'openFile'),
    startEdit: vi.spyOn(workspace, 'startFileTabEdit')
  }
}

function click(label, container = root) {
  const button = [...container.querySelectorAll('button')].find((item) => item.textContent.includes(label) || item.getAttribute('aria-label') === label)
  expect(button, label).toBeTruthy()
  expect(button.disabled).toBe(false)
  button.click()
}

afterEach(() => {
  app?.unmount()
  chat?.cancelQueueDrain()
  root?.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

it('does not load mail accounts when opening a chat without the Control Center', async () => {
  await mountPage()
  expect(api.get.mock.calls.map(([url]) => url)).not.toContain('/mail/accounts')
})

it('adds Git when repository detection finishes after a new session has attached', async () => {
  await mountPage()
  const controlCenter = useControlCenterStore()
  const add = vi.spyOn(controlCenter, 'ensureApp').mockResolvedValue()
  await chat.loadSession(null)
  await controlCenter.attachCreatedSession('created', controlCenter)
  useExecutionAgentStore().items = [{ id: 'computer', connected: true, operations: ['git.changes'] }]
  api.get.mockResolvedValue({ data: { repository: '/worktree', files: [], truncated: false } })
  chat.selectedSessionId = 'created'
  chat.current = { id: 'created', projectId: 'project', preferredExecutionAgentId: 'computer', currentDirectory: '/worktree', messages: [] }
  await vi.waitFor(() => expect(add).toHaveBeenCalledWith('git'))
})

describe('personal chat entry', () => {
  it('opens the personal session from its dedicated entry and reuses its known ID', async () => {
    const { router } = await mountPage({ location: '/?personal' })
    await vi.waitFor(() => expect(chat.current?.id).toBe('personal'))
    expect(router.currentRoute.value.query).toEqual({ session: 'personal' })
    expect(chat.personalChatSessionId).toBe('personal')
    await router.push('/?personal')
    await vi.waitFor(() => expect(router.currentRoute.value.query.session).toBe('personal'))
    expect(api.post.mock.calls.filter(([url]) => url === '/assistant/chat')).toHaveLength(1)
  })

  it.each(['/', '/?new', '/?project=project-draft', '/?session=parent&project=parent-project'])(
    'preserves the explicit route %s', async (location) => {
      const { router } = await mountPage({ location })
      await nextTick()
      expect(router.currentRoute.value.fullPath).toBe(location)
      expect(api.post).not.toHaveBeenCalledWith('/assistant/chat')
    }
  )

  it.each(['/', '/?new', '/?project=project-draft', '/?session=parent&project=parent-project'])(
    'does not let a late personal-chat response replace %s', async (location) => {
      let finish
      const { router } = await mountPage({ location: '/?personal', chatReply: () => new Promise(resolve => { finish = resolve }) })
      await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
      await router.push(location)
      finish({ data: { sessionId: 'personal' } })
      await new Promise(resolve => setTimeout(resolve, 0))
      await nextTick()
      expect(router.currentRoute.value.fullPath).toBe(location)
    }
  )
})

describe('child skill draft previews', () => {
  it.each([
    ['build_skill', undefined],
    ['edit_skill', 'parent-project']
  ])('opens %s from its child session read-only (explicit root=%s)', async (actionKind, rootIdentity) => {
    const { approval, workspace, preview, openFile, startEdit } = await mountPage({ actionKind, rootIdentity })
    const parentPreviews = workspace.filePreviews
    click('Entwurf ansehen')
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"] pre')?.textContent).toBe(source))
    expect(preview).toHaveBeenCalledWith(expect.objectContaining({ id: approval.id, payload: approval.payload }))
    expect(api.get).toHaveBeenCalledWith('/workspace/files/preview', {
      params: { scope: 'session', session_id: 'child', path: 'skill-draft.json', ...(rootIdentity ? { root_identity: rootIdentity } : {}) }
    })
    const dialog = document.querySelector('[role="dialog"]')
    expect(dialog.textContent).toContain('Nur Lesezugriff')
    expect(dialog.querySelector('input, textarea, [contenteditable="true"]')).toBeNull()
    expect(dialog.textContent).not.toMatch(/Bearbeiten|Speichern/)
    expect(openFile).not.toHaveBeenCalled()
    expect(startEdit).not.toHaveBeenCalled()
    expect(api.patch).not.toHaveBeenCalled()
    expect(workspace.sessionId).toBe('parent')
    expect(workspace.rootIdentity).toBe('parent')
    expect(workspace.filePreviews).toBe(parentPreviews)
    expect(workspace.editorDraft).toBe('Unsaved parent draft')
    expect(workspace.editorDirty).toBe(true)
    click('Entwurfsvorschau schließen', dialog)
    await nextTick()
    click('Für diesen Lauf erlauben')
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/approvals/skill-approval/decisions', {
      decision: 'allow_once', expected_revision: initialRevision
    }))
  })

  it('does not present changed bytes as the initially approved draft', async () => {
    await mountPage({ reply: async () => ({ data: { kind: 'text', content: 'Changed source', revision: parentRevision } }) })
    click('Entwurf ansehen')
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"] [role="alert"]')).not.toBeNull())
    expect(document.querySelector('[role="dialog"]').textContent).toContain('Die Datei wurde seit dem Öffnen geändert')
    expect(document.querySelector('[role="dialog"]').textContent).not.toContain('Changed source')
  })

  it('can approve the initial inline skill preview without opening the draft file', async () => {
    const { preview } = await mountPage()
    click('Für diesen Lauf erlauben')
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/approvals/skill-approval/decisions', {
      decision: 'allow_once', expected_revision: initialRevision
    }))
    expect(preview).not.toHaveBeenCalled()
  })

  it('ignores late preview results after the dialog closes', async () => {
    let resolve
    const { preview } = await mountPage({ reply: () => new Promise((done) => { resolve = done }) })
    click('Entwurf ansehen')
    await vi.waitFor(() => expect(preview).toHaveBeenCalledOnce())
    click('Entwurfsvorschau schließen', document.querySelector('[role="dialog"]'))
    resolve({ data: { kind: 'text', content: source, revision: initialRevision } })
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeNull())
    expect(root.textContent).not.toContain(source)
  })
})
