import { createPinia, setActivePinia } from 'pinia'
import { expect, it, vi } from 'vitest'
import { useApprovalStore } from '../src/features/approvals/store'
import { useInAppNotificationStore } from '../src/features/notifications/store'
import { useProjectStore } from '../src/features/projects/store'
import { useQuestionStore } from '../src/features/questions/store'

const { api } = vi.hoisted(() => ({ api: { get: vi.fn() } }))
vi.mock('src/boot/axios', () => ({ api }))

it.each([
  [useProjectStore, 'fetchAll', 'items', (items) => ({ items })],
  [useApprovalStore, 'fetchPending', 'pending', (items) => items],
  [useQuestionStore, 'fetchPending', 'pending', (items) => items],
  [useInAppNotificationStore, 'fetchLatest', 'items', (items) => ({ items })]
])('batches overlapping %s reads and retains a fresh follow-up', async (useStore, action, field, response) => {
  setActivePinia(createPinia())
  let finishFirst
  api.get.mockReset().mockReturnValueOnce(new Promise((resolve) => { finishFirst = resolve }))
    .mockResolvedValue({ data: response([{ id: 'new' }]) })
  const store = useStore()
  const first = store[action]()
  const second = store[action]()
  const third = store[action]()
  finishFirst({ data: response([{ id: 'old' }]) })
  await Promise.all([first, second, third])
  expect([api.get.mock.calls.length, store[field]]).toEqual([2, [{ id: 'new' }]])
})

it('retains an invalidation after a local mutation supersedes the queued read', async () => {
  setActivePinia(createPinia())
  let finishFirst
  api.get.mockReset().mockReturnValueOnce(new Promise((resolve) => { finishFirst = resolve }))
    .mockResolvedValue({ data: [{ id: 'new' }] })
  const store = useApprovalStore()
  const first = store.fetchPending()
  const second = store.fetchPending()
  store.add({ id: 'local' })
  const third = store.fetchPending()
  finishFirst({ data: [] })
  await Promise.all([first, second, third])
  expect(store.pending).toEqual([{ id: 'new' }])
})

it('releases a stalled read on its HTTP timeout and runs the queued reload', async () => {
  setActivePinia(createPinia())
  vi.useFakeTimers()
  api.get.mockReset().mockImplementationOnce((url, { timeout }) => new Promise((resolve, reject) => {
    if (timeout) setTimeout(() => reject(new Error('timeout')), timeout)
  }))
    .mockResolvedValue({ data: [] })
  const store = useApprovalStore()
  const failed = expect(store.fetchPending()).rejects.toThrow('timeout')
  const refresh = store.fetchPending()
  try {
    await vi.advanceTimersByTimeAsync(30_000)
    await Promise.all([failed, refresh])
    expect(store.loaded).toBe(true)
  } finally {
    vi.useRealTimers()
  }
})
