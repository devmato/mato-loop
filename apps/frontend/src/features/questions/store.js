import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'
import { errorDescriptor } from 'src/utils/api-errors'

const TERMINAL_ANSWER_ERRORS = new Set([
  'question.runInactive',
  'question.alreadyAnswered',
  'question.notFound'
])

export const useQuestionStore = defineStore('questions', {
  state: () => ({
    pending: [],
    submittingId: null,
    drafts: {},
    loading: false,
    loaded: false,
    loadFailed: false,
    revision: 0
  }),
  getters: {
    forSession: (state) => (sessionId) => state.pending.filter((item) => item.sessionId === sessionId)
  },
  actions: {
    async fetchPending(config = {}) {
      const revision = ++this.revision
      this.loading = true
      this.loadFailed = false
      try {
        const { data } = await api.get('/questions/pending', config)
        if (revision !== this.revision || !Array.isArray(data)) return
        this.pending = data
        this.loaded = true
        const pendingIds = new Set(this.pending.map((item) => item.id))
        for (const id of Object.keys(this.drafts)) {
          if (!pendingIds.has(id)) delete this.drafts[id]
        }
      } catch (error) {
        if (revision === this.revision) this.loadFailed = true
        throw error
      } finally {
        if (revision === this.revision) this.loading = false
      }
    },
    add(question) {
      if (this.pending.some((item) => item.id === question.id)) return
      this.revision += 1
      this.pending.push(question)
      this.loading = false
      this.loaded = true
      this.loadFailed = false
    },
    ensureDraft(request) {
      if (!this.drafts[request.id]) {
        this.drafts[request.id] = {
          index: 0,
          answers: Object.fromEntries(request.questions.map((question) => [question.id, []])),
          custom: {},
          customOn: {},
          rememberForSession: Object.fromEntries(
            request.questions.filter((question) => question.one_time === true).map((question) => [question.id, false])
          ),
          minimized: false
        }
      }
      return this.drafts[request.id]
    },
    async answer(id, answers, rememberForSession = []) {
      this.submittingId = id
      try {
        await api.post(`/questions/${id}/answers`, {
          answers,
          ...(rememberForSession.length ? { remember_for_session: rememberForSession } : {})
        })
        this.revision += 1
        this.pending = this.pending.filter((item) => item.id !== id)
        delete this.drafts[id]
      } catch (error) {
        if (TERMINAL_ANSWER_ERRORS.has(errorDescriptor(error)?.code)) {
          await this.fetchPending()
        }
        throw error
      } finally {
        this.submittingId = null
      }
    },
    async reject(id) {
      this.submittingId = id
      try {
        await api.post(`/questions/${id}/reject`)
        this.revision += 1
        this.pending = this.pending.filter((item) => item.id !== id)
        delete this.drafts[id]
      } catch (error) {
        if (TERMINAL_ANSWER_ERRORS.has(errorDescriptor(error)?.code)) {
          await this.fetchPending()
        }
        throw error
      } finally {
        this.submittingId = null
      }
    }
  }
})
