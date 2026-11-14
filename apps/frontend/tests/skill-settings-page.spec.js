// @vitest-environment jsdom
import { createApp, h, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { EditorView } from '@codemirror/view'
import { Notify, Quasar } from 'quasar'
import { afterEach, expect, it, vi } from 'vitest'
import { quasarOptions } from './quasar-options'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), put: vi.fn(), post: vi.fn() } }))
const { confirm } = vi.hoisted(() => ({ confirm: vi.fn(async () => false) }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm }) }))

import { adminRoutes, settingsRoutes } from '../src/features/settings/routes'
import { api } from 'src/boot/axios'
import { useSkillStore } from '../src/stores/skills'
import { useAuthStore } from '../src/stores/auth'

let app, root
afterEach(() => {
  app?.unmount()
  root?.remove()
  delete Range.prototype.getBoundingClientRect
  delete Range.prototype.getClientRects
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

async function selectFile(path) {
  document.querySelector('[role="combobox"]').click()
  let option
  await vi.waitFor(() => {
    option = [...document.querySelectorAll('[role="option"]')].find((item) => item.textContent.trim() === path)
    expect(option).toBeTruthy()
  })
  option.click()
  await nextTick()
}

async function mountPage(path = '/skills') {
  // jsdom cannot measure CodeMirror ranges; keep the real editor and its update events.
  Range.prototype.getBoundingClientRect = () => new DOMRect(0, 0, 1, 16)
  Range.prototype.getClientRects = () => [new DOMRect(0, 0, 1, 16)]
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  const skill = { id: 'skill', name: 'example', scope: 'personal', enabled: true, document: '# Instructions', revision: 'a', resourcePaths: ['SKILL.md', 'script.py'] }
  api.get.mockImplementation(async (url) => {
    if (url === '/skills/settings') return { data: { personalSkillsEnabled: true } }
    if (url === '/skills') return { data: { items: [skill] } }
    return { data: new TextEncoder().encode('print(1)\n').buffer, headers: { etag: '"a"', 'x-skill-executable': 'false' } }
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    ...settingsRoutes.filter((item) => item.name === 'settings-skills').map((item) => ({ ...item, path: '/skills' })),
    ...adminRoutes.filter((item) => item.name === 'admin-skills').map((item) => ({ ...item, path: '/admin/skills' }))
  ] })
  await router.push(path)
  root = document.createElement('div')
  document.body.append(root)
  const pinia = createPinia()
  app = createApp({ render: () => h(RouterView) })
  app.use(pinia).use(router).use(Quasar, { ...quasarOptions, plugins: { Notify } })
  useAuthStore(pinia).isAdmin = true
  app.mount(root)
  return { router, pinia, skill }
}

it.each(['text', 'executable', 'navigation'])('keeps %s edits made during a pending save unsaved', async (change) => {
  const { router, pinia, skill } = await mountPage()
  let finishSave
  api.put.mockImplementation(() => new Promise((resolve) => { finishSave = resolve }))
  await vi.waitFor(() => expect(root.querySelector('[aria-label="example bearbeiten"]')).not.toBeNull())
  root.querySelector('[aria-label="example bearbeiten"]').click()
  await vi.dynamicImportSettled()
  await vi.waitFor(() => expect(document.querySelector('.cm-editor')).not.toBeNull())
  await selectFile('script.py')
  await vi.waitFor(() => expect(document.querySelector('.cm-content').textContent).toContain('print(1)'))
  const editor = EditorView.findFromDOM(document.querySelector('.cm-editor'))
  editor.dispatch({ changes: { from: 0, insert: '# Saved edit\n' }, userEvent: 'input.type' })
  await nextTick()
  editor.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: 's', ctrlKey: true, bubbles: true, cancelable: true }))
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledOnce())
  if (change !== 'executable') {
    const view = EditorView.findFromDOM(document.querySelector('.cm-editor'))
    view.dispatch({ changes: { from: 0, insert: '# Later edit\n' }, userEvent: 'input.type' })
  } else {
    document.querySelector('[role="checkbox"]').click()
  }
  await nextTick()
  finishSave({ data: { ...skill, revision: 'b' } })
  await vi.waitFor(() => expect(useSkillStore(pinia).saving).toBe(false))
  await nextTick()
  if (change === 'navigation') {
    await router.push('/skills')
    expect(router.currentRoute.value.name).toBe('settings-skill-edit')
  } else {
    await selectFile('SKILL.md')
    await vi.waitFor(() => expect(confirm).toHaveBeenCalledWith('Ungespeicherte Änderungen verwerfen?', 'Datei wechseln'))
  }
})

it.each(['/skills', '/admin/skills'])('creates a skill from %s without losing edits during the URL change', async (base) => {
  const { router, skill } = await mountPage(`${base}/new`)
  let finishSave
  api.post.mockImplementation(() => new Promise((resolve) => { finishSave = resolve }))
  await vi.waitFor(() => expect(document.querySelector('.cm-editor')).not.toBeNull())
  const editor = EditorView.findFromDOM(document.querySelector('.cm-editor'))
  editor.dispatch({ changes: { from: editor.state.doc.length, insert: 'First edit' } })
  await nextTick()
  editor.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: 's', ctrlKey: true, bubbles: true, cancelable: true }))
  await vi.waitFor(() => expect(api.post).toHaveBeenCalledOnce())
  editor.dispatch({ changes: { from: editor.state.doc.length, insert: ' Later edit' } })
  await nextTick()
  const scope = base.startsWith('/admin') ? 'instance' : 'personal'
  finishSave({ data: { ...skill, scope, document: api.post.mock.calls[0][1].document } })
  await vi.waitFor(() => expect(router.currentRoute.value.path).toBe(`${base}/skill`))
  expect(api.post.mock.calls[0][2]).toEqual({ params: { scope } })
  expect(document.querySelector('.cm-content').textContent).toContain('First edit Later edit')
})
