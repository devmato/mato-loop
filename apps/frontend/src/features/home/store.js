import { defineStore } from 'pinia'
import { markRaw } from 'vue'
import { api } from 'src/boot/axios'

export const useHomeStore = defineStore('home', {
  state: () => ({ sessions: [], loading: false, loaded: false, loadFailed: false, loadRequest: null }),
  actions: {
    async load(config = {}) {
      if (this.loadRequest) await this.loadRequest
      if (this.loading) return
      this.loading = true
      this.loadFailed = false
      try {
        this.loadRequest = markRaw(api.get('/sessions', { ...config, params: { pageSize: 6, sort: 'recent' } }))
        const { data } = await this.loadRequest
        this.sessions = data.items
        this.loaded = true
      } catch (error) {
        this.loadFailed = true
        throw error
      } finally {
        this.loading = false
        this.loadRequest = null
      }
    }
  }
})
