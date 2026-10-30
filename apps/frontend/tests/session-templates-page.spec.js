// @vitest-environment jsdom
import { createApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { afterEach, expect, it, vi } from 'vitest'
import { Quasar } from 'quasar'
import { quasarOptions } from './quasar-options'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))
vi.mock('src/boot/axios', () => ({ api: { get: vi.fn(), put: vi.fn() } }))
vi.mock('src/composables/confirm', () => ({ useConfirm: () => ({ confirm: async () => true }) }))

import Page from '../src/features/settings/SessionTemplatesPage.vue'
import { api } from 'src/boot/axios'

let app, root
afterEach(() => { app?.unmount(); root?.remove(); vi.clearAllMocks() })

it('saves worktree support and reasoning choices for an inherited template model', async () => {
  const template = { id: 'template', name: 'Coding', agentIds: ['plan'], startAgentId: 'plan', memoryMode: 'full', messageSize: 'small', worktreesEnabled: false, modelId: null, reasoningEffort: null, agentModels: {} }
  const templates = { templates: [template], personalSettingsEnabled: true }
  const responses = {
    '/providers': {
      providers: [{ id: 'provider', name: 'Provider', enabled: true }],
      models: [{ id: 'standard', providerId: 'provider', modelId: 'standard', enabled: true, supportsTools: true, reasoningEfforts: [] }, { id: 'profile', providerId: 'provider', modelId: 'profile', enabled: true, supportsTools: true, reasoningEfforts: ['high'], reasoningEffort: 'high' }]
    },
    '/agent-profiles': { effectiveAgents: [{ id: 'plan', name: 'Plan' }] },
    '/model-defaults': { effective: { defaultModelId: 'standard', profileModels: { plan: { modelId: 'profile' } } } },
    '/session-templates': templates
  }
  api.get.mockImplementation(async (url) => ({ data: responses[url] }))
  api.put.mockResolvedValue({ data: templates })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: Page }] })
  await router.push('/')
  root = document.createElement('div')
  document.body.append(root)
  app = createApp({ render: () => h(RouterView) })
  app.use(createPinia()).use(router).use(Quasar, quasarOptions)
  app.config.globalProperties.$q.notify = vi.fn()
  app.mount(root)
  await vi.waitFor(() => expect(root.querySelector('[aria-label="Coding bearbeiten"]')?.disabled).toBe(false))
  root.querySelector('[aria-label="Coding bearbeiten"]').click()
  await vi.waitFor(() => expect(document.querySelector('.q-expansion-item .q-item')).not.toBeNull())
  document.querySelector('.q-toggle').click()
  document.querySelector('.q-expansion-item .q-item').click()
  const reasoning = await vi.waitUntil(() => [...document.querySelectorAll('.q-expansion-item [role="combobox"]')].find((item) => item.closest('.q-field').textContent.includes('Reasoning-Stufe')))
  reasoning.click()
  const high = await vi.waitUntil(() => [...document.querySelectorAll('[role="option"]')].find((item) => item.querySelector('.q-item__label')?.textContent === 'Hoch'))
  high.click()
  document.querySelector('.q-dialog form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith('/session-templates/template', { ...template, worktreesEnabled: true, agentModels: { plan: { modelId: null, reasoningEffort: 'high' } } }, { params: { scope: 'personal' } }))
})
