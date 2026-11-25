// @vitest-environment jsdom
import { createApp, h, ref } from 'vue'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar, QDialog, QCard, QForm, QCardSection, QInput, QSelect, QBtn, QBanner, QCheckbox, QCardActions, QItem, QItemSection, QItemLabel, QIcon, ClosePopup } from 'quasar'
import SessionDialogs from '../src/features/chat/components/SessionDialogs.vue'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))

let app, root
afterEach(() => { app?.unmount(); root?.remove(); vi.unstubAllGlobals() })

it('shows an opt-out for defaults and creates a project in the same dialog', async () => {
  vi.stubGlobal('scrollTo', vi.fn())
  const target = { label: 'Zielprojekt', value: 'target', project: { gitWorkspaceMode: 'worktree' } }
  const projectId = ref('target')
  const move = vi.fn()
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(SessionDialogs, {
    renameOpen: false, renameTitle: '', moveOpen: true, moveProjectId: projectId.value,
    moveSourceSession: { projectId: 'source', workspaceMode: 'worktree' },
    projectOptions: [target, { label: 'Ohne Projekt', value: null }], requiredRule: () => true,
    onMove: move
  }) })
  app.use(Quasar, {
    components: { QDialog, QCard, QForm, QCardSection, QInput, QSelect, QBtn, QBanner, QCheckbox, QCardActions, QItem, QItemSection, QItemLabel, QIcon },
    directives: { ClosePopup }
  })
  app.mount(root)
  const dialog = await vi.waitUntil(() => document.querySelector('.q-dialog'))
  const checkbox = () => dialog.querySelector('[role="checkbox"]')
  const submit = () => dialog.querySelector('[type="submit"]').click()
  const textButton = (text) => [...dialog.querySelectorAll('button')].find(button => button.textContent.includes(text))
  expect(checkbox().getAttribute('aria-checked')).toBe('true')
  expect(dialog.textContent).toContain('Der bisherige Worktree wird nicht verändert.')
  checkbox().click()
  submit()
  await vi.waitFor(() => expect(move).toHaveBeenLastCalledWith({ applyProjectDefaults: false }))
  projectId.value = null
  await vi.waitFor(() => expect(checkbox()).toBeNull())
  textButton('Neues Projekt anlegen').click()
  const name = await vi.waitUntil(() => dialog.querySelector('input[aria-label="Projektname"]'))
  await vi.waitFor(() => expect(document.activeElement).toBe(name))
  expect(dialog.querySelector('[type="submit"]').disabled).toBe(true)
  name.value = '  Neues Projekt  '
  name.dispatchEvent(new Event('input', { bubbles: true }))
  await vi.waitFor(() => expect(dialog.querySelector('[type="submit"]').disabled).toBe(false))
  submit()
  await vi.waitFor(() => expect(move).toHaveBeenLastCalledWith({ newProjectName: 'Neues Projekt' }))
})
