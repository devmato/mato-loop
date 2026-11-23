import { defineStore } from 'pinia'
import { markRaw } from 'vue'
import { api } from 'src/boot/axios'
import { queueRefresh } from 'src/utils/queued-refresh'
import { reconcileApiRows } from 'src/utils/api-snapshot'
import { errorDescriptor } from 'src/utils/api-errors'
import { validRelativePath } from 'src/features/files/attachments'

const TERMINAL_DECISION_ERRORS = new Set([
  'approval.policyChanged',
  'approval.requestChanged',
  'approval.runInactive',
  'approval.targetChanged',
  'approval.decisionUnavailable',
  'approval.alreadyDecided',
  'approval.notFound'
])

export const useApprovalStore = defineStore('approvals', {
  state: () => ({
    pending: [],
    submittingId: null,
    loading: false,
    loaded: false,
    loadFailed: false,
    revision: 0,
    grants: [],
    grantsLoading: false,
    grantsLoaded: false,
    grantsLoadFailed: false,
    grantRevision: 0,
    grantRefreshPromise: null,
    grantSessionId: null
  }),
  getters: {
    pendingCount: (state) => state.pending.length,
    grantsForSession: (state) => (sessionId) => state.grants.filter((item) => (
      item.sessionId === sessionId && !item.runId
    )),
    forSession: (state) => (sessionId) => state.pending.filter((item) => (
      item.sessionId === sessionId
    ))
  },
  actions: {
    async previewSkillDraft(approval) {
      const sessionId = approval?.payload?.subagentSessionId
      const draft = approval?.details?.draftFile
      if (!sessionId || !['build_skill', 'edit_skill'].includes(approval?.actionKind)
        || draft?.root !== 'session' || !validRelativePath(draft.path)) return null
      // An omitted root is resolved by the backend from the child, not the active parent.
      return (await api.get('/workspace/files/preview', {
        params: {
          scope: 'session', session_id: sessionId, path: draft.path,
          ...(draft.rootIdentity ? { root_identity: draft.rootIdentity } : {})
        }
      })).data
    },
    async fetchPending(config = {}) {
      return queueRefresh(this, config, async (revision) => {
        this.loading = true
        this.loadFailed = false
        try {
          const { data } = await api.get('/approvals/pending', { timeout: 30_000, ...config })
          if (revision !== this.revision || !Array.isArray(data)) return
          this.pending = reconcileApiRows(this.pending, data)
          this.loaded = true
        } catch (error) {
          if (revision === this.revision) this.loadFailed = true
          throw error
        } finally {
          if (revision === this.revision) this.loading = false
        }
      })
    },
    add(approval) {
      if (this.pending.some((item) => item.id === approval.id)) return
      this.revision += 1
      this.pending.push(approval)
      this.loading = false
      this.loaded = true
      this.loadFailed = false
    },
    remove(id) {
      if (!id) return
      this.revision += 1
      this.pending = this.pending.filter((item) => item.id !== id)
      this.loading = false
    },
    async decide(id, decision, expectedRevision, memoryEdit, argumentEdit, grantOption) {
      this.submittingId = id
      try {
        await api.post(`/approvals/${id}/decisions`, {
          decision,
          ...(decision.startsWith('allow') && grantOption != null ? { grant_option: grantOption } : {}),
          ...(decision === 'approve' && memoryEdit != null ? { memory_edit: memoryEdit } : {}),
          ...(decision.startsWith('allow') && expectedRevision != null
            ? { expected_revision: expectedRevision }
            : {}),
          ...(argumentEdit
            ? {
                argument_edits: argumentEdit.edits,
                argument_revision: argumentEdit.revision
              }
            : {})
        })
        this.remove(id)
      } catch (error) {
        if (TERMINAL_DECISION_ERRORS.has(errorDescriptor(error)?.code)) {
          await this.fetchPending()
        }
        throw error
      } finally {
        this.submittingId = null
      }
    },
    async fetchGrants(config = {}, sessionId = this.grantSessionId) {
      this.grantSessionId = sessionId
      let request = queueRefresh(this, { ...config, sessionId }, async (revision) => {
        this.grantsLoading = true
        this.grantsLoadFailed = false
        try {
          const { data } = await api.get(sessionId ? `/approvals/sessions/${sessionId}/permissions` : '/approvals/permissions', { timeout: 30_000, ...config })
          if (revision !== this.grantRevision || !Array.isArray(data)) return
          this.grants = data
          this.grantsLoaded = true
          return true
        } catch (error) {
          if (revision === this.grantRevision) this.grantsLoadFailed = true
          throw error
        } finally {
          if (revision === this.grantRevision) this.grantsLoading = false
        }
      }, 'grantRevision')
      this.grantRefreshPromise = markRaw(request)
      let loaded
      // A view waiting for grants must also wait for invalidations received during its read.
      do {
        request = this.grantRefreshPromise
        try {
          loaded = await request
        } catch (error) {
          if (this.grantRefreshPromise === request) throw error
        }
      } while (this.grantRefreshPromise !== request)
      return loaded && this.grantSessionId === sessionId
    },
    async revoke(sessionId, grantId) {
      const resource = grantId.startsWith('session-secret:')
        ? `secrets/${grantId.slice('session-secret:'.length)}`
        : `permissions/${grantId}`
      await api.delete(`/approvals/sessions/${sessionId}/${resource}`)
      this.grantRevision += 1
      this.grants = this.grants.filter((item) => item.id !== grantId)
      this.grantsLoading = false
    },
    async revokeAll(sessionId) {
      await api.delete(`/approvals/sessions/${sessionId}/permissions`)
      this.grantRevision += 1
      this.grants = this.grants.filter((item) => item.sessionId !== sessionId || item.runId || item.derived || item.readOnly || item.source === 'session_input')
      this.grantsLoading = false
    }
  }
})
