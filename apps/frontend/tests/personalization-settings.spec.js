// @vitest-environment jsdom
import { createApp, h, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { Dialog, Notify, Quasar } from 'quasar'
import { afterEach, expect, it, vi } from 'vitest'
import { quasarOptions } from './quasar-options'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), patch: vi.fn() } }))
vi.mock('src/features/personalization/AgentIdentitySettingsPage.vue', () => ({ default: { render: () => null } }))
vi.mock('src/features/personalization/AssistantSettings.vue', () => ({ default: { render: () => null } }))

import PersonalizationSettingsPage from '../src/features/personalization/PersonalizationSettingsPage.vue'
import { api } from '../src/boot/axios'

let app, root
afterEach(() => { app?.unmount(); root?.remove(); vi.unstubAllGlobals() })

it('adds labeled phone numbers, validates them, and lets the user remove an entry', async () => {
  vi.stubGlobal('scrollTo', vi.fn())
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: PersonalizationSettingsPage },
    { path: '/memory', name: 'settings-memory', component: { render: () => null } }
  ] })
  api.get.mockResolvedValue({ data: {} })
  api.patch.mockImplementation((_url, data) => Promise.resolve({ data }))
  await router.push('/')
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(RouterView) })
  app.use(createPinia()).use(router).use(Quasar, { ...quasarOptions, plugins: { Dialog, Notify } })
  app.mount(root)

  const button = (text) => [...root.querySelectorAll('button')].find((el) => el.textContent.trim().endsWith(text))
  const inputs = () => [...root.querySelectorAll('input[type="tel"]')]
  await vi.waitFor(() => expect(button('Telefonnummer hinzufügen')?.disabled).toBe(false))
  button('Telefonnummer hinzufügen').click()
  button('Telefonnummer hinzufügen').click()
  await nextTick()
  button('Speichern').click()
  await vi.waitFor(() => expect(root.textContent).toContain('Gib eine gültige Telefonnummer ein.'))
  expect(api.patch).not.toHaveBeenCalled()

  const secondLabel = root.querySelectorAll('[role="combobox"]')[1]
  secondLabel.click()
  const work = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')]
    .find((option) => option.textContent.trim() === 'Arbeit'))
  work.click()
  const numbers = ['+49 (30) ' + '0000-12', '030/0000-12']
  inputs().forEach((input, index) => {
    input.value = numbers[index]
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await nextTick()
  button('Speichern').click()
  const mobile = { label: 'mobile', number: numbers[0] }
  const office = { label: 'work', number: numbers[1] }
  await vi.waitFor(() => expect(api.patch).toHaveBeenCalledWith('/user-profile', { phoneNumbers: [mobile, office] }))
  expect(inputs().map((input) => input.value)).toEqual(numbers)

  root.querySelector('[aria-label="Telefonnummer 1 entfernen"]').click()
  const confirm = await vi.waitUntil(() => [...document.querySelectorAll('.q-dialog button')]
    .find((el) => el.textContent.trim().endsWith('Entfernen')))
  confirm.click()
  await vi.waitFor(() => expect(inputs().map((input) => input.value)).toEqual([numbers[1]]))
  button('Speichern').click()
  await vi.waitFor(() => expect(api.patch).toHaveBeenLastCalledWith('/user-profile', { phoneNumbers: [office] }))
})
