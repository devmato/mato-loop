import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'

export const useUserPreferencesStore = defineStore('user-preferences', {
  state: () => ({ preferredLanguage: null, loading: false, loaded: false, loadFailed: false, saving: false }),
  actions: {
    async fetchSettings() {
      this.loading = true
      this.loadFailed = false
      try {
        const { data } = await api.get('/users/me/preferences')
        this.preferredLanguage = data.preferredLanguage
        this.loaded = true
        return data
      } catch (error) {
        this.loadFailed = true
        throw error
      } finally { this.loading = false }
    },
    async saveSettings(preferredLanguage) {
      this.saving = true
      try {
        const { data } = await api.put('/users/me/preferences', { preferredLanguage })
        this.preferredLanguage = data.preferredLanguage
        return data
      } finally { this.saving = false }
    }
  }
})
