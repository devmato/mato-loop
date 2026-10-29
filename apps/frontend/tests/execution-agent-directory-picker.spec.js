// @vitest-environment jsdom
import { createApp, h, ref } from 'vue'
import { afterEach, expect, it, vi } from 'vitest'
import {
  Quasar, QAvatar, QBreadcrumbs, QBreadcrumbsEl, QBtn, QCard, QCardActions,
  QCardSection, QDialog, QIcon, QItem, QItemLabel, QItemSection, QLinearProgress,
  QList, QSeparator, QToolbar, Dialog, Notify, Ripple
} from 'quasar'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), post: vi.fn() } }))

import ExecutionAgentDirectoryPickerDialog from '../src/features/computers/ExecutionAgentDirectoryPickerDialog.vue'
import { api } from 'src/boot/axios'

let app, root

afterEach(() => {
  app?.unmount()
  root?.remove()
  document.querySelectorAll('.q-dialog').forEach((dialog) => dialog.remove())
  vi.clearAllMocks()
})

it('browses, creates and selects directories on the chosen execution agent', async () => {
  api.post.mockResolvedValue({ data: { path: '/home/user/projects/new-folder' } })
  api.get.mockImplementation(async (_url, { params }) => ({
    data: params.path === '/home/user'
      ? {
          path: '/home/user',
          entries: [
            { name: 'projects', path: '/home/user/projects', kind: 'directory' },
            { name: 'notes.txt', path: '/home/user/notes.txt', kind: 'file' }
          ],
          hasMore: false,
          nextOffset: null
        }
      : { path: params.path, entries: [], hasMore: false, nextOffset: null }
  }))
  const open = ref(true)
  const selected = ref(null)
  window.scrollTo = vi.fn()
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({
    render: () => h(ExecutionAgentDirectoryPickerDialog, {
      modelValue: open.value,
      executionAgentId: 'agent-1',
      initialPath: '/home/user/',
      'onUpdate:modelValue': (value) => { open.value = value },
      onSelect: (value) => { selected.value = value }
    })
  })
  app.use(Quasar, {
    components: {
      QAvatar, QBreadcrumbs, QBreadcrumbsEl, QBtn, QCard, QCardActions,
      QCardSection, QDialog, QIcon, QItem, QItemLabel, QItemSection,
      QLinearProgress, QList, QSeparator, QToolbar
    },
    directives: { Ripple },
    plugins: { Dialog, Notify }
  })
  app.mount(root)

  await vi.waitFor(() => expect(document.body.textContent).toContain('/home/user/projects'))
  expect(document.body.textContent).not.toContain('notes.txt')
  expect(api.get).toHaveBeenCalledWith('/workspace/entries', expect.objectContaining({
    params: expect.objectContaining({
      execution_agent_id: 'agent-1',
      path: '/home/user'
    })
  }))

  ;[...document.querySelectorAll('.q-item')]
    .find((item) => item.textContent.includes('/home/user/projects'))
    .click()
  await vi.waitFor(() => expect(api.get).toHaveBeenLastCalledWith(
    '/workspace/entries',
    expect.objectContaining({ params: expect.objectContaining({ path: '/home/user/projects' }) })
  ))
  await vi.waitFor(() => expect(document.body.textContent).toContain('Keine Unterordner vorhanden.'))
  document.querySelector('[aria-label="Neuen Ordner anlegen"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-dialog input')).not.toBeNull())
  const input = document.querySelector('.q-dialog input')
  await vi.waitFor(() => expect(document.activeElement).toBe(input), { timeout: 100 })
  input.value = 'new-folder'
  input.dispatchEvent(new Event('input', { bubbles: true }))
  const createButton = [...document.querySelectorAll('button')].find((item) => item.textContent === 'Anlegen')
  await vi.waitFor(() => expect(createButton.disabled).toBe(false))
  createButton.click()
  await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith('/workspace/directories', {
    parent: '/home/user/projects', name: 'new-folder'
  }, { params: { scope: 'filesystem', execution_agent_id: 'agent-1' } }))
  await vi.waitFor(() => expect(document.body.textContent).toContain('new-folder'))
  ;[...document.querySelectorAll('button')]
    .find((button) => button.textContent.includes('Diesen Ordner auswählen'))
    .click()

  expect(selected.value).toBe('/home/user/projects/new-folder')
  expect(open.value).toBe(false)
})

it('keeps the drive root when navigating Windows directories', async () => {
  api.get.mockImplementation(async (_url, { params }) => ({
    data: { path: params.path, entries: [], hasMore: false, nextOffset: null }
  }))
  window.scrollTo = vi.fn()
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({
    render: () => h(ExecutionAgentDirectoryPickerDialog, {
      modelValue: true,
      executionAgentId: 'windows-agent',
      initialPath: 'C:/Users/Test/workspace'
    })
  })
  app.use(Quasar, {
    components: {
      QAvatar, QBreadcrumbs, QBreadcrumbsEl, QBtn, QCard, QCardActions,
      QCardSection, QDialog, QIcon, QItem, QItemLabel, QItemSection,
      QLinearProgress, QList, QSeparator, QToolbar
    },
    directives: { Ripple }
  })
  app.mount(root)
  await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith('/workspace/entries',
    expect.objectContaining({ params: expect.objectContaining({ path: 'C:/Users/Test/workspace' }) })))
  ;[...document.querySelectorAll('.q-breadcrumbs__el')]
    .find((item) => item.textContent.includes('C:/'))
    .click()
  await vi.waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/workspace/entries',
    expect.objectContaining({ params: expect.objectContaining({ path: 'C:/' }) })))
})
