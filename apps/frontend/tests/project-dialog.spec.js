// @vitest-environment jsdom
import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar, Dialog, Notify, QDialog, QCard, QForm, QCardSection, QInput, QSeparator, QSelect, QItem, QItemSection, QIcon, QItemLabel, QBtn, QTooltip, QCardActions, QSpace, ClosePopup } from 'quasar'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }))
vi.mock('src/features/computers/ExecutionAgentDirectoryPickerDialog.vue', () => ({ default: { render: () => null } }))
import ProjectDialog from '../src/features/projects/ProjectDialog.vue'
import { api } from '../src/boot/axios'

let app, root
afterEach(() => { app?.unmount(); root?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it.each([false, true])('saves project fields through the shared dialog (editing=%s)', async (editing) => {
  vi.stubGlobal('scrollTo', vi.fn())
  api.get.mockImplementation(url => Promise.resolve({ data: url === '/session-templates'
    ? { templates: [{ id: 'coding', name: 'Coding' }] }
    : { items: [{ id: 'computer', alias: 'Rechner', connected: true, operations: [], homeDirectory: '/home/user' }] } }))
  const project = { id: 'project', name: 'Bestehend', instructions: 'Anweisungen', iconInitials: 'AB', iconColor: 'blue', defaultSessionTemplateId: 'coding', defaultExecutionAgentId: 'computer', defaultCurrentDirectory: '/home/user/project', gitTargetBranch: 'main', gitFetchBeforeActions: false }
  const request = editing ? api.patch : api.post
  request.mockReset().mockRejectedValueOnce(new Error('unavailable')).mockResolvedValue({ data: project })
  const open = ref(true), created = vi.fn()
  root = document.createElement('div'); document.body.append(root)
  app = createApp({ render: () => h(ProjectDialog, { modelValue: open.value, 'onUpdate:modelValue': value => { open.value = value }, project: editing ? project : null, onCreated: created }) })
  app.use(createPinia()).use(Quasar, { components: { QDialog, QCard, QForm, QCardSection, QInput, QSeparator, QSelect, QItem, QItemSection, QIcon, QItemLabel, QBtn, QTooltip, QCardActions, QSpace }, directives: { ClosePopup }, plugins: { Dialog, Notify } })
  app.mount(root)
  await vi.waitFor(() => expect(document.querySelector('.q-dialog input')).not.toBeNull())
  if (editing) {
    await vi.waitFor(() => expect([
      document.querySelector('.q-dialog [role="status"]')?.textContent.trim(),
      document.querySelector('[aria-label="Git-Repository erneut prüfen"]')?.disabled
    ]).toEqual(['Git-Prüfung nicht verfügbar. Agent-Version und Git-Installation prüfen.', true]))
  }
  const name = document.querySelector('.q-dialog input')
  name.value = 'Umbenannt'
  name.dispatchEvent(new Event('input', { bubbles: true }))
  const initials = document.querySelector('input[aria-label="Projektkürzel"]')
  expect(initials.value).toBe(editing ? 'AB' : '')
  initials.value = editing ? '' : 'UM'
  initials.dispatchEvent(new Event('input', { bubbles: true }))
  const colorField = [...document.querySelectorAll('.q-field')].find(field => field.querySelector('.q-field__label')?.textContent === 'Projektfarbe')
  colorField.querySelector('[role="combobox"]').click()
  const color = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find(option => option.textContent.trim().endsWith(editing ? 'Automatisch' : 'Türkis')))
  color.click()
  const save = () => document.querySelector('.q-dialog form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  save()
  await vi.waitFor(() => expect(request).toHaveBeenCalledOnce())
  expect(open.value).toBe(true)
  expect(name.value).toBe('Umbenannt')
  save()
  await vi.waitFor(() => expect(open.value).toBe(false))
  expect(request).toHaveBeenLastCalledWith(editing ? '/projects/project' : '/projects', {
    name: 'Umbenannt', instructions: editing ? 'Anweisungen' : '',
    iconInitials: editing ? null : 'UM',
    iconColor: editing ? null : 'teal',
    defaultSessionTemplateId: editing ? 'coding' : null,
    defaultExecutionAgentId: editing ? 'computer' : null,
    defaultCurrentDirectory: editing ? '/home/user/project' : null,
    gitTargetBranch: editing ? 'main' : null,
    gitWorkspaceMode: 'local',
    gitFetchBeforeActions: !editing
  })
  if (editing) expect(created).not.toHaveBeenCalled()
  else expect(created).toHaveBeenCalledWith(project)
})

it('filters local branches and saves the selected target branch', async () => {
  vi.stubGlobal('scrollTo', vi.fn())
  api.get.mockImplementation(url => Promise.resolve({ data: url === '/session-templates' ? { templates: [] }
    : { items: [{ id: 'computer', connected: true, operations: ['git.repository'] }] } }))
  api.post.mockReset().mockResolvedValue({ data: { repository: '/repo', defaultBranch: 'develop', localBranches: ['develop', 'feature/release'] } })
  api.patch.mockReset().mockResolvedValue({ data: {} })
  root = document.createElement('div'); document.body.append(root)
  app = createApp({ render: () => h(ProjectDialog, { modelValue: true, project: { id: 'project', name: 'Projekt', defaultExecutionAgentId: 'computer', defaultCurrentDirectory: '/repo' } }) })
  app.use(createPinia()).use(Quasar, { components: { QDialog, QCard, QForm, QCardSection, QInput, QSeparator, QSelect, QItem, QItemSection, QIcon, QItemLabel, QBtn, QTooltip, QCardActions, QSpace }, directives: { ClosePopup }, plugins: { Dialog, Notify } })
  app.mount(root)
  await vi.waitFor(() => expect(document.querySelector('input[aria-label="Standard-Zielbranch"]')?.disabled).toBe(false))
  const input = document.querySelector('input[aria-label="Standard-Zielbranch"]')
  input.focus()
  input.value = 'RELEASE'
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await vi.waitFor(() => expect([...document.querySelectorAll('[role="option"] .q-item__label')].map(option => option.textContent)).toEqual(['feature/release']))
  document.querySelector('[role="option"]').click()
  document.querySelector('.q-dialog form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await vi.waitFor(() => expect(api.patch).toHaveBeenCalledWith('/projects/project', expect.objectContaining({ gitTargetBranch: 'feature/release' })))
})
