import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('src/utils/notifications', () => ({
  notifyBackendReachable: vi.fn(),
  notifyBackendUnavailable: vi.fn(),
  notifyError: vi.fn()
}))

import { api, recoverControlledAuth } from '../src/boot/axios'
import { useAuthStore } from '../src/stores/auth'
import { useChatStore } from '../src/features/chat/store'
import { useVoiceStore } from '../src/features/voice/store'
import { notifyBackendUnavailable, notifyError } from '../src/utils/notifications'

function storage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key)
  }
}

describe('Axios auth recovery', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
    global.window = {
      localStorage: storage(),
      location: { reload: vi.fn(), replace: vi.fn() }
    }
  })

  afterEach(() => {
    vi.restoreAllMocks()
    delete global.window
  })

  it.each([
    ['Service Unavailable', true],
    [{ error: { code: 'http.serviceUnavailable' } }, false]
  ])('distinguishes a proxy outage from a structured API error (%j)', async (data, unavailable) => {
    const error = { code: 'ERR_BAD_RESPONSE', response: { status: 503, data }, config: {} }

    await expect(api.get('/test', { adapter: () => Promise.reject(error) })).rejects.toBe(error)

    expect([notifyBackendUnavailable.mock.calls.length, notifyError.mock.calls.length])
      .toEqual(unavailable ? [1, 0] : [0, 1])
  })

  it('refreshes identity after an admin denial but ignores invalid login credentials', async () => {
    const auth = useAuthStore()
    auth.authenticated = true
    auth.userId = 'user-1'
    auth.isAdmin = true
    const fetchMe = vi.spyOn(auth, 'fetchMe').mockImplementation((config) => {
      auth.applyIdentity({ userId: 'user-1', username: 'viktor', isAdmin: false }, true)
      return Promise.resolve(config)
    })

    await recoverControlledAuth({
      config: {},
      response: { data: { error: { code: 'auth.invalidCredentials' } } }
    })
    expect(fetchMe).not.toHaveBeenCalled()

    await recoverControlledAuth({
      config: {},
      response: { data: { error: { code: 'auth.adminRequired' } } }
    })

    expect(fetchMe).toHaveBeenCalledWith({
      authRefresh: true,
      suppressErrorToast: true
    })
    expect(auth.isAdmin).toBe(false)
    expect(window.location.reload).toHaveBeenCalledOnce()
  })

  it('invalidates an expired session with active voice only once without authenticated cleanup requests', async () => {
    const auth = useAuthStore()
    auth.authenticated = true
    auth.userId = 'user-1'
    useVoiceStore().$patch({ phase: 'listening', sessionId: 'chat', connectionId: 'connection' })
    useChatStore().current = { id: 'chat', messages: [] }
    const request = vi.fn().mockResolvedValue({ data: { id: 'chat', messages: [] } })
    vi.spyOn(api, 'delete').mockImplementation(request)
    vi.spyOn(api, 'get').mockImplementation(request)
    const error = {
      config: {},
      response: { data: { error: { code: 'auth.sessionExpired' } } }
    }

    await Promise.all([recoverControlledAuth(error), recoverControlledAuth(error)])

    expect(auth.authenticated).toBe(false)
    expect(window.location.replace).toHaveBeenCalledOnce()
    expect(window.location.replace).toHaveBeenCalledWith('/login')
    expect(request).not.toHaveBeenCalled()
  })
})
