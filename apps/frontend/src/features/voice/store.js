import { defineStore } from 'pinia'
import { toRaw } from 'vue'
import { api } from 'src/boot/axios'
import { notifyError } from 'src/utils/notifications'
import { useChatStore } from 'src/features/chat/store'
import { createVoiceClient } from './client'

const clients = new WeakMap()

export const useVoiceStore = defineStore('voice', {
  state: () => ({ sessionId: null, connectionId: null, phase: 'ended', muted: false, wakeLockActive: null, tasks: [], generation: 0 }),
  getters: { active: (state) => state.phase !== 'ended' },
  actions: {
    async start(sessionId) {
      if (this.active) return
      const generation = ++this.generation
      this.sessionId = sessionId
      this.phase = 'starting'
      this.muted = false
      this.wakeLockActive = null
      this.tasks = []
      const client = createVoiceClient({
        negotiate: async (id, payload, signal) => {
          const { data } = await api.post(`/sessions/${id}/voice`, payload, { signal })
          if (generation !== this.generation && this.sessionId === id) {
            await api.delete(`/sessions/${id}/voice/${data.connectionId}`)
          } else if (generation === this.generation) this.connectionId = data.connectionId
          return data
        },
        onPhase: (phase) => { if (generation === this.generation) this.phase = phase },
        onWakeLock: (active) => { if (generation === this.generation) this.wakeLockActive = active },
        onControl: (state) => {
          if (generation !== this.generation) return
          if (Array.isArray(state.tasks)) this.tasks = state.tasks
        },
        onError: (code) => {
          if (generation !== this.generation) return
          notifyError({ error: { code } })
          void this.end()
        },
        onClose: () => { if (generation === this.generation) void this.end() }
      })
      clients.set(toRaw(this.$state), client)
      try { await client.start(sessionId) } catch {
        if (generation === this.generation) await this.end()
        // Axios already displayed the controlled API error.
      }
    },
    toggleMute() {
      this.muted = !this.muted
      clients.get(toRaw(this.$state))?.mute(this.muted)
    },
    interrupt() { clients.get(toRaw(this.$state))?.interrupt() },
    disposeLocal() {
      this.generation += 1
      clients.get(toRaw(this.$state))?.close()
      clients.delete(toRaw(this.$state))
      this.phase = 'ended'
      this.sessionId = null
      this.connectionId = null
      this.muted = false
      this.wakeLockActive = null
      this.tasks = []
    },
    async end() {
      if (this.phase === 'closing' || (!this.active && !this.connectionId)) return
      const sessionId = this.sessionId, connectionId = this.connectionId
      this.disposeLocal()
      const generation = this.generation
      this.sessionId = sessionId
      this.phase = 'closing'
      try {
        if (sessionId && connectionId) await api.delete(`/sessions/${sessionId}/voice/${connectionId}`)
      } catch (error) {
        if (!error?.isAxiosError) notifyError(error)
      } finally {
        if (generation === this.generation && sessionId && useChatStore().current?.id === sessionId) {
          try { await useChatStore().refreshSession(sessionId) } catch (error) {
            if (!error?.isAxiosError) notifyError(error)
          }
        }
        if (generation === this.generation) this.phase = 'ended'
      }
    }
  }
})
