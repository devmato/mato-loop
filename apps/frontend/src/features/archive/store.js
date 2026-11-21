import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'

export const useArchiveStore = defineStore('archive', {
  state: () => ({ projects: [], sessionLists: {}, trashSessionLists: {}, trash: { projects: [], sessions: [] }, loading: false, failed: false }),
  actions: {
    async load(mode = 'archive') {
      this.loading = true
      this.failed = false
      try {
        if (mode === 'trash') {
          const { data } = await api.get('/trash')
          this.trash = data
          this.trashSessionLists = {}
          return
        }
        const [{ data }] = await Promise.all([api.get('/projects', { params: { archived: true } }), this.loadSessions()])
        this.projects = data.items
      } catch {
        this.failed = true
      } finally { this.loading = false }
    },
    async restoreTrash(kind, id) {
      const { data } = await api.post(`/trash/${kind}/${id}/restore`)
      return data
    },
    async loadTrashSessions(projectId, more = false) {
      const list = this.trashSessionLists[projectId]
      const { data } = await api.get(`/trash/projects/${projectId}/sessions`, { params: {
        ...(more && list?.nextCursor ? { cursor: list.nextCursor } : {})
      } })
      this.trashSessionLists[projectId] = { ...data, items: more ? [...(list?.items || []), ...data.items] : data.items }
    },
    async loadSessions(projectId = null, more = false) {
      const key = projectId || 'sessions'
      const list = this.sessionLists[key] || { items: [], nextCursor: null }
      const { data } = await api.get('/sessions', { params: {
        archived: true, pageSize: 20, ...(projectId ? { projectId } : {}),
        ...(more && list.nextCursor ? { cursor: list.nextCursor } : {})
      } })
      this.sessionLists[key] = { items: more ? [...list.items, ...data.items] : data.items, nextCursor: data.nextCursor }
    }
  }
})
