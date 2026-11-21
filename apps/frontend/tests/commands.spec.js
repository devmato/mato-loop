// @vitest-environment jsdom
import { createApp, h, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar, Dialog, Notify } from 'quasar'
import { quasarOptions } from './quasar-options'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))
vi.mock('src/composables/confirm', () => ({ useConfirm: vi.fn(() => ({ confirm: vi.fn().mockResolvedValue(true) })) }))

import GitActionList from '../src/features/coding/GitActionList.vue'
import GitApp from '../src/features/coding/GitApp.vue'
import GitWorkspacePanel from '../src/features/coding/GitWorkspacePanel.vue'
import WorkspaceField from '../src/features/coding/WorkspaceField.vue'
import { useGitActionStore } from '../src/features/coding/git-actions-store'
import { sessionCreatePayload } from '../src/features/chat/state'
import { useCommandStore } from '../src/features/commands/store'
import CommandList from '../src/features/commands/CommandList.vue'
import { useChatStore } from '../src/features/chat/store'
import { useExecutionAgentStore } from '../src/features/computers/store'
import { useProjectStore } from '../src/features/projects/store'
import { useControlCenterStore } from '../src/app/control-center/store'
import { useRepositoryProbe } from '../src/features/coding/repository'
import { useGitChangesStore } from '../src/features/files/git-store'
import { api } from 'src/boot/axios'
import { apiErrorMessage } from 'src/utils/api-errors'
import { useSidebarSessions } from '../src/features/chat/useSidebarSessions'
import { chooseDeletion } from '../src/features/archive/deletion-choice'
import { useWorkspaceStore } from '../src/stores/workspace'
import { useConfirm } from 'src/composables/confirm'

let app, root
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.clearAllMocks(); vi.useRealTimers() })

it.each([false, null])('persists the Git fetch override without losing false or inheritance (%s)', async fetchBeforeActions => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', codingWorkspace: { fetchBeforeActions: true } }
  api.put.mockResolvedValue({ data: { codingWorkspace: { fetchBeforeActions } } })
  await useGitActionStore().setFetchBeforeActions(fetchBeforeActions)
  expect([api.put.mock.calls, chat.current.codingWorkspace.fetchBeforeActions]).toEqual([
    [['/coding/sessions/session/target', { fetchBeforeActions }]], fetchBeforeActions
  ])
})

it('hides a directly opened personal chat after restoring its binding', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  Object.assign(chat.ensureSessionList(), { items: [{ id: 'regular' }], loaded: true, nextCursor: 'next' })
  chat.applySessionResult({ id: 'personal', projectId: null, messages: [] })
  api.get.mockResolvedValueOnce({ data: { sessionId: 'personal' } })
  await chat.fetchPersonalChatBinding()
  const sidebar = useSidebarSessions({ chat, projects: { items: [] } })
  expect(sidebar.standaloneSessionList.value.items.map(item => item.id)).toEqual(['regular'])
})

function mountPanel(props, session = {}, router) {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', preferredExecutionAgentId: 'agent', currentDirectory: '/repo', ...session }
  useExecutionAgentStore().items = [{ id: 'agent', connected: true, operations: ['git.branch'] }]
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitWorkspacePanel, {
    project: { defaultExecutionAgentId: 'agent', defaultCurrentDirectory: '/repo' }, ...props
  }) })
  if (router) app.use(router)
  app.use(pinia).use(Quasar, { ...quasarOptions, plugins: { Dialog, Notify } }).mount(root)
  return chat
}

it('does not replace a new context catalog with a late previous response', async () => {
  setActivePinia(createPinia())
  const commands = useCommandStore()
  let resolvePrevious
  api.post.mockImplementationOnce(() => new Promise(resolve => { resolvePrevious = resolve }))
    .mockResolvedValueOnce({ data: { items: [{ name: 'current-task' }] } })
  const previous = commands.load({ session_id: 'previous' })
  await commands.load({ session_id: 'current' })
  resolvePrevious({ data: { items: [{ name: 'previous-task' }] } })
  await previous
  expect(commands.items.map(command => command.name)).toEqual(['current-task'])
})

it('keeps the open palette and selection stable when project polling only updates statistics', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  useChatStore().current = { id: 'session', projectId: 'project' }
  const projects = useProjectStore()
  const project = { id: 'project', name: 'Projekt', gitTargetBranch: 'main', sessionCount: 1, hasUnreadMessages: false, updatedAt: 'before' }
  projects.items = [project]
  api.post.mockResolvedValue({ data: { items: [
    { name: 'first-task', description: 'Erster Auftrag' }, { name: 'selected-task', description: 'Gewählter Auftrag' }
  ] } })
  api.get.mockResolvedValue({ data: { items: [{ ...project, sessionCount: 2, hasUnreadMessages: true, updatedAt: 'after' }] } })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(CommandList, { id: 'palette' }) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  await vi.waitFor(() => expect(root.querySelectorAll('[role="option"]')).toHaveLength(2))
  root.querySelectorAll('[role="option"]')[1].dispatchEvent(new MouseEvent('mouseenter'))
  await nextTick()
  await projects.fetchAll()
  await projects.fetchAll()
  await nextTick()
  expect([api.post.mock.calls.length, root.querySelector('[aria-selected="true"]')?.textContent.includes('/selected-task')])
    .toEqual([1, true])
})

it('queues a selected command during a run without consuming its draft', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const commands = useCommandStore()
  commands.loaded = true
  commands.items = [{ source: 'git', name: 'git-review', label: 'Code-Review', description: 'Review' }]
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', messages: [], agentProfileId: 'stored', preferredExecutionAgentId: 'agent', currentDirectory: '/repo' }
  chat.drafts.sessionAgentProfiles.session = 'selected'
  useExecutionAgentStore().items = [{ id: 'agent', connected: true }]
  chat.running = true
  chat.runId = 'active-run'
  chat.queueDrainPromise = Promise.resolve()
  const send = vi.spyOn(chat, 'send').mockResolvedValue({})
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitActionList) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  await nextTick()
  root.querySelector('.q-item--clickable').click()
  expect(send).toHaveBeenCalledWith('/git-review', { preserveDraft: true, activeFile: null })
})

it('allows removing a queued finish command without a connected computer or repository catalog', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', projectId: 'project', preferredExecutionAgentId: 'agent', currentDirectory: '/repo' }
  chat.queuedMessages.session = [{ id: 'finish', content: '/git-finish' }]
  useProjectStore().items = [{ id: 'project', name: 'Projekt' }]
  useExecutionAgentStore().items = [{ id: 'agent', connected: false }]
  const remove = vi.spyOn(chat, 'removeQueuedMessage').mockImplementation(async () => {
    chat.queuedMessages.session = []
  })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitApp) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  ;[...root.querySelectorAll('[role="tab"]')].find(tab => tab.textContent === 'Repository').click()
  await vi.waitFor(() => expect(root.querySelector('[aria-labelledby="git-settings-title"] [aria-label="Abschluss einplanen"]')
    ?.closest('.git-workspace-footer')?.textContent).toContain('Projektvorgaben'))
  root.querySelector('[aria-label="Abschluss einplanen"]').click()
  await vi.waitFor(() => expect(remove.mock.calls).toEqual([['finish']]))
})

it('queues publication without a remote or a connected computer in the Git app', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const commands = useCommandStore(), chat = useChatStore()
  commands.loaded = true
  commands.items = [{ source: 'git', name: 'git-sync', label: 'Pull (Fast-forward)', disabledReason: 'Remote einrichten, um zu veröffentlichen.' }, { source: 'git', name: 'git-remote', label: 'Remote einrichten' }]
  chat.outboxLoaded = true
  chat.current = { id: 'session', preferredExecutionAgentId: 'agent', currentDirectory: '/repo', workspaceMode: 'local' }
  useExecutionAgentStore().items = [{ id: 'agent', connected: false }]
  const send = vi.spyOn(chat, 'send').mockResolvedValue({})
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitApp) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  ;[...root.querySelectorAll('[role="tab"]')].find(tab => tab.textContent === 'Repository').click()
  await nextTick()
  root.querySelector('.q-expansion-item > .q-expansion-item__container > .q-item').click()
  await nextTick()
  ;[...root.querySelectorAll('.q-item--clickable')].find(item => item.textContent.includes('Pull (Fast-forward)')).click()
  expect(send).toHaveBeenCalledWith('/git-sync', { preserveDraft: true, activeFile: null })
})

it.each(['git-merge', 'git-pr'])('submits the default integration action without commit options (%s)', async name => {
  const pinia = createPinia()
  setActivePinia(pinia)
  useCommandStore().items = ['git-review', 'git-commit', 'git-merge', 'git-finish', 'git-pr', 'git-push', 'git-sync']
    .map(name => ({ name, label: name, source: 'git' }))
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', messages: [], preferredExecutionAgentId: 'agent', currentDirectory: '/repo' }
  const send = vi.spyOn(chat, 'send').mockResolvedValue({})
  root = document.createElement('div')
  app = createApp({ render: () => h(GitActionList) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  if (name === 'git-merge') {
    root.querySelector('.q-expansion-item > .q-expansion-item__container > .q-item').click()
    await nextTick()
  }
  const action = [...root.querySelectorAll('.q-item--clickable')].find(item => item.querySelector('.q-item__label').textContent === name)
  action.click()
  expect(send).toHaveBeenCalledWith(`/${name}`, { preserveDraft: true, activeFile: null })
})

it('shows four primary Git actions and expands the individual actions', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  useChatStore().outboxLoaded = true
  useCommandStore().items = ['git-init', 'git-review', 'git-commit', 'git-merge', 'git-finish', 'git-pr', 'git-push', 'git-sync']
    .map(name => ({ name, label: name, source: 'git' }))
  root = document.createElement('div')
  app = createApp({ render: () => h(GitActionList) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  expect([...root.querySelectorAll('.q-list > .q-item--clickable')].map(item => item.querySelector('.q-item__label').textContent))
    .toEqual(['git-review', 'git-finish', 'git-push', 'git-pr'])
  expect(root.querySelector('.q-expansion-item > .q-expansion-item__container > .q-item').getAttribute('aria-expanded')).toBe('false')
  root.querySelector('.q-expansion-item > .q-expansion-item__container > .q-item').click()
  await nextTick()
  expect([
    [...root.querySelectorAll('.q-item--clickable:not(label)')].map(item => item.querySelector('.q-item__label').textContent),
    [...root.querySelectorAll('[role="checkbox"]')].map(item => item.getAttribute('aria-label'))
  ]).toEqual([
    ['git-review', 'git-finish', 'git-push', 'git-pr', 'Weitere Git-Aktionen', 'git-commit', 'git-merge', 'git-sync'],
    []
  ])
})

it('offers repository initialization in settings rather than daily actions', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', preferredExecutionAgentId: 'agent', currentDirectory: '/folder' }
  useCommandStore().items = [{ name: 'git-init', label: 'Git-Repository anlegen', source: 'git' }]
  const run = vi.spyOn(useGitActionStore(), 'run').mockResolvedValue('session')
  root = document.createElement('div')
  app = createApp({ render: () => h(GitApp) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  ;[...root.querySelectorAll('[role="tab"]')].find(tab => tab.textContent === 'Repository').click()
  await nextTick()
  root.querySelector('[aria-labelledby="git-settings-title"] .q-item--clickable').click()
  expect([root.querySelector('[aria-label="Agentenaufträge"]').textContent, run.mock.calls]).toEqual(['', [['git-init']]])
})

it('does not recheck a repository when computer polling replaces unchanged agents', async () => {
  vi.useFakeTimers()
  const pinia = createPinia()
  setActivePinia(pinia)
  const agents = useExecutionAgentStore()
  agents.items = [{ id: 'agent', connected: true, operations: ['git.repository'] }]
  api.post.mockResolvedValue({ data: { repository: '/repo' } })
  root = document.createElement('div')
  app = createApp({ setup() {
    const { repository } = useRepositoryProbe(() => 'agent', () => '/repo')
    return () => h('span', repository.value?.repository)
  } })
  app.use(pinia).mount(root)
  await vi.advanceTimersByTimeAsync(300)
  agents.items = agents.items.map(agent => ({ ...agent, operations: [...agent.operations] }))
  await nextTick()
  await vi.advanceTimersByTimeAsync(300)
  expect(api.post).toHaveBeenCalledTimes(1)
})

it('adds Git once per new project draft without opening it or creating a session', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore(), controlCenter = useControlCenterStore()
  controlCenter.resetDraft()
  Object.assign(chat.drafts.newSession.options, { preferredExecutionAgentId: 'agent', currentDirectory: '/repo' })
  useProjectStore().items = [{ id: 'project', defaultExecutionAgentId: 'agent', defaultCurrentDirectory: '/repo' }]
  useExecutionAgentStore().items = [{ id: 'agent', connected: true, operations: ['git.repository'] }]
  api.post.mockResolvedValue({ data: { repository: '/repo' } })
  const create = vi.spyOn(chat, 'createEmptySession')
  root = document.createElement('div')
  app = createApp({ render: () => h(WorkspaceField, { projectId: 'project', session: chat.activeContext }) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  await vi.waitFor(() => expect([controlCenter.tabs.some(tab => tab.appId === 'git'), controlCenter.activeAppId, controlCenter.open])
    .toEqual([true, 'session', false]))
  await controlCenter.removeIntegration('git')
  useProjectStore().items = [...useProjectStore().items]
  await nextTick()
  expect([controlCenter.open, controlCenter.tabs.some(tab => tab.appId === 'git'), create.mock.calls.length])
    .toEqual([false, false, 0])
  controlCenter.resetDraft()
  await vi.waitFor(() => expect(controlCenter.tabs.some(tab => tab.appId === 'git')).toBe(true))
})

function openDeletionDialog(action, trashEnabled) {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  api.get.mockImplementation(async () => ({ data: { trashEnabled, trashRetentionDays: 7 } }))
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h('div') })
  app.use(Quasar, { ...quasarOptions, plugins: { Dialog } }).mount(root)
  return action(app.config.globalProperties.$q)
}

const deletionDialog = () => document.querySelector('.q-dialog')
const deletionButtons = () => [...deletionDialog().querySelectorAll('button')]
const deletionHeading = () => deletionDialog()?.querySelector('[role="heading"]')?.textContent
const deletionAction = () => deletionButtons()[1].querySelector('[aria-hidden="false"]').textContent
const worktreeWarning = () => deletionDialog().querySelector('[aria-hidden="false"] > [role="status"] > [aria-hidden="false"]')?.textContent
const permanentCheckbox = () => deletionDialog().querySelector('[aria-label="Sofort endgültig löschen"]')

it.each([
  { name: 'keep by default' }, { name: 'remove', select: true }, { name: 'blocked', blocked: true },
  { name: 'cancel from trash', trash: true, fromTrash: true, cancel: true },
  { name: 'keep despite scheduling', trash: true, fromTrash: true, scheduled: true },
  { name: 'move without scheduling', trash: true }, { name: 'schedule', trash: true, select: true },
  { name: 'cancel move', trash: true, cancel: true }, { name: 'delete immediately', trash: true, select: true, toggle: true }
])('confirms deletion of an unopened session: $name', async ({ trash = false, fromTrash = false, select, blocked, scheduled, cancel, toggle }) => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const impact = 'a'.repeat(64)
  const startsInTrashMode = trash && !fromTrash
  vi.spyOn(chat, 'sessionDeleteImpact').mockResolvedValue({
    ...(startsInTrashMode && !toggle ? { fileReferenceCount: 1, requiredAutomationCount: 1 } : {}),
    worktree: fromTrash && cancel ? null : { path: '/worktree', impact, sessions: [], scheduled,
      removalError: blocked ? { code: 'git.worktreePermissionDenied', params: { path: '/worktree/cache' } } : null }
  })
  const deleted = vi.spyOn(chat, 'deleteSession').mockResolvedValue()
  const removal = openDeletionDialog($q => useSidebarSessions({ chat, projects: useProjectStore(),
    controlCenter: useControlCenterStore(), workspace: {}, automations: { fetchAutomations: vi.fn().mockResolvedValue() },
    route: { query: {} }, $q: { dialog: $q.dialog, notify: vi.fn() }
  }).removeSession({ id: 'other', title: 'Auftrag' }, { permanent: fromTrash }), trash)
  await vi.waitFor(() => expect(deletionHeading()).toBe('Session löschen?'))
  expect(deletionAction()).toBe(startsInTrashMode ? 'In den Papierkorb' : 'Endgültig löschen')
  expect(Boolean(permanentCheckbox())).toBe(startsInTrashMode)
  expect(deletionDialog().textContent).not.toContain('7 Tage')
  const checkbox = deletionDialog().querySelector('[aria-label="Worktree ebenfalls entfernen"]')
  expect(checkbox?.getAttribute('aria-checked')).toBe(fromTrash && cancel ? undefined : String(Boolean(scheduled)))
  if (blocked) {
    expect(checkbox.getAttribute('aria-disabled')).toBe('true')
    expect(deletionDialog().textContent).toContain('/worktree/cache')
  }
  if (select || scheduled) checkbox.click()
  await nextTick()
  if (select) expect(worktreeWarning()).toContain(startsInTrashMode
    ? 'Erst beim endgültigen Löschen.' : 'Nicht committete Änderungen und unversionierte Dateien gehen verloren.')
  if (toggle) {
    permanentCheckbox().click()
    await nextTick()
    expect(worktreeWarning()).not.toContain('Erst beim endgültigen Löschen.')
  }
  expect(deletionHeading()).toBe('Session löschen?')
  expect(deletionAction()).toBe(startsInTrashMode && !toggle ? 'In den Papierkorb' : 'Endgültig löschen')
  deletionButtons()[cancel ? 0 : 1].click()
  await removal
  const permanent = !startsInTrashMode || Boolean(toggle)
  expect(deleted.mock.calls).toEqual(cancel ? [] : [['other', {
    permanent, worktreeImpact: select ? impact : null, keepWorktree: Boolean(scheduled),
    ...(permanent ? { automationMode: null, automationImpact: null } : {})
  }]])
  await vi.waitFor(() => expect(deletionDialog()).toBeNull())
})

it('uses the same confirmation for both project deletion actions', async () => {
  setActivePinia(createPinia())
  const choice = openDeletionDialog($q => chooseDeletion($q, 'Projekt', 'Beispiel'), true)
  await vi.waitFor(() => expect(deletionHeading()).toBe('Projekt löschen?'))
  expect(deletionAction()).toBe('In den Papierkorb')
  permanentCheckbox().click()
  await nextTick()
  expect(deletionHeading()).toBe('Projekt löschen?')
  expect(deletionAction()).toBe('Endgültig löschen')
  deletionButtons()[1].click()
  expect(await choice).toBe(true)
  await vi.waitFor(() => expect(deletionDialog()).toBeNull())
})

it('adopts a draft remote without changing an unrelated project workspace', async () => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore(), commands = useGitActionStore(), projects = useProjectStore()
  chat.outboxLoaded = true
  chat.sessionsProjectId = 'project'
  Object.assign(chat.drafts.newSession.options, { preferredExecutionAgentId: 'agent', currentDirectory: '/repo' })
  projects.items = [{ id: 'project', name: 'Projekt', defaultExecutionAgentId: 'agent', defaultCurrentDirectory: '/other' }]
  useExecutionAgentStore().items = [{ id: 'agent', connected: true, operations: ['git.repository'] }]
  useControlCenterStore().$patch({ activeAppId: 'git' })
  await useControlCenterStore().setOpen(true)
  vi.spyOn(useCommandStore(), 'load').mockResolvedValue()
  const session = { id: 'session', projectId: 'project', preferredExecutionAgentId: 'agent', currentDirectory: '/repo' }
  vi.spyOn(chat, 'createEmptySession').mockImplementation(async () => { chat.current = session; return session })
  const adopt = vi.spyOn(commands, 'updateTarget').mockResolvedValue('session')
  const update = vi.spyOn(projects, 'update')
  api.post.mockResolvedValue({ data: { repository: '/repo', remotes: ['origin'], defaultRemote: 'origin',
    remoteUrls: { origin: 'https://example.com/repo.git' }, remoteIdentities: { origin: 'a'.repeat(64) } } })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitApp) })
  app.use(pinia).use(Quasar, { ...quasarOptions, plugins: { Notify } }).mount(root)
  ;[...root.querySelectorAll('[role="tab"]')].find(tab => tab.textContent === 'Repository').click()
  await vi.waitFor(() => expect(root.querySelector('[aria-label="Remote bearbeiten"]')?.disabled).toBe(false))
  root.querySelector('[aria-label="Remote bearbeiten"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog form')).not.toBeNull())
  document.querySelector('.q-dialog form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await vi.waitFor(() => expect(adopt).toHaveBeenCalledWith({ remote: 'origin', remote_identity: 'a'.repeat(64) }))
  expect(update).not.toHaveBeenCalled()
})

it.each([[false, true], [true, true], [false, false]])('rechecks worktree availability when reopening the selector (initial error: %s, template enabled: %s)', async (initialError, enabled) => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.sessionsProjectId = 'project'
  Object.assign(chat.drafts.newSession.options, { preferredExecutionAgentId: 'agent', currentDirectory: '/repo', sessionProfile: { worktreesEnabled: enabled } })
  useProjectStore().items = [{ id: 'project', defaultExecutionAgentId: 'agent', defaultCurrentDirectory: '/repo' }]
  useExecutionAgentStore().items = [{ id: 'agent', connected: true, operations: ['git.repository', 'git.worktree'] }]
  const info = { repository: '/repo', remotes: [], defaultRemote: null, defaultBranch: 'main', targetHasCommit: false }
  const probeError = { response: { status: 403, data: { error: { code: 'permission.denied' } } } }
  let fail = initialError
  api.post.mockImplementation(async () => {
    if (fail) throw probeError
    return { data: { ...info } }
  })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(WorkspaceField, { session: chat.activeContext, projectId: 'project', onUpdate: patch => chat.updateActive(patch) }) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  root.querySelector('[role="combobox"]').click()
  if (!enabled) {
    await vi.waitFor(() => expect(document.body.textContent).toContain(apiErrorMessage({ error: { code: 'sessionTemplate.worktreesDisabled' } })))
    expect([...document.querySelectorAll('.q-item')].find(item => item.textContent.includes('Worktree')).getAttribute('aria-disabled')).toBe('true')
    return
  }
  await vi.waitFor(() => expect(document.body.textContent).toContain(initialError ? apiErrorMessage(probeError) : 'Der lokale Zielbranch benötigt einen Commit'))
  expect([...document.querySelectorAll('.q-item')].find(item => item.textContent.includes('Worktree')).getAttribute('aria-disabled')).toBe('true')
  info.targetHasCommit = true
  fail = false
  ;[...document.querySelectorAll('.q-item')].find(item => item.textContent.includes('Lokal')).click()
  await vi.waitFor(() => expect(document.querySelector('.q-menu')).toBeNull())
  root.querySelector('[role="combobox"]').click()
  await vi.waitFor(() => expect([...document.querySelectorAll('.q-item')].find(item => item.textContent.includes('Worktree')).getAttribute('aria-disabled')).not.toBe('true'))
  ;[...document.querySelectorAll('.q-item')].find(item => item.textContent.includes('Worktree')).click()
  await vi.waitFor(() => expect(chat.activeContext.workspaceMode).toBe('worktree'))
})

it('keeps the standalone workspace information read-only for existing sessions even offline', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore()
  chat.outboxLoaded = true
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(WorkspaceField, { session: chat.activeContext }) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  expect(root.textContent).toContain('Nicht verfügbar')
  chat.current = { id: 'current', preferredExecutionAgentId: 'agent', currentDirectory: '/repo', workspaceMode: 'worktree' }
  useExecutionAgentStore().items = [{ id: 'agent', connected: false }]
  await nextTick()
  expect([root.textContent.includes('Worktree'), root.querySelector('[role="combobox"]')]).toEqual([true, null])
})

it('groups existing worktrees below local and new without repeating their type', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  useProjectStore().items = [{ id: 'project', defaultExecutionAgentId: 'agent', defaultCurrentDirectory: '/repo' }]
  useExecutionAgentStore().items = [{ id: 'agent', connected: true, operations: ['git.repository', 'git.worktree'] }]
  api.post.mockResolvedValue({ data: { repository: '/repo', defaultBranch: 'main', worktrees: [
    { path: '/existing', branch: 'task' }, { path: '/repo', branch: 'main' }, { path: '/other', branch: 'feature' }
  ] } })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(WorkspaceField, { projectId: 'project', session: {
    preferredExecutionAgentId: 'agent', currentDirectory: '/repo', workspaceMode: 'local', sessionProfile: { worktreesEnabled: true }
  } }) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  root.querySelector('[role="combobox"]').click()
  await vi.waitFor(() => expect([...document.querySelectorAll('[role="listbox"] [role="option"], [role="listbox"] hr')]
    .map(item => item.tagName === 'HR' ? 'separator' : item.querySelector('.q-item__section--main').textContent))
    .toEqual(['Lokal', 'Neuer Worktree', 'separator', 'existing · task', 'other · feature']))
})

it('uses an existing worktree at session creation without requesting another worktree', () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  const options = { ...chat.activeContext, preferredExecutionAgentId: 'agent', currentDirectory: '/repo', workspaceMode: 'worktree', workspacePath: '/existing', gitFetchBeforeActions: false }
  const payload = sessionCreatePayload(options, 'project', {})
  expect([payload.workspace_path, payload.git_fetch_before_actions]).toEqual(['/existing', false])
})

it('keeps the selected workspace unchanged after a failed switch', async () => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', currentDirectory: '/original' }
  api.put.mockRejectedValueOnce(new Error('blocked'))
  await expect(useGitActionStore().workspaceAction('select', { directory: '/existing' })).rejects.toThrow('blocked')
  expect(chat.current.currentDirectory).toBe('/original')
})

it.each(['/repo/file.py', '/other/file.py'])('protects only dirty editors in the affected checkout (%s)', async path => {
  setActivePinia(createPinia())
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', preferredExecutionAgentId: 'agent', currentDirectory: '/repo/src' }
  useControlCenterStore().tabs = [{ kind: 'file', tabId: 'dirty', fileTarget: { scope: 'filesystem', executionAgentId: 'agent', path } }]
  useWorkspaceStore().fileTabStates.dirty = { editorDirty: true }
  vi.spyOn(chat, 'refreshSession').mockResolvedValue()
  api.post.mockResolvedValue({ data: {} })
  await useGitActionStore().workspaceAction('branch-switch', { name: 'task' }, '/repo')
  expect(api.post.mock.calls.length).toBe(path.startsWith('/repo/') ? 0 : 1)
})

it('creates a branch from the workspace panel using the standard prompt', async () => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', projectId: 'project', preferredExecutionAgentId: 'agent', currentDirectory: '/repo' }
  chat.running = true
  useWorkspaceStore().editorDirty = true
  useProjectStore().items = [{ id: 'project', defaultExecutionAgentId: 'agent', defaultCurrentDirectory: '/repo' }]
  useExecutionAgentStore().items = [{ id: 'agent', connected: true, operations: ['git.repository', 'git.branch'] }]
  useControlCenterStore().$patch({ activeAppId: 'git' })
  await useControlCenterStore().setOpen(true)
  vi.spyOn(useCommandStore(), 'load').mockResolvedValue()
  vi.spyOn(chat, 'refreshSession').mockResolvedValue()
  api.post.mockResolvedValue({ data: { repository: '/repo', branch: 'main', localBranches: ['main'],
    worktrees: [{ path: '/repo', branch: 'main', main: true, locked: false }] } })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitApp) })
  app.use(pinia).use(Quasar, { ...quasarOptions, plugins: { Dialog, Notify } }).mount(root)
  const tabs = [...root.querySelectorAll('[role="tab"]')]
  expect(tabs.map(tab => tab.textContent)).toEqual(['Änderungen', 'Repository'])
  tabs[1].click()
  await vi.waitFor(() => expect(root.querySelector('[aria-label="Branch bearbeiten"]')?.disabled).toBe(false))
  expect([...root.querySelectorAll('h2')].map(heading => heading.textContent)).toEqual(['Aktionen', 'Einstellungen'])
  root.querySelector('[aria-label="Branch bearbeiten"]').click()
  await vi.waitFor(() => expect(document.querySelector('[aria-label="Branch erstellen"]')).not.toBeNull())
  document.querySelector('[aria-label="Branch erstellen"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog input[maxlength="300"]')).not.toBeNull())
  const input = document.querySelector('.q-dialog input[maxlength="300"]')
  input.value = 'task'
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  ;[...document.querySelectorAll('.q-dialog button')].find(button => button.textContent === 'Speichern').click()
  await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/coding/sessions/session/git', {
    action: 'branch-create', directory: '/repo', name: 'task'
  }))
})

it('keeps branch inspection searchable and closable during a run', async () => {
  const chat = mountPanel({ repository: { repository: '/repo', localBranches: ['main', 'task'] } })
  chat.running = true
  root.querySelector('[aria-label="Branch bearbeiten"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog input')).not.toBeNull())
  const search = document.querySelector('.q-dialog input')
  search.value = 'task'
  search.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  document.querySelector('[aria-label="Schließen"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog')).toBeNull())
})

it('shows where an occupied branch is checked out and switches only after confirmation', async () => {
  mountPanel({
    repository: { repository: '/repo', branch: 'main', localBranches: ['main', 'occupied', 'free'], worktrees: [
      { path: '/repo', branch: 'main', main: true }, { path: '/other', branch: 'occupied' }
    ] }
  })
  const select = vi.spyOn(useGitActionStore(), 'workspaceAction').mockResolvedValue(true)
  root.querySelector('[aria-label="Branch bearbeiten"]').click()
  await vi.waitFor(() => expect(document.querySelector('[aria-label="Branch occupied markieren"]')).not.toBeNull())
  document.querySelector('[aria-label="Branch occupied markieren"]').click()
  await nextTick()
  const locationVisible = document.querySelector('.q-dialog').textContent.includes('Ausgecheckt in: /other')
  const blocked = document.querySelector('[aria-label="Branch wechseln"]').disabled
  const search = document.querySelector('.q-dialog input')
  search.value = 'free'
  search.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  document.querySelector('[aria-label="Branch free markieren"]').click()
  await nextTick()
  const rowActions = select.mock.calls.length
  document.querySelector('[aria-label="Branch wechseln"]').click()
  await vi.waitFor(() => expect([locationVisible, blocked, rowActions, select.mock.calls]).toEqual([
    true, true, 0, [['branch-switch', { name: 'free' }, '/repo']]
  ]))
  await nextTick()
  root.querySelector('[aria-label="Arbeitsbereich bearbeiten"]').click()
  await vi.waitFor(() => expect([...document.querySelectorAll('.q-dialog .q-list > .q-item')]
    .map(item => item.querySelector('.q-item__label').textContent)).toEqual(['Lokal', 'other']))
})

it('lists the current workspace first and only activates a workspace through its menu', async () => {
  mountPanel({
    repository: { repository: '/worktrees/selected', worktrees: ['/worktrees/first', '/repo', '/worktrees/selected'].map(path => ({ path, main: path === '/repo' })) }
  }, { currentDirectory: '/worktrees/selected', sessionProfile: { worktreesEnabled: true } })
  const select = vi.spyOn(useGitActionStore(), 'workspaceAction').mockResolvedValue(true)
  root.querySelector('[aria-label="Arbeitsbereich bearbeiten"]').click()
  await vi.waitFor(() => expect([...document.querySelector('.q-dialog .q-list').children]
    .map(item => item.tagName === 'HR' ? 'separator' : item.querySelector('.q-item__label').textContent))
    .toEqual(['selected', 'separator', 'Lokal', 'separator', 'first']))
  const first = document.querySelector('[aria-label="Arbeitsbereich /worktrees/first markieren"]')
  const parentPath = first.querySelector('.q-item__label--caption').textContent
  first.click()
  await nextTick()
  const rowActions = select.mock.calls.length
  document.querySelector('[aria-label="Aktionen für Worktree first"]').click()
  await vi.waitFor(() => expect(document.querySelector('[aria-label="Arbeitsbereich /worktrees/first aktivieren"]')).not.toBeNull())
  document.querySelector('[aria-label="Arbeitsbereich /worktrees/first aktivieren"]').click()
  await vi.waitFor(() => expect([parentPath, rowActions, select.mock.calls]).toEqual(['/worktrees/', 0, [['select', { directory: '/worktrees/first', create: false }, '/worktrees/selected']]]))
})

it.each([false, true])('asks before leaving a detached HEAD and respects the decision (%s)', async accepted => {
  const confirm = vi.fn().mockResolvedValue(accepted)
  vi.mocked(useConfirm).mockReturnValueOnce({ confirm })
  mountPanel({ repository: { repository: '/repo', branch: null, localBranches: ['task'] } })
  const select = vi.spyOn(useGitActionStore(), 'workspaceAction').mockResolvedValue(true)
  root.querySelector('[aria-label="Branch bearbeiten"]').click()
  await vi.waitFor(() => expect(document.querySelector('[aria-label="Branch task markieren"]')).not.toBeNull())
  document.querySelector('[aria-label="Branch task markieren"]').click()
  await nextTick()
  document.querySelector('[aria-label="Branch wechseln"]').click()
  await vi.waitFor(() => expect(confirm).toHaveBeenCalledWith(
    expect.stringContaining('Commits des losgelösten Stands, die durch keinen Branch gesichert sind, können danach verloren gehen.'),
    'Losgelösten Stand verlassen', { okLabel: 'Wechseln' }
  ))
  await nextTick()
  expect(select.mock.calls).toEqual(accepted ? [['branch-switch', { name: 'task' }, '/repo']] : [])
})

it('edits the target separately from branch switching', async () => {
  const chat = mountPanel({
    repository: { repository: '/repo', branch: 'main', localBranches: ['main', 'develop'] }, targetBranch: 'main'
  })
  useProjectStore().items = [{ id: 'project', gitRemote: 'origin' }]
  api.put.mockResolvedValue({ data: { codingWorkspace: { targetBranch: 'develop' }, projectId: 'project' } })
  root.querySelector('[aria-label="Zielbranch bearbeiten"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog [role="combobox"]')).not.toBeNull())
  document.querySelector('.q-dialog [role="combobox"]').click()
  await vi.waitFor(() => expect([...document.querySelectorAll('[role="option"]')].find(item => item.textContent.includes('develop'))).toBeDefined())
  ;[...document.querySelectorAll('[role="option"]')].find(item => item.textContent.includes('develop')).click()
  await nextTick()
  document.querySelector('.q-dialog form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await vi.waitFor(() => expect([api.put.mock.calls, chat.current.codingWorkspace, useProjectStore().items[0].gitRemote]).toEqual([
    [['/coding/sessions/session/target', { target_branch: 'develop' }]], { targetBranch: 'develop' }, 'origin'
  ]))
})

it('removes an inactive worktree from the workspace dialog without selecting it', async () => {
  const chat = mountPanel({ project: { defaultExecutionAgentId: 'other-agent', defaultCurrentDirectory: '/other' },
    repository: { repository: '/repo', branch: 'main', localBranches: ['main'],
    worktrees: [{ path: '/repo', branch: 'main', main: true, locked: false },
      { path: '/worktrees/task', branch: null, main: false, locked: false }] } })
  vi.spyOn(chat, 'refreshSession').mockResolvedValue()
  api.post.mockResolvedValue({ data: { ok: true } })
  root.querySelector('[aria-label="Arbeitsbereich bearbeiten"]').click()
  await vi.waitFor(() => expect(document.querySelector('[aria-label="Aktionen für Worktree task"]')).not.toBeNull())
  document.querySelector('[aria-label="Aktionen für Worktree task"]').click()
  await vi.waitFor(() => expect(document.querySelector('[aria-label="Worktree task entfernen"]')).not.toBeNull())
  document.querySelector('[aria-label="Worktree task entfernen"]').click()
  await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/coding/sessions/session/git', {
    action: 'remove', directory: '/worktrees/task', force: true
  }))
  expect(chat.current.currentDirectory).toBe('/repo')
})

it('opens a worktree session chip without changing the selected directory', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', name: 'chat', component: { render: () => null } }] })
  await router.push('/')
  const chat = mountPanel({
    repository: { repository: '/repo', worktrees: [{ path: '/external', branch: 'task', dirty: true,
      sessions: [{ id: 'other', title: 'Shared work', projectId: 'project' }] }] }
  }, { id: 'current' }, router)
  root.querySelector('[aria-label="Arbeitsbereich bearbeiten"]').click()
  await vi.waitFor(() => expect(document.querySelector('[aria-label="Arbeitsbereich /external markieren"]')).not.toBeNull())
  document.querySelector('[aria-label="Arbeitsbereich /external markieren"]').click()
  await vi.waitFor(() => expect(document.querySelector('[aria-label="Session Shared work öffnen"]')).not.toBeNull())
  document.querySelector('[aria-label="Session Shared work öffnen"]').click()
  await vi.waitFor(() => expect([router.currentRoute.value.query, chat.current.currentDirectory]).toEqual([
    { session: 'other', project: 'project' }, '/repo'
  ]))
})

it('opens a deleted session-workspace file from Git even when the editor inspects another repository', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore(), git = useGitChangesStore()
  chat.current = { id: 'session', preferredExecutionAgentId: 'agent', currentDirectory: '/repo' }
  useExecutionAgentStore().items = [{ id: 'agent', connected: true, operations: ['git.repository', 'git.changes'] }]
  useControlCenterStore().$patch({ activeAppId: 'git' })
  await useControlCenterStore().setOpen(true)
  const open = vi.spyOn(useControlCenterStore(), 'openFile').mockResolvedValue('file')
  vi.spyOn(useCommandStore(), 'load').mockResolvedValue()
  api.post.mockResolvedValue({ data: { repository: '/repo', remotes: [], defaultRemote: null, defaultBranch: 'main' } })
  api.get.mockResolvedValue({ data: { repository: '/repo', files: [{ path: 'deleted.txt', status: 'deleted', additions: 0, deletions: 2 }], truncated: false } })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitApp) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  await vi.waitFor(() => expect(root.querySelector('[aria-label="Diff öffnen: deleted.txt"]')).not.toBeNull())
  expect([...root.querySelectorAll('[role="tab"] .text-positive, [role="tab"] .text-negative')].map(item => item.textContent)).toEqual(['−2'])
  git.setContext({ sessionId: 'session', executionAgentId: 'agent', currentDirectory: '/other' })
  Object.assign(git.current, { repository: '/other', files: [{ path: 'foreign.txt' }] })
  await nextTick()
  root.querySelector('[aria-label="Diff öffnen: deleted.txt"]').click()
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ path: '/repo/deleted.txt', executionAgentId: 'agent', currentDirectory: '/repo' }), { diff: true })
})
