import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'
import { queueRefresh } from 'src/utils/queued-refresh'

export const useProjectStore = defineStore('projects', {
  state: () => ({
    items: [],
    loading: false,
    loaded: false,
    loadFailed: false,
    revision: 0
  }),
  actions: {
    async fetchAll(config = {}) {
      return queueRefresh(this, config, async (revision) => {
        this.loading = !this.loaded
        this.loadFailed = false
        try {
          const { data } = await api.get('/projects', { timeout: 30_000, ...config })
          if (revision !== this.revision) return
          this.items = data.items
          this.loaded = true
        } catch (error) {
          if (revision === this.revision) this.loadFailed = true
          throw error
        } finally {
          if (revision === this.revision) this.loading = false
        }
      })
    },
    async create(name, instructions = '', defaults = {}) {
      const { data } = await api.post('/projects', {
        name,
        instructions,
        ...defaults
      })
      this.revision += 1
      this.loading = false
      this.items = [...this.items, data]
      return data
    },
    async update(id, name, instructions = '', defaults = {}) {
      const { data } = await api.patch(`/projects/${id}`, { name, instructions, ...defaults })
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
    async remove(id, { automationMode = null, automationImpact, suppressErrorToast = false } = {}) {
      const config = {
        ...(suppressErrorToast ? { suppressErrorToast: true } : {}),
        params: {
          ...(automationMode ? { automationMode } : {}),
          automationImpact
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
