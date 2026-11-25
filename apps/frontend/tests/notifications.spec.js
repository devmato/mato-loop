import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  create: vi.fn()
}))

vi.mock('quasar', () => ({
  Notify: { create: mocks.create }
}))

import { backendUnavailable, notifyBackendReachable, notifyBackendUnavailable } from '../src/utils/notifications'

describe('backend availability notification', () => {
  afterEach(() => {
    notifyBackendReachable()
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('ignores brief failures and clears a confirmed outage only after recovery', async () => {
    vi.useFakeTimers()
    const probe = vi.fn().mockResolvedValueOnce(true).mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    notifyBackendUnavailable(probe)
    await vi.advanceTimersByTimeAsync(1500)
    expect(backendUnavailable.value).toBe(false)

    notifyBackendUnavailable(probe)
    notifyBackendUnavailable(probe)
    await vi.advanceTimersByTimeAsync(1499)
    expect(backendUnavailable.value).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(backendUnavailable.value).toBe(true)
    await vi.advanceTimersByTimeAsync(5000)
    expect(backendUnavailable.value).toBe(false)
    expect(probe).toHaveBeenCalledTimes(3)
    expect(mocks.create).not.toHaveBeenCalled()
  })

  it('reports an unconfirmed write after a proxy timeout when the server is still reachable', async () => {
    vi.useFakeTimers()
    notifyBackendUnavailable(async () => true, { response: { status: 504 }, config: { method: 'post' } })
    await vi.advanceTimersByTimeAsync(1500)
    expect(mocks.create.mock.calls).toEqual([[{
      type: 'negative',
      message: 'Der Ausgang der Aktion ist unklar. Prüfe zuerst, ob die Änderung übernommen wurde, bevor du es erneut versuchst.'
    }]])
  })
})
