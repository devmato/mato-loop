import { defineStore } from 'pinia'
import { markRaw } from 'vue'
import { api } from 'src/boot/axios'
import { reorderedTabs } from 'src/shared/ui/useTabReorder'
import { queueRefresh } from 'src/utils/queued-refresh'
import { useUserPreferencesStore } from 'src/features/settings/user-preferences-store'
import { equalSessionSummaryValue } from './state'

export const sessionTabKey = (sessionId, projectId = null) => sessionId || `draft:${projectId || 'standalone'}`
export const sessionTabTarget = (tab) => ({
  name: 'chat',
  query: {
    ...(tab.projectId ? { project: tab.projectId } : {}),
    ...(tab.sessionId ? { session: tab.sessionId } : { new: '1' })
  }
})

export const useSessionTabsStore = defineStore('session-tabs', {
  state: () => ({ items: [], summaries: {}, summaryVersions: {}, summaryRevision: 0, readingPositions: {}, owner: null, loaded: false, loading: false, loadRequest: null,
    followLatestRequest: null, lastVisitedKey: null, visitClock: 0, controlCenterOpen: false, confirmed: { sessionTabs: [], controlCenterOpen: false }, pending: null, saving: null, revision: 0, generation: 0 }),
  actions: {
    async setOwner(owner) {
      if (this.owner === owner) return
      const generation = this.generation + 1, summaryRevision = this.summaryRevision + 1
      this.$reset()
      this.generation = generation
      this.summaryRevision = summaryRevision
      this.owner = owner
      if (owner) await this.fetchWorkspace()
    },
    fetchWorkspace() {
      if (this.loadRequest) return this.loadRequest
      if (!this.owner || this.saving) return Promise.resolve()
      const generation = this.generation, revision = this.revision
      this.loading = true
      const request = (async () => { try {
        const { data } = await api.get('/users/me/workspace-state')
        if (generation !== this.generation || revision !== this.revision) return
        this.applyWorkspace(data)
        this.confirmed = data
        this.loaded = true
      } finally { if (generation === this.generation) { this.loading = false; this.loadRequest = null } }
      })()
      this.loadRequest = markRaw(request)
      return request
    },
    applyWorkspace(data) {
      const localUsage = new Map(this.items.map(tab => [tab.key, tab.lastUsedAt ?? 0]))
      this.items = (data.sessionTabs || []).map((tab, index) => {
        const key = sessionTabKey(tab.sessionId, tab.projectId)
        // Older workspaces only have their saved list order.
        return { ...tab, key, addedAt: tab.addedAt || index + 1, lastUsedAt: Math.max(tab.lastUsedAt ?? 0, localUsage.get(key) ?? 0) }
      })
      this.controlCenterOpen = data.controlCenterOpen === true
    },
    applySummaryUpdates(ids, summaries) {
      let next = this.summaries
      for (const id of ids) {
        // An equal targeted response must still supersede an older in-flight refresh.
        this.summaryVersions[id] = (this.summaryVersions[id] || 0) + 1
        if (equalSessionSummaryValue(next[id], summaries[id])) continue
        if (next === this.summaries) next = { ...next }
        if (summaries[id]) next[id] = summaries[id]
        else delete next[id]
      }
      if (next !== this.summaries) this.summaries = next
    },
    refreshSummaries() {
      return queueRefresh(this, {}, async revision => {
        const ids = this.items.map(tab => tab.sessionId).filter(Boolean)
        if (!this.owner || !ids.length) { this.summaries = {}; return }
        const params = new URLSearchParams({ pageSize: '100', includeArchived: 'true' })
        const versions = { ...this.summaryVersions }
        ids.forEach(id => params.append('sessionIds', id))
        const { data } = await api.get('/sessions', { params })
        if (revision === this.summaryRevision) {
          const next = Object.fromEntries((data.items || []).map(item => [item.id, item]))
          for (const id of ids) {
            if ((versions[id] || 0) === (this.summaryVersions[id] || 0)) continue
            if (this.summaries[id]) next[id] = this.summaries[id]
            else delete next[id]
          }
          this.summaries = next
        }
      }, 'summaryRevision')
    },
    saveWorkspace(patch) {
      if (!this.owner || !this.loaded) return Promise.resolve()
      this.revision += 1
      const pending = { ...this.pending, ...patch }
      if (patch.sessionTabs) {
        // A structural save already includes the latest local usage.
        delete pending.sessionTabUsage
      } else if (patch.sessionTabUsage) {
        const usage = new Map((this.pending?.sessionTabUsage || []).map(tab => [sessionTabKey(tab.sessionId, tab.projectId), tab]))
        for (const tab of patch.sessionTabUsage) usage.set(sessionTabKey(tab.sessionId, tab.projectId), tab)
        pending.sessionTabUsage = [...usage.values()]
      }
      this.pending = pending
      if (this.saving) return this.saving
      const generation = this.generation
      const request = Promise.resolve().then(async () => {
        try {
          while (this.generation === generation && this.pending) {
            const next = this.pending
            this.pending = null
            const { data } = await api.put('/users/me/workspace-state', next)
            if (this.generation !== generation) return
            this.confirmed = data
          }
          if (this.generation === generation) this.applyWorkspace(this.confirmed)
        } catch (error) {
          if (this.generation === generation) { this.pending = null; this.applyWorkspace(this.confirmed) }
          throw error
        } finally { if (this.generation === generation) this.saving = null }
      })
      this.saving = markRaw(request)
      return request
    },
    persistTabs() {
      const sessionTabs = this.items.map(({ sessionId, projectId, title, preview, addedAt, lastUsedAt }) => ({ sessionId, projectId, title, preview, addedAt, lastUsedAt }))
      void this.saveWorkspace({ sessionTabs }).catch(() => {})
    },
    async setControlCenterOpen(open) {
      const generation = this.generation
      if (this.owner && !this.loaded) await this.fetchWorkspace()
      if (generation !== this.generation) return
      if (open === this.controlCenterOpen) return Promise.resolve()
      this.controlCenterOpen = open
      return this.saveWorkspace({ controlCenterOpen: open })
    },
    visit(context, { keep = false } = {}) {
      const key = sessionTabKey(context.id, context.projectId)
      const limit = useUserPreferencesStore().sessionTabsLimit
      const existing = this.items.find(tab => tab.key === key)
      const markUsed = key !== this.lastVisitedKey || !existing
      if (markUsed) {
        this.lastVisitedKey = key
        this.visitClock = Math.max(Date.now(), this.visitClock + 1)
      }
      if (existing) {
        const title = context.title || 'Neue Session'
        const projectId = context.projectId || null
        const promoted = keep && existing.preview
        Object.assign(existing, { title, projectId, preview: existing.preview && !keep })
        if (markUsed) existing.lastUsedAt = Math.max(existing.lastUsedAt ?? 0, this.visitClock)
        if (promoted) this.persistTabs()
        else if (markUsed) {
          const { sessionId, projectId, lastUsedAt } = existing
          void this.saveWorkspace({ sessionTabUsage: [{ sessionId, projectId, lastUsedAt }] }).catch(() => {})
        }
        return existing
      }
      const tab = {
        key, sessionId: context.id || null, projectId: context.projectId || null,
        title: context.title || 'Neue Session', preview: !keep, addedAt: this.visitClock, lastUsedAt: this.visitClock
      }
      const preview = this.items.findIndex(item => item.preview)
      if (preview >= 0 && !keep) {
        delete this.readingPositions[this.items[preview].key]
        this.items.splice(preview, 1, tab)
      } else this.items.push(tab)
      if (this.items.length > limit) {
        const removed = new Set(this.items.filter(item => item.key !== key)
          .sort((a, b) => (a.lastUsedAt ?? 0) - (b.lastUsedAt ?? 0))
          .slice(0, this.items.length - limit).map(item => item.key))
        this.items = this.items.filter(item => !removed.has(item.key))
        for (const key of removed) delete this.readingPositions[key]
      }
      this.persistTabs()
      return tab
    },
    keep(key) {
      const tab = this.items.find(tab => tab.key === key)
      if (tab?.preview) { tab.preview = false; this.persistTabs() }
    },
    attachDraft(sessionId, projectId = null) {
      const draft = this.items.find(tab => tab.key === sessionTabKey(null, projectId))
      if (!draft) return
      if (this.items.some(tab => tab.key === sessionId)) this.close(draft.key)
      else Object.assign(draft, { key: sessionId, sessionId, preview: false })
      this.persistTabs()
    },
    close(key) {
      if (!this.items.some(tab => tab.key === key)) return
      this.items = this.items.filter(tab => tab.key !== key)
      delete this.readingPositions[key]
      this.persistTabs()
    },
    removeProject(projectId, sessionIds = []) {
      const ids = new Set(sessionIds)
      const removed = this.items.filter(tab => tab.projectId === projectId || ids.has(tab.sessionId))
      if (!removed.length) return
      this.items = this.items.filter(tab => !removed.includes(tab))
      for (const tab of removed) delete this.readingPositions[tab.key]
      this.persistTabs()
    },
    reorder(source, target, after = false) {
      const next = reorderedTabs(this.items, source, target, after, tab => tab.key)
      if (next === this.items) return
      this.items = next
      const moved = this.items.find(tab => tab.key === source)
      if (moved) moved.preview = false
      this.persistTabs()
    },
    move(key, offset) {
      const index = this.items.findIndex(tab => tab.key === key)
      const target = this.items[index + offset]
      if (index >= 0 && target) this.reorder(key, target.key, offset > 0)
    }
  }
})
