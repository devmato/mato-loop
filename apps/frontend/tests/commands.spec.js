// @vitest-environment jsdom
import { createApp, h, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar, Notify } from 'quasar'
import { quasarOptions } from './quasar-options'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm: vi.fn().mockResolvedValue(true) }) }))

import GitActionList from '../src/features/coding/GitActionList.vue'
import GitApp from '../src/features/coding/GitApp.vue'
import WorkspaceField from '../src/features/coding/WorkspaceField.vue'
import { useGitActionStore } from '../src/features/coding/git-actions-store'
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

let app, root
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.clearAllMocks(); vi.useRealTimers() })

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

it('runs a selected command in the current session without consuming its draft', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const commands = useCommandStore()
  commands.loaded = true
  commands.items = [{ source: 'git', name: 'git-review', label: 'Änderungen prüfen', description: 'Review' }]
  const chat = useChatStore()
  chat.outboxLoaded = true
  chat.current = { id: 'session', messages: [], agentProfileId: 'stored', preferredExecutionAgentId: 'agent', currentDirectory: '/repo' }
  chat.drafts.sessionAgentProfiles.session = 'selected'
  useExecutionAgentStore().items = [{ id: 'agent', connected: true }]
  const send = vi.spyOn(chat, 'sendToCurrent').mockResolvedValue({})
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitActionList) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  await nextTick()
  root.querySelector('.q-item--clickable').click()
  expect(send).toHaveBeenCalledWith('/git-review', [], expect.objectContaining({ clearDraft: false, activeFile: null, agentProfileId: 'selected' }))
  chat.queueDrainPromise = Promise.resolve()
  await nextTick()
  expect(root.querySelector('.q-item').getAttribute('aria-disabled')).toBe('true')
})

it('explains and blocks publication without a remote in the Git app', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const commands = useCommandStore(), chat = useChatStore()
  commands.loaded = true
  commands.items = [{ source: 'git', name: 'git-sync', label: 'Projektverzeichnis aktualisieren', disabledReason: 'Remote einrichten, um zu veröffentlichen.' }, { source: 'git', name: 'git-remote', label: 'Remote einrichten' }]
  chat.outboxLoaded = true
  chat.current = { id: 'session', preferredExecutionAgentId: 'agent', currentDirectory: '/repo', workspaceMode: 'local' }
  useExecutionAgentStore().items = [{ id: 'agent', connected: true }]
  const run = vi.spyOn(useGitActionStore(), 'run')
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitActionList) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  await nextTick()
  root.querySelector('.q-item').click()
  expect([root.textContent.includes('Remote einrichten, um zu veröffentlichen.'), root.textContent.includes('/git-'), root.querySelectorAll('.q-item').length, run.mock.calls.length])
    .toEqual([true, false, 1, 0])
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
  useControlCenterStore().$patch({ open: true, activeAppId: 'git' })
  vi.spyOn(useCommandStore(), 'load').mockResolvedValue()
  const session = { id: 'session', projectId: 'project', preferredExecutionAgentId: 'agent', currentDirectory: '/repo' }
  vi.spyOn(chat, 'createEmptySession').mockImplementation(async () => { chat.current = session; return session })
  const adopt = vi.spyOn(commands, 'useRemote').mockResolvedValue('session')
  const update = vi.spyOn(projects, 'update')
  api.post.mockResolvedValue({ data: { repository: '/repo', remotes: ['origin'], defaultRemote: 'origin',
    remoteUrls: { origin: 'https://example.com/repo.git' }, remoteIdentities: { origin: 'a'.repeat(64) } } })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitApp) })
  app.use(pinia).use(Quasar, { ...quasarOptions, plugins: { Notify } }).mount(root)
  root.querySelectorAll('[role="tab"]')[1].click()
  await vi.waitFor(() => expect([...root.querySelectorAll('button')].some(button => button.textContent.includes('Bearbeiten'))).toBe(true))
  const edit = [...root.querySelectorAll('button')].find(button => button.textContent.includes('Bearbeiten'))
  edit.click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog form')).not.toBeNull())
  document.querySelector('.q-dialog form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await vi.waitFor(() => expect(adopt).toHaveBeenCalledWith('origin', 'a'.repeat(64)))
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

it('opens a deleted session-workspace file from Git even when the editor inspects another repository', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const chat = useChatStore(), git = useGitChangesStore()
  chat.current = { id: 'session', preferredExecutionAgentId: 'agent', currentDirectory: '/repo' }
  useExecutionAgentStore().items = [{ id: 'agent', connected: true, operations: ['git.repository', 'git.changes'] }]
  useControlCenterStore().$patch({ open: true, activeAppId: 'git' })
  const open = vi.spyOn(useControlCenterStore(), 'openFile').mockResolvedValue('file')
  vi.spyOn(useCommandStore(), 'load').mockResolvedValue()
  api.post.mockResolvedValue({ data: { repository: '/repo', remotes: [], defaultRemote: null, defaultBranch: 'main' } })
  api.get.mockResolvedValue({ data: { repository: '/repo', files: [{ path: 'deleted.txt', status: 'deleted', additions: 0, deletions: 2 }], truncated: false } })
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(GitApp) })
  app.use(pinia).use(Quasar, quasarOptions).mount(root)
  await vi.waitFor(() => expect(root.querySelector('[aria-label="Diff öffnen: deleted.txt"]')).not.toBeNull())
  git.setContext({ sessionId: 'session', executionAgentId: 'agent', currentDirectory: '/other' })
  Object.assign(git.current, { repository: '/other', files: [{ path: 'foreign.txt' }] })
  await nextTick()
  root.querySelector('[aria-label="Diff öffnen: deleted.txt"]').click()
  expect(open).toHaveBeenCalledWith(expect.objectContaining({ path: '/repo/deleted.txt', executionAgentId: 'agent', currentDirectory: '/repo' }), { diff: true })
})
