import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { api } = vi.hoisted(() => ({
  api: { delete: vi.fn(), get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn() }
}))

vi.mock('src/boot/axios', () => ({ api }))

import { useIntegrationStore } from '../src/stores/integrations'
import { useAuthStore } from '../src/stores/auth'

const searchResponse = {
  configured: true,
  enabled: true,
  effectiveEnabled: true,
  unavailableReason: null,
  providers: [
    {
      id: 'integration-id',
      name: 'Intern',
      integrationType: 'search',
      providerType: 'searxng',
      enabled: true,
      supportsIntegrationPolicies: false,
      supportsResourcePolicies: false,
      config: { baseUrl: 'https://search.example.com' }
    }
  ]
}

const calendarResponse = {
  configured: true,
  accounts: [
    {
      id: 'calendar-id',
      name: 'Arbeit',
      integrationType: 'calendar',
      providerType: 'caldav',
      enabled: true,
      hasCredential: true,
      supportsIntegrationPolicies: true,
      supportsResourcePolicies: true,
      config: { baseUrl: 'https://calendar.example.com', username: 'mato' }
    }
  ]
}

const knowledgeResponse = {
  configured: true,
  bases: [
    {
      id: 'knowledge-id',
      name: 'Wiki',
      integrationType: 'knowledge',
      providerType: 'bookstack',
      enabled: true,
      hasCredential: true,
      supportsIntegrationPolicies: true,
      supportsResourcePolicies: true,
      config: { baseUrl: 'https://wiki.example.com' }
    }
  ]
}

const recipesResponse = {
  configured: true,
  accounts: [
    {
      id: 'mealie-id',
      name: 'Familienküche',
      integrationType: 'recipes',
      providerType: 'mealie',
      enabled: true,
      hasCredential: true,
      supportsIntegrationPolicies: true,
      supportsResourcePolicies: false,
      config: { baseUrl: 'http://mealie.example.com' }
    }
  ]
}

const musicResponse = {
  configured: true,
  instances: [{
    id: 'sonobin-id',
    name: 'SonoBin',
    integrationType: 'music',
    providerType: 'sonobin',
    enabled: true,
    hasCredential: true,
    config: { baseUrl: 'https://sonobin.example.com' },
    toolCount: 28
  }]
}

const openScadResponse = {
  configured: true,
  instances: [{
    id: 'openscad-id',
    name: 'OpenSCAD',
    integrationType: 'openscad',
    providerType: 'openscad',
    executionAgentId: 'agent-1',
    enabled: true,
    config: { binaryPath: '/usr/bin/openscad' },
    executionLocation: 'agent'
  }]
}

const codingResponse = {
  configured: true,
  instances: [{
    id: 'opencode-id',
    name: 'OpenCode',
    integrationType: 'coding',
    providerType: 'opencode',
    enabled: true,
    hasCredential: true,
    config: { baseUrl: 'http://localhost:4096', username: 'opencode' },
    serverVersion: '1.0.0'
  }]
}

const catalogResponse = {
  adapters: [
    {
      integrationType: 'search',
      adapterType: 'searxng',
      executionLocation: 'backend',
      availability: 'instance_only'
    },
    {
      integrationType: 'openscad',
      adapterType: 'openscad',
      executionLocation: 'agent',
      availability: 'disabled'
    }
  ]
}

describe('integration store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('sends only the desired status and the selected owner scope', async () => {
    api.patch.mockResolvedValue({ data: { id: 'calendar-id', enabled: false } })
    const store = useIntegrationStore()

    await store.setIntegrationEnabled('calendar-id', false, 'personal')

    expect(api.patch).toHaveBeenCalledWith(
      '/integrations/calendar-id/enabled', { enabled: false }, { params: { scope: 'personal' } }
    )
  })

  it('loads the integration catalog and applies availability by scope', async () => {
    api.get.mockResolvedValue({ data: catalogResponse })
    const store = useIntegrationStore()

    await store.fetchCatalog()

    expect(api.get).toHaveBeenCalledWith('/integrations/catalog')
    expect(store.catalog).toEqual(catalogResponse.adapters)
    expect(store.catalogLoaded).toBe(true)
    expect(store.catalogLoadFailed).toBe(false)
    expect(store.adapterAvailable('searxng', 'instance')).toBe(true)
    expect(store.adapterAvailable('searxng', 'personal')).toBe(false)
    expect(store.adapterAvailable('openscad', 'instance')).toBe(false)
  })

  it('updates one catalog adapter from the server response', async () => {
    const updated = { ...catalogResponse.adapters[1], availability: 'full' }
    api.put.mockResolvedValue({ data: updated })
    const store = useIntegrationStore()
    store.catalog = [...catalogResponse.adapters]

    await store.setAdapterAvailability('openscad', 'full')

    expect(api.put).toHaveBeenCalledWith('/integrations/catalog/openscad', { availability: 'full' })
    expect(store.catalog[1]).toEqual(updated)
    expect(store.adapterAvailable('openscad', 'personal')).toBe(true)
    expect(store.adapterAvailable('openscad', 'instance')).toBe(false)
    expect(store.catalogLoading).toBe(false)
  })

  it('keeps personal-only adapters out of instance scope', () => {
    const store = useIntegrationStore()
    store.catalog = [{
      integrationType: 'coding',
      adapterType: 'opencode',
      executionLocation: 'backend',
      availability: 'full',
      personalOnly: true
    }]

    expect(store.adapterAvailable('opencode', 'instance')).toBe(false)
    expect(store.adapterAvailable('opencode', 'personal')).toBe(true)
  })

  it('keeps Jina instance-only even for administrators', () => {
    useAuthStore().isAdmin = true
    const store = useIntegrationStore()
    store.catalog = [{ integrationType: 'webfetch', adapterType: 'jina', executionLocation: 'backend', availability: 'instance_only', instanceOnly: true }]
    expect([store.adapterAvailable('jina', 'instance'), store.adapterAvailable('jina', 'personal')]).toEqual([true, false])
  })

  it('manages personal OpenCode integrations without exposing blank passwords', async () => {
    api.get.mockResolvedValue({ data: codingResponse })
    api.post.mockResolvedValue({ data: codingResponse })
    api.put.mockResolvedValue({ data: codingResponse })
    api.delete.mockResolvedValue({})
    const store = useIntegrationStore()
    store.selectScope('personal')

    await store.fetchCoding()
    await store.createOpenCode({ name: 'OpenCode', baseUrl: 'http://localhost:4096', username: 'opencode', password: '' })
    await store.updateOpenCode('opencode-id', { name: 'OpenCode', password: '', removePassword: false })
    await store.removeOpenCode('opencode-id')

    expect(api.post).toHaveBeenCalledWith('/integrations/coding/opencode', {
      name: 'OpenCode', baseUrl: 'http://localhost:4096', username: 'opencode'
    })
    expect(api.put).toHaveBeenCalledWith('/integrations/coding/opencode/opencode-id', {
      name: 'OpenCode', removePassword: false
    })
    expect(api.delete).toHaveBeenCalledWith('/integrations/coding/opencode/opencode-id')
    expect(store.coding).toEqual(codingResponse)
  })

  it('keeps the stored Arcane API key when updating its connection', async () => {
    const response = { configured: true, instances: [{ id: 'arcane-id', name: 'Server', integrationType: 'infrastructure', providerType: 'arcane', hasCredential: true }] }
    api.get.mockResolvedValue({ data: response })
    api.put.mockResolvedValue({ data: response })
    const store = useIntegrationStore()

    await store.updateArcane('arcane-id', { name: 'Server', base_url: 'https://arcane.example.test', api_key: '', enabled: true })

    expect(api.put).toHaveBeenCalledWith('/integrations/infrastructure/arcane/arcane-id', {
      name: 'Server', base_url: 'https://arcane.example.test', enabled: true
    }, { params: { scope: 'instance' } })
    expect(store.arcane).toEqual(response)
  })

  it('discovers personal OpenCode integrations independently of the settings scope', async () => {
    api.get.mockResolvedValue({ data: codingResponse })
    const store = useIntegrationStore()

    const result = await store.fetchCoding({ personal: true })

    expect(api.get).toHaveBeenCalledWith('/integrations/coding', {
      suppressErrorToast: true,
      params: { scope: 'personal' }
    })
    expect(result).toEqual(codingResponse)
  })

  it('loads only configured external integrations', async () => {
    api.get.mockResolvedValue({ data: searchResponse })
    const store = useIntegrationStore()

    await store.fetchSearch()

    expect(api.get).toHaveBeenCalledWith('/integrations/search', { params: { scope: 'instance' } })
    expect(store.search.providers.map((item) => item.providerType)).toEqual(['searxng'])
    expect(store.loaded).toBe(true)
    expect(store.loadFailed).toBe(false)
  })

  it('marks a failed initial load without inventing an integration', async () => {
    api.get.mockRejectedValue(new Error('offline'))
    const store = useIntegrationStore()

    await expect(store.fetchSearch()).rejects.toThrow('offline')

    expect(store.search.providers).toEqual([])
    expect(store.loaded).toBe(false)
    expect(store.loadFailed).toBe(true)
  })

  it('reloads the canonical contract after creating an integration', async () => {
    api.post.mockResolvedValue({ data: searchResponse })
    api.get.mockResolvedValue({ data: searchResponse })
    const store = useIntegrationStore()
    const payload = {
      integration_type: 'search',
      provider_type: 'searxng',
      name: 'Intern',
      base_url: 'https://search.example.com',
      enabled: true
    }

    await store.createSearch(payload)

    expect(api.post).toHaveBeenCalledWith(
      '/integrations/search/providers', payload, { params: { scope: 'instance' } }
    )
    expect(store.search.providers).toHaveLength(1)
  })

  it('loads and dispatches calendar account operations', async () => {
    api.get.mockResolvedValue({ data: calendarResponse })
    api.post.mockResolvedValue({ data: { status: 'ok' } })
    api.delete.mockResolvedValue({})
    const store = useIntegrationStore()
    const payload = {
      integration_type: 'calendar',
      provider_type: 'caldav',
      name: 'Arbeit',
      base_url: 'https://calendar.example.com',
      username: 'mato',
      password: 'secret',
      enabled: true
    }

    await store.fetchCalendar()
    await store.createCalendar(payload)
    await store.testCalendar('calendar-id')
    await store.removeCalendar('calendar-id')

    expect(api.get).toHaveBeenCalledWith('/integrations/calendar', { params: { scope: 'instance' } })
    expect(api.post).toHaveBeenCalledWith(
      '/integrations/calendar/accounts', payload, { params: { scope: 'instance' } }
    )
    expect(api.post).toHaveBeenCalledWith(
      '/integrations/calendar/accounts/calendar-id/test', null, { params: { scope: 'instance' } }
    )
    expect(api.delete).toHaveBeenCalledWith(
      '/integrations/calendar/accounts/calendar-id', { params: { scope: 'instance' } }
    )
    expect(store.calendar.accounts).toHaveLength(1)
  })

  it('omits a blank password when updating a calendar account', async () => {
    api.get.mockResolvedValue({ data: calendarResponse })
    api.put.mockResolvedValue({ data: calendarResponse })
    const store = useIntegrationStore()

    await store.updateCalendar('calendar-id', {
      name: 'Arbeit',
      password: '',
      enabled: true
    })

    expect(api.put).toHaveBeenCalledWith(
      '/integrations/calendar/accounts/calendar-id',
      { name: 'Arbeit', enabled: true },
      { params: { scope: 'instance' } }
    )
  })

  it('loads and dispatches knowledge base operations', async () => {
    api.get.mockResolvedValue({ data: knowledgeResponse })
    api.post.mockResolvedValue({ data: { status: 'ok' } })
    api.delete.mockResolvedValue({})
    const store = useIntegrationStore()
    const payload = {
      integration_type: 'knowledge',
      provider_type: 'bookstack',
      name: 'Wiki',
      base_url: 'https://wiki.example.com',
      token_id: 'token',
      token_secret: 'secret',
      enabled: true
    }

    await store.fetchKnowledge()
    await store.createKnowledge(payload)
    await store.testKnowledge('knowledge-id')
    await store.removeKnowledge('knowledge-id')

    expect(api.get).toHaveBeenCalledWith('/integrations/knowledge', { params: { scope: 'instance' } })
    expect(api.post).toHaveBeenCalledWith(
      '/integrations/knowledge/bases', payload, { params: { scope: 'instance' } }
    )
    expect(api.post).toHaveBeenCalledWith(
      '/integrations/knowledge/bases/knowledge-id/test', null, { params: { scope: 'instance' } }
    )
    expect(api.delete).toHaveBeenCalledWith(
      '/integrations/knowledge/bases/knowledge-id', { params: { scope: 'instance' } }
    )
    expect(store.knowledge.bases).toHaveLength(1)
  })

  it('omits incomplete token fields when updating a knowledge base', async () => {
    api.get.mockResolvedValue({ data: knowledgeResponse })
    api.put.mockResolvedValue({ data: knowledgeResponse })
    const store = useIntegrationStore()

    await store.updateKnowledge('knowledge-id', {
      name: 'Wiki',
      token_id: 'token',
      token_secret: '',
      enabled: true
    })

    expect(api.put).toHaveBeenCalledWith(
      '/integrations/knowledge/bases/knowledge-id',
      { name: 'Wiki', enabled: true },
      { params: { scope: 'instance' } }
    )
  })

  it('loads and dispatches Mealie account operations', async () => {
    api.get.mockResolvedValue({ data: recipesResponse })
    api.post.mockResolvedValue({ data: { status: 'ok' } })
    api.delete.mockResolvedValue({})
    const store = useIntegrationStore()
    const payload = {
      integration_type: 'recipes',
      provider_type: 'mealie',
      name: 'Familienküche',
      base_url: 'http://mealie.example.com',
      token: 'secret',
      enabled: true
    }

    await store.fetchRecipes()
    await store.createRecipes(payload)
    await store.testRecipes('mealie-id')
    await store.removeRecipes('mealie-id')

    expect(api.get).toHaveBeenCalledWith('/integrations/recipes', { params: { scope: 'instance' } })
    expect(api.post).toHaveBeenCalledWith(
      '/integrations/recipes/accounts', payload, { params: { scope: 'instance' } }
    )
    expect(api.post).toHaveBeenCalledWith(
      '/integrations/recipes/accounts/mealie-id/test', null, { params: { scope: 'instance' } }
    )
    expect(api.delete).toHaveBeenCalledWith(
      '/integrations/recipes/accounts/mealie-id', { params: { scope: 'instance' } }
    )
    expect(store.recipes.accounts).toHaveLength(1)
  })

  it('keeps the stored Mealie token on a blank update', async () => {
    api.get.mockResolvedValue({ data: recipesResponse })
    api.put.mockResolvedValue({ data: recipesResponse })
    const store = useIntegrationStore()

    await store.updateRecipes('mealie-id', { name: 'Familienküche', token: '   ', enabled: true })

    expect(api.put).toHaveBeenCalledWith(
      '/integrations/recipes/accounts/mealie-id',
      { name: 'Familienküche', enabled: true },
      { params: { scope: 'instance' } }
    )
  })

  it('manages SonoBin integrations without resending a blank token', async () => {
    api.get.mockResolvedValue({ data: musicResponse })
    api.post.mockResolvedValue({ data: { status: 'ok' } })
    api.put.mockResolvedValue({ data: musicResponse })
    api.delete.mockResolvedValue({})
    const store = useIntegrationStore()
    const payload = {
      integration_type: 'music',
      provider_type: 'sonobin',
      name: 'SonoBin',
      baseUrl: 'https://sonobin.example.com',
      bearerToken: 'secret',
      enabled: true
    }

    await store.fetchMusic()
    await store.createSonoBin(payload)
    await store.updateSonoBin('sonobin-id', { ...payload, bearerToken: '' })
    await store.testSonoBin('sonobin-id')
    await store.removeSonoBin('sonobin-id')

    expect(api.get).toHaveBeenCalledWith('/integrations/music/sonobin', { params: { scope: 'instance' } })
    expect(api.post).toHaveBeenCalledWith('/integrations/music/sonobin', payload, { params: { scope: 'instance' } })
    expect(api.put).toHaveBeenCalledWith(
      '/integrations/music/sonobin/sonobin-id',
      {
        integration_type: 'music',
        provider_type: 'sonobin',
        name: 'SonoBin',
        baseUrl: 'https://sonobin.example.com',
        enabled: true
      },
      { params: { scope: 'instance' } }
    )
    expect(api.post).toHaveBeenCalledWith(
      '/integrations/music/sonobin/sonobin-id/test', null, { params: { scope: 'instance' } }
    )
    expect(api.delete).toHaveBeenCalledWith(
      '/integrations/music/sonobin/sonobin-id', { params: { scope: 'instance' } }
    )
    expect(store.music).toEqual(musicResponse)
  })

  it('loads retained personal integrations regardless of catalog availability', async () => {
    const responses = {
      '/integrations/search': searchResponse,
      '/integrations/calendar': calendarResponse,
      '/integrations/mail': { configured: false, accounts: [] },
      '/integrations/knowledge': knowledgeResponse,
      '/integrations/recipes': recipesResponse,
      '/integrations/infrastructure/arcane': { configured: false, instances: [] },
      '/integrations/music/sonobin': musicResponse,
      '/integrations/openscad': openScadResponse,
      '/integrations/coding': codingResponse,
      '/integrations/browser': { configured: false, instances: [] },
      '/integrations/support/tbs-desk': { configured: false, accounts: [] }
    }
    api.get.mockImplementation((url) => Promise.resolve({ data: responses[url] }))
    const store = useIntegrationStore()
    store.selectScope('personal')

    await store.fetchAll()

    expect(api.get).toHaveBeenCalledTimes(Object.keys(responses).length)
    for (const path of Object.keys(responses)) {
      if (path === '/integrations/support/tbs-desk') expect(api.get).toHaveBeenCalledWith(path)
      else {
        expect(api.get).toHaveBeenCalledWith(path, {
          params: { scope: 'personal' },
          ...(['/integrations/coding', '/integrations/browser'].includes(path) ? { suppressErrorToast: true } : {})
        })
      }
    }
    expect(store.search.providers).toHaveLength(1)
    expect(store.openscad.instances).toHaveLength(1)
    expect(store.coding.instances).toHaveLength(1)
  })

  it('loads and dispatches OpenSCAD CRUD, discovery and test operations', async () => {
    api.get.mockResolvedValue({ data: openScadResponse })
    api.post.mockImplementation((url) => Promise.resolve({
      data: url.endsWith('/discover')
        ? { available: true, executablePath: '/opt/openscad', version: '2025.01', headlessPreview: true }
        : { status: 'ok' }
    }))
    api.put.mockResolvedValue({})
    api.delete.mockResolvedValue({})
    const store = useIntegrationStore()
    const payload = {
      executionAgentId: 'agent-1',
      name: 'OpenSCAD',
      config: { binaryPath: '/usr/bin/openscad' },
      enabled: true
    }

    await store.fetchOpenScad()
    await store.createOpenScad(payload)
    await store.updateOpenScad('openscad-id', payload)
    const discovered = await store.discoverOpenScad('agent-1')
    await store.testOpenScad('openscad-id')
    await store.removeOpenScad('openscad-id')

    expect(api.get).toHaveBeenCalledWith('/integrations/openscad', { params: { scope: 'instance' } })
    expect(api.post).toHaveBeenCalledWith('/integrations/openscad', payload, { params: { scope: 'instance' } })
    expect(api.put).toHaveBeenCalledWith('/integrations/openscad/openscad-id', payload, { params: { scope: 'instance' } })
    expect(api.post).toHaveBeenCalledWith(
      '/integrations/openscad/discover',
      { executionAgentId: 'agent-1' },
      { params: { scope: 'instance' } }
    )
    expect(api.post).toHaveBeenCalledWith(
      '/integrations/openscad/openscad-id/test', null, { params: { scope: 'instance' } }
    )
    expect(api.delete).toHaveBeenCalledWith(
      '/integrations/openscad/openscad-id', { params: { scope: 'instance' } }
    )
    expect(discovered.executablePath).toBe('/opt/openscad')
    expect(store.openscad.instances).toEqual(openScadResponse.instances)
  })

  it('keeps loading active until concurrent integration requests finish', async () => {
    let resolveSearch
    let resolveCalendar
    api.get.mockImplementation((url) => new Promise((resolve) => {
      if (url === '/integrations/search') resolveSearch = resolve
      else resolveCalendar = resolve
    }))
    const store = useIntegrationStore()

    const search = store.fetchSearch()
    const calendar = store.fetchCalendar()
    expect(store.loading).toBe(true)

    resolveSearch({ data: searchResponse })
    await search
    expect(store.loading).toBe(true)
    expect(store.pendingLoads).toBe(1)

    resolveCalendar({ data: calendarResponse })
    await calendar
    expect(store.loading).toBe(false)
    expect(store.pendingLoads).toBe(0)
  })

  it('discards a late integration response from the previous scope', async () => {
    const requests = []
    api.get.mockImplementation((_url, config) => new Promise((resolve) => {
      requests.push({ scope: config.params.scope, resolve })
    }))
    const store = useIntegrationStore()

    const instanceLoad = store.fetchSearch()
    store.selectScope('personal')
    const personalLoad = store.fetchSearch()
    requests[1].resolve({ data: { ...searchResponse, providers: [{ id: 'personal' }] } })
    await personalLoad
    requests[0].resolve({ data: { ...searchResponse, providers: [{ id: 'instance' }] } })
    await instanceLoad

    expect(store.scope).toBe('personal')
    expect(store.search.providers).toEqual([{ id: 'personal' }])
    expect(store.loading).toBe(false)
    expect(store.pendingLoads).toBe(0)
  })

  it('keeps no-key search available independently of personal adapter availability', async () => {
    api.get.mockResolvedValue({ data: searchResponse })
    const store = useIntegrationStore()
    store.selectScope('personal')

    await store.fetchEffectiveSearch()

    expect(api.get).toHaveBeenCalledWith('/integrations/search', { params: { scope: 'personal' } })
    expect(api.get).not.toHaveBeenCalledWith('/integrations/search', { params: { scope: 'instance' } })
    expect(store.search.providers).toHaveLength(1)
    expect(store.search.effectiveEnabled).toBe(true)
  })

  it('loads and saves resource policies for one integration', async () => {
    const access = {
      integrationId: 'integration-id',
      defaultResourceKey: 'calendar:work',
      defaultTaskListKey: null,
      actions: [
        {
          capability: 'calendar.read',
          label: 'Lesen',
          allowedPolicies: ['disabled', 'ask', 'auto'],
          policy: 'auto'
        }
      ],
      resources: [
        { key: 'calendar:work', groupKey: 'work', label: 'Arbeit' }
      ]
    }
    api.get.mockResolvedValue({ data: access })
    api.put.mockResolvedValue({ data: access })
    const store = useIntegrationStore()

    await store.fetchAccess('integration-id')
    await store.saveAccess(
      'integration-id',
      { defaultResourceKey: 'calendar:work', defaultTaskListKey: null }
    )

    expect(api.get).toHaveBeenCalledWith(
      '/integrations/integration-id/access', { params: { scope: 'instance' } }
    )
    expect(api.put).toHaveBeenCalledWith('/integrations/integration-id/access', {
      defaultResourceKey: 'calendar:work',
      defaultTaskListKey: null
    }, { params: { scope: 'instance' } })
  })

  it('keeps integration access on the selected personal scope', async () => {
    api.get.mockResolvedValue({ data: { integrationId: 'personal-id' } })
    api.put.mockResolvedValue({ data: { integrationId: 'personal-id' } })
    const store = useIntegrationStore()
    store.selectScope('personal')

    await store.fetchAccess('personal-id')
    await store.saveAccess('personal-id', { defaultResourceKey: null, defaultTaskListKey: null })

    expect(api.get).toHaveBeenCalledWith(
      '/integrations/personal-id/access', { params: { scope: 'personal' } }
    )
    expect(api.put).toHaveBeenCalledWith(
      '/integrations/personal-id/access',
      { defaultResourceKey: null, defaultTaskListKey: null },
      { params: { scope: 'personal' } }
    )
  })

  it('discards access responses from a stale scope', async () => {
    const requests = []
    api.get.mockImplementation((_url, config) => new Promise((resolve) => {
      requests.push({ params: config.params, resolve })
    }))
    const store = useIntegrationStore()

    const load = store.fetchAccess('integration-id')
    store.selectScope('personal')
    requests[0].resolve({ data: { integrationId: 'integration-id' } })

    await expect(load).resolves.toBeNull()
    expect(requests[0].params).toEqual({ scope: 'instance' })
  })
})
