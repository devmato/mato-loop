import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { api } = vi.hoisted(() => ({
  api: { delete: vi.fn(), get: vi.fn(), patch: vi.fn(), put: vi.fn() }
}))

vi.mock('src/boot/axios', () => ({ api }))

import { useToolSettingsStore } from '../src/stores/tool-settings'

const settings = () => ({
  groups: [{
    id: 'web',
    label: 'Web',
    icon: 'language',
    tools: [{
      name: 'webfetch',
      label: 'Webfetch',
      source: { type: 'builtin' },
      available: false,
      actions: [{ capability: 'network.fetch', configurable: true }]
    }]
  }],
  transcribeAudio: { timeoutSeconds: 180 }
})

const configuration = {
  groups: [{
    id: 'web',
    label: 'Web',
    icon: 'language',
    tools: [{
      name: 'webfetch',
      label: 'Webfetch',
      icon: 'language',
      available: true
    }]
  }],
  toolAvailability: { webfetch: true },
  webAccess: { userAgent: 'MatoLoop/1.0', maxResponseMiB: 32, publicAddressesOnly: false },
  transcribeAudio: { timeoutSeconds: 180 }
}

describe('tool settings store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('loads and saves admin-only tool configuration without scope parameters', async () => {
    api.get.mockResolvedValue({ data: configuration })
    api.put.mockResolvedValue({ data: configuration })
    const store = useToolSettingsStore()
    const payload = {
      toolAvailability: { webfetch: true },
      webFetch: configuration.webFetch,
      webAccess: configuration.webAccess
    }

    await store.fetchConfiguration()
    await store.saveConfiguration(payload)

    expect(api.get).toHaveBeenCalledWith('/tools/configuration')
    expect(api.put).toHaveBeenCalledWith('/tools/configuration', payload)
    expect(store.configurationGroups).toEqual(configuration.groups)
    expect(store.configurationWebAccess).toEqual(configuration.webAccess)
  })

  it('defaults shared web access to public addresses when the server omits the option', () => {
    const store = useToolSettingsStore()

    store.assignConfiguration({
      ...configuration,
      webAccess: { userAgent: 'MatoLoop/1.0', maxResponseMiB: 32 }
    })

    expect(store.configurationWebAccess.publicAddressesOnly).toBe(true)
  })

  it('updates loaded tools after technical availability changes', () => {
    const store = useToolSettingsStore()
    store.assignSettings({
      ...settings(),
      groups: [{
        ...settings().groups[0],
        tools: [{
          ...settings().groups[0].tools[0],
          available: true,
          actions: settings().groups[0].tools[0].actions.map((action) => ({
            ...action,
            available: true
          }))
        }]
      }]
    })

    store.assignConfiguration({
      ...configuration,
      toolAvailability: { webfetch: false }
    })

    expect(store.groups[0].tools[0].available).toBe(false)
    expect(store.transcribeAudio.timeoutSeconds).toBe(180)
  })

  it('discards an older configuration response', async () => {
    const requests = []
    api.get.mockImplementation(() => new Promise((resolve) => requests.push(resolve)))
    const store = useToolSettingsStore()

    const first = store.fetchConfiguration()
    const second = store.fetchConfiguration()
    requests[1]({ data: configuration })
    await second
    requests[0]({ data: { ...configuration, groups: [] } })
    await first

    expect(store.configurationGroups).toEqual(configuration.groups)
  })

  it('keeps the narrow availability endpoint for dictation state', async () => {
    api.get.mockResolvedValue({ data: { tools: { transcribe_audio: true } } })
    const store = useToolSettingsStore()

    await store.fetchAvailability()

    expect(api.get).toHaveBeenCalledWith('/tools/availability')
    expect(store.transcribeAudioAvailable).toBe(true)
  })

})
