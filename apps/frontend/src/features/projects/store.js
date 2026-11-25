import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'
import { queueRefresh } from 'src/utils/queued-refresh'
import { reconcileApiRows } from 'src/utils/api-snapshot'

const pendingSummaryIds = new WeakMap()
const fullRefreshRequests = new WeakMap()

export const useProjectStore = defineStore('projects', {
  state: () => ({
    items: [],
    loading: false,
    loaded: false,
    loadFailed: false,
    reordering: false,
    revision: 0
  }),
  getters: {
    iconOrder: ({ items }) => new Map([...items]
      .sort((a, b) => (Date.parse(a.createdAt) || 0) - (Date.parse(b.createdAt) || 0) || a.id.localeCompare(b.id))
      .map(({ id }, index) => [id, index]))
  },
  actions: {
    async reorder(projectIds) {
      if (this.reordering) return
      this.reordering = true
      try {
        await api.put('/projects/order', { projectIds })
        this.revision += 1
        this.loading = false
        const positions = new Map(projectIds.map((id, index) => [id, index]))
        this.items = [...this.items].sort((a, b) => (
          (positions.get(a.id) ?? positions.size) - (positions.get(b.id) ?? positions.size)
        ))
      } finally {
        this.reordering = false
      }
    },
    async fetchAll(config = {}) {
      const request = queueRefresh(this, config, async (revision) => {
        this.loading = !this.loaded
        this.loadFailed = false
        try {
          const { data } = await api.get('/projects', { timeout: 30_000, ...config })
          if (revision !== this.revision) return
          this.items = reconcileApiRows(this.items, data.items)
          this.loaded = true
          pendingSummaryIds.get(this)?.clear()
        } catch (error) {
          if (revision === this.revision) this.loadFailed = true
          throw error
        } finally {
          if (revision === this.revision) this.loading = false
        }
      })
      fullRefreshRequests.set(this, request)
      try {
        await request
      } finally {
        if (fullRefreshRequests.get(this) === request) fullRefreshRequests.delete(this)
      }
    },
    async refreshSummaries(ids, config = {}) {
      const requested = [...new Set(ids.filter(Boolean))]
      if (!requested.length) return
      // A targeted status read must not supersede a canonical list update.
      if (!this.loaded || fullRefreshRequests.has(this)) return this.fetchAll(config)
      if (!pendingSummaryIds.has(this)) pendingSummaryIds.set(this, new Set())
      const pending = pendingSummaryIds.get(this)
      requested.forEach(id => pending.add(id))
      this.loading = false
      return queueRefresh(this, { ...config, targeted: true }, async (revision) => {
        // Keep superseded requests' IDs until a current read has applied them.
        const projectIds = [...pending]
        const existing = new Set(this.items.map(item => item.id))
        const summaries = new Map()
        for (let offset = 0; offset < projectIds.length; offset += 100) {
          const ids = projectIds.slice(offset, offset + 100)
          const params = new URLSearchParams(config.params)
          // A newly observed project needs its full defaults and settings.
          params.set('view', ids.every(id => existing.has(id)) ? 'status' : 'full')
          params.delete('projectIds')
          ids.forEach(id => params.append('projectIds', id))
          const { data } = await api.get('/projects', { timeout: 30_000, ...config, params })
          if (revision !== this.revision) return
          data.items.forEach(item => summaries.set(item.id, item))
        }
        const targets = new Set(projectIds)
        const next = this.items
          .filter(item => !targets.has(item.id) || summaries.has(item.id))
          .map(item => targets.has(item.id) ? { ...item, ...summaries.get(item.id) } : item)
        next.push(...[...summaries.values()].filter(item => !existing.has(item.id)))
        this.items = reconcileApiRows(this.items, next)
        projectIds.forEach(id => pending.delete(id))
      })
    },
    async create(name, instructions = '', defaults = {}, config) {
      const { data } = await api.post('/projects', {
        name,
        instructions,
        ...defaults
      }, config)
      this.revision += 1
      this.loading = false
      this.items = [...this.items, data]
      return data
    },
    async update(id, name, instructions = '', defaults = {}, config) {
      const { data } = await api.patch(`/projects/${id}`, { name, instructions, ...defaults }, config)
      this.revision += 1
      this.loading = false
      this.items = this.items.map((project) => project.id === id ? data : project)
      return data
    },
    async setArchived(id, archived) {
      const { data } = await api.post(`/projects/${id}/archive`, { archived })
      await this.fetchAll()
      return data
    },
    async remove(id, { permanent = false, automationMode = null, automationImpact, suppressErrorToast = false } = {}) {
      const config = {
        ...(suppressErrorToast ? { suppressErrorToast: true } : {}),
        params: {
          permanent,
          ...(automationMode ? { automationMode } : {}),
          ...(automationImpact ? { automationImpact } : {})
        }
      }
      await api.delete(`/projects/${id}`, config)
      this.revision += 1
      this.loading = false
      this.items = this.items.filter((project) => project.id !== id)
    },
    async deleteImpact(id, { suppressErrorToast = false } = {}) {
      const { data } = await api.get(
        `/projects/${id}/delete-impact`,
        suppressErrorToast ? { suppressErrorToast: true } : undefined
      )
      return data
    },
    async listCodingBindings(integrationId) {
      const { data } = await api.get(`/projects/coding/integrations/${integrationId}/projects`)
      return data.items
    },
    async setCodingBinding(id, binding) {
      const { data } = await api.put(`/projects/${id}/coding/binding`, binding)
      this.revision += 1
      this.loading = false
      this.items = this.items.map((project) => (
        project.id === id ? { ...project, codingService: data } : project
      ))
      return data
    },
    async clearCodingBinding(id) {
      await api.delete(`/projects/${id}/coding/binding`)
      this.revision += 1
      this.loading = false
      this.items = this.items.map((project) => (
        project.id === id ? { ...project, codingService: null } : project
      ))
    }
  }
})
