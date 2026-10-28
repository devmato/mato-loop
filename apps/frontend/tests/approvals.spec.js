import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('src/boot/axios', () => ({
  api: { delete: vi.fn(), get: vi.fn(), post: vi.fn() }
}))

import { useApprovalStore } from '../src/stores/approvals'
import { api } from 'src/boot/axios'

describe('inline approvals', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('removes an approval after a successful decision', async () => {
    api.post.mockResolvedValue({ data: { status: 'approved' } })
    const approvals = useApprovalStore()
    approvals.add({ id: 'approval-1', sessionId: 'session-1' })

    await approvals.decide('approval-1', 'allow_once')

    expect(approvals.pending).toEqual([])
    expect(api.post).toHaveBeenCalledWith('/approvals/approval-1/decisions', {
      decision: 'allow_once'
    })
  })

  it('loads pending approvals with request options and exposes their count', async () => {
    api.get.mockResolvedValue({ data: [{ id: 'approval-1' }] })
    const approvals = useApprovalStore()

    await approvals.fetchPending({ suppressErrorToast: true })

    expect(api.get).toHaveBeenCalledWith('/approvals/pending', { timeout: 30_000, suppressErrorToast: true })
    expect(approvals.pendingCount).toBe(1)
    expect(approvals.loaded).toBe(true)
    expect(approvals.loadFailed).toBe(false)
  })

  it('counts memory approvals globally but excludes them from session approvals', () => {
    const approvals = useApprovalStore()
    approvals.add({ id: 'agent', requestType: 'agent_action', sessionId: 'session-1' })
    approvals.add({ id: 'memory', requestType: 'memory_change', sessionId: 'session-1' })

    expect(approvals.pendingCount).toBe(2)
    expect(approvals.forSession('session-1').map((item) => item.id)).toEqual(['agent'])
  })

  it('does not reload previously viewed grants when loading pending approvals', async () => {
    api.get.mockResolvedValue({ data: [] })
    const approvals = useApprovalStore()
    approvals.grantsLoaded = true

    await approvals.fetchPending()

    expect(api.get).toHaveBeenCalledExactlyOnceWith('/approvals/pending', { timeout: 30_000 })
  })

  it('does not overwrite an approval added while pending approvals load', async () => {
    let resolveRequest
    api.get.mockReturnValue(new Promise((resolve) => { resolveRequest = resolve }))
    const approvals = useApprovalStore()
    const request = approvals.fetchPending()
    const refresh = approvals.fetchPending()

    approvals.add({ id: 'approval-new', sessionId: 'session-1' })
    resolveRequest({ data: [] })
    await Promise.all([request, refresh])

    expect(approvals.pending).toEqual([{ id: 'approval-new', sessionId: 'session-1' }])
  })

  it('sends the expected draft revision only for allow decisions', async () => {
    api.post.mockResolvedValue({ data: { status: 'approved' } })
    const approvals = useApprovalStore()
    approvals.add({ id: 'allow', sessionId: 'session-1' })
    approvals.add({ id: 'deny', sessionId: 'session-1' })
    approvals.add({ id: 'approve', requestType: 'memory_change' })

    await approvals.decide('allow', 'allow_once', 'revision-2')
    await approvals.decide('deny', 'deny', 'revision-2')
    await approvals.decide('approve', 'approve', 'revision-2')

    expect(api.post).toHaveBeenNthCalledWith(1, '/approvals/allow/decisions', {
      decision: 'allow_once', expected_revision: 'revision-2'
    })
    expect(api.post).toHaveBeenNthCalledWith(2, '/approvals/deny/decisions', {
      decision: 'deny'
    })
    expect(api.post).toHaveBeenNthCalledWith(3, '/approvals/approve/decisions', {
      decision: 'approve'
    })
  })

  it('submits edited arguments together with a one-time decision', async () => {
    api.post.mockResolvedValue({ data: { status: 'approved' } })
    const approvals = useApprovalStore()
    approvals.add({ id: 'edit', sessionId: 'session-1' })

    await approvals.decide('edit', 'allow_once', undefined, undefined, {
      revision: 'a'.repeat(64),
      edits: [{ path: ['command'], value: 'pwd' }]
    })

    expect(api.post).toHaveBeenCalledWith('/approvals/edit/decisions', {
      decision: 'allow_once',
      argument_revision: 'a'.repeat(64),
      argument_edits: [{ path: ['command'], value: 'pwd' }]
    })
  })

  it('does not preview an invalid child draft path or a non-session root', async () => {
    const approvals = useApprovalStore()
    for (const draftFile of [
      { root: 'session', path: '../parent.json' },
      { root: 'workspace', path: 'draft.json' }
    ]) {
      expect(await approvals.previewSkillDraft({
        actionKind: 'build_skill', payload: { subagentSessionId: 'child' }, details: { draftFile }
      })).toBeNull()
    }
    expect(api.get).not.toHaveBeenCalled()
  })

  it('keeps an approval visible when the decision fails', async () => {
    api.post.mockRejectedValue(new Error('request failed'))
    const approvals = useApprovalStore()
    const approval = { id: 'approval-1', sessionId: 'session-1' }
    approvals.add(approval)

    await expect(approvals.decide('approval-1', 'deny')).rejects.toThrow('request failed')

    expect(approvals.pending).toEqual([approval])
  })

  it.each([
    'approval.runInactive',
    'approval.targetChanged',
    'approval.decisionUnavailable',
    'approval.policyChanged'
  ])('reconciles terminal approval error %s', async (code) => {
    api.post.mockRejectedValue({ response: { data: { error: { code } } } })
    api.get.mockResolvedValue({ data: [] })
    const approvals = useApprovalStore()
    approvals.add({ id: 'approval-1', sessionId: 'session-1' })

    await expect(approvals.decide('approval-1', 'deny')).rejects.toBeTruthy()

    expect(api.get).toHaveBeenCalledWith('/approvals/pending', { timeout: 30_000 })
    expect(approvals.pending).toEqual([])
  })

  it('loads grants from all sessions', async () => {
    api.get.mockResolvedValue({ data: [{ id: 'grant-1', sessionId: 'session-1' }] })
    const approvals = useApprovalStore()

    await approvals.fetchGrants({ suppressErrorToast: true })

    expect(api.get).toHaveBeenCalledWith('/approvals/permissions', { suppressErrorToast: true })
    expect(approvals.grants).toEqual([{ id: 'grant-1', sessionId: 'session-1' }])
    expect(approvals.grantsLoaded).toBe(true)
    expect(approvals.grantsLoadFailed).toBe(false)
  })

  it('exposes only session-wide grants for the selected session', () => {
    const approvals = useApprovalStore()
    approvals.grants = [
      { id: 'grant-1', sessionId: 'session-1' },
      { id: 'grant-2', sessionId: 'session-2' },
      { id: 'grant-3', sessionId: 'session-1', runId: 'run-1' }
    ]

    expect(approvals.grantsForSession('session-1')).toEqual([
      { id: 'grant-1', sessionId: 'session-1' }
    ])
    expect(approvals.grantsForSession(null)).toEqual([])
  })

  it('removes a revoked grant from the global list', async () => {
    api.delete.mockResolvedValue({})
    const approvals = useApprovalStore()
    approvals.grants = [{ id: 'grant-1', sessionId: 'session-1' }]

    await approvals.revoke('session-1', 'grant-1')

    expect(api.delete).toHaveBeenCalledWith('/approvals/sessions/session-1/permissions/grant-1')
    expect(approvals.grants).toEqual([])
  })

  it('keeps a grant visible when revoking fails', async () => {
    api.delete.mockRejectedValue(new Error('request failed'))
    const approvals = useApprovalStore()
    const grant = { id: 'grant-1', sessionId: 'session-1' }
    approvals.grants = [grant]

    await expect(approvals.revoke('session-1', 'grant-1')).rejects.toThrow('request failed')

    expect(approvals.grants).toEqual([grant])
  })

  it('removes only session-wide grants of one session after bulk revoke', async () => {
    api.delete.mockResolvedValue({})
    const approvals = useApprovalStore()
    approvals.grants = [
      { id: 'grant-1', sessionId: 'session-1' },
      { id: 'grant-2', sessionId: 'session-1' },
      { id: 'grant-3', sessionId: 'session-2' },
      { id: 'grant-4', sessionId: 'session-1', runId: 'run-1' },
      { id: 'temporary:read', sessionId: 'session-1', derived: true },
      { id: 'session-secret:token', sessionId: 'session-1', readOnly: true }
    ]

    await approvals.revokeAll('session-1')

    expect(api.delete).toHaveBeenCalledWith('/approvals/sessions/session-1/permissions')
    expect(approvals.grants).toEqual([
      { id: 'grant-3', sessionId: 'session-2' },
      { id: 'grant-4', sessionId: 'session-1', runId: 'run-1' },
      { id: 'temporary:read', sessionId: 'session-1', derived: true },
      { id: 'session-secret:token', sessionId: 'session-1', readOnly: true }
    ])
  })

  it('keeps all grants visible when bulk revoke fails', async () => {
    api.delete.mockRejectedValue(new Error('request failed'))
    const approvals = useApprovalStore()
    const grants = [
      { id: 'grant-1', sessionId: 'session-1' },
      { id: 'grant-2', sessionId: 'session-1' }
    ]
    approvals.grants = grants

    await expect(approvals.revokeAll('session-1')).rejects.toThrow('request failed')

    expect(approvals.grants).toEqual(grants)
  })
})
