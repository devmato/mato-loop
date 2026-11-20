import { defineStore } from 'pinia'
import { markRaw } from 'vue'
import { api } from 'src/boot/axios'

export const DEFAULT_SESSION_TABS_LIMIT = 10
export const MAX_SESSION_TABS_LIMIT = 20

export const useUserPreferencesStore = defineStore('user-preferences', {
  state: () => ({ preferredLanguage: null, sessionsPosition: 'belowProjects', sessionsPageSize: 8, sessionTabsEnabled: false, sessionTabsPosition: 'bottom', sessionTabsOrder: 'project', sessionTabsLimit: DEFAULT_SESSION_TABS_LIMIT, trashEnabled: false, trashRetentionDays: 30, loading: false, loaded: false, loadFailed: false, saving: false, loadRequest: null }),
  actions: {
    applySettings(data) {
      this.preferredLanguage = data.preferredLanguage
      this.sessionsPosition = data.sessionsPosition ?? 'belowProjects'
      this.sessionsPageSize = data.sessionsPageSize ?? 8
      this.sessionTabsEnabled = data.sessionTabsEnabled ?? false
      this.sessionTabsPosition = data.sessionTabsPosition ?? 'bottom'
      this.sessionTabsOrder = data.sessionTabsOrder ?? 'project'
      this.sessionTabsLimit = Math.min(data.sessionTabsLimit ?? DEFAULT_SESSION_TABS_LIMIT, MAX_SESSION_TABS_LIMIT)
      this.trashEnabled = data.trashEnabled ?? false
      this.trashRetentionDays = data.trashRetentionDays ?? 30
    },
    async fetchSettings() {
      if (this.loadRequest) return this.loadRequest
      this.loading = true
      this.loadFailed = false
      const request = api.get('/users/me/preferences').then(({ data }) => {
        this.applySettings(data)
        this.loaded = true
        return data
      })
      this.loadRequest = markRaw(request)
      try {
        return await request
      } catch (error) {
        this.loadFailed = true
        throw error
      } finally {
        this.loading = false
        this.loadRequest = null
      }
    },
    async saveSettings(preferences) {
      this.saving = true
      try {
        const { data } = await api.put('/users/me/preferences', preferences)
        this.applySettings(data)
        return data
      } finally { this.saving = false }
    }
  }
})
