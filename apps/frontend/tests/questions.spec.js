import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('src/boot/axios', () => ({
  api: { get: vi.fn(), post: vi.fn() }
}))

import { useQuestionStore } from '../src/stores/questions'
import { useApprovalStore } from '../src/stores/approvals'
import { api } from 'src/boot/axios'

describe('agent questions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('removes a question after answers were accepted', async () => {
    api.post.mockResolvedValue({ data: { status: 'answered' } })
    const questions = useQuestionStore()
    questions.add({ id: 'question-1', sessionId: 'session-1' })
    const answers = { format: ['Kurz'], topics: ['API'], note: [] }

    await questions.answer('question-1', answers)

    expect(api.post).toHaveBeenCalledWith('/questions/question-1/answers', { answers })
    expect(questions.pending).toEqual([])
  })

  it('submits selected fields for temporary session reuse', async () => {
    api.post.mockResolvedValue({ data: { status: 'answered' } })
    const questions = useQuestionStore()
    questions.add({ id: 'question-1', sessionId: 'session-1' })
    const answers = { SSH_USERNAME: ['viktor'], SSH_PASSWORD: ['secret'] }

    await questions.answer('question-1', answers, ['SSH_USERNAME'])

    expect(api.post).toHaveBeenCalledWith('/questions/question-1/answers', {
      answers,
      remember_for_session: ['SSH_USERNAME']
    })
  })

  it('leaves previously loaded grants to the visible SSE consumer after administrator consent', async () => {
    api.post.mockResolvedValue({ data: { status: 'answered' } })
    api.get.mockResolvedValue({ data: [] })
    const approvals = useApprovalStore()
    approvals.grantsLoaded = true
    const questions = useQuestionStore()
    questions.add({ id: 'admin', questions: [{ id: 'PASSWORD', administrator: { alias: 'AI-PC' } }] })
    await questions.answer('admin', { PASSWORD: ['secret'] }, ['PASSWORD'])
    expect(api.get).not.toHaveBeenCalled()
  })

  it('ignores malformed pending responses', async () => {
    api.get.mockResolvedValue({ data: {} })
    const questions = useQuestionStore()

    await questions.fetchPending()

    expect(questions.pending).toEqual([])
  })

  it('forwards request options while loading pending questions', async () => {
    api.get.mockResolvedValue({ data: [] })
    const questions = useQuestionStore()

    await questions.fetchPending({ suppressErrorToast: true })

    expect(api.get).toHaveBeenCalledWith('/questions/pending', { suppressErrorToast: true })
    expect(questions.loaded).toBe(true)
    expect(questions.loadFailed).toBe(false)
  })

  it('keeps events received while pending questions are loading', async () => {
    let resolveFetch
    api.get.mockReturnValue(new Promise((resolve) => { resolveFetch = resolve }))
    const questions = useQuestionStore()
    const fetch = questions.fetchPending()

    questions.add({ id: 'question-1', sessionId: 'session-1' })
    resolveFetch({ data: [] })
    await fetch

    expect(questions.pending).toHaveLength(1)
  })

  it('keeps a question visible when submission fails', async () => {
    api.post.mockRejectedValue(new Error('request failed'))
    const questions = useQuestionStore()
    const question = { id: 'question-1', sessionId: 'session-1' }
    questions.add(question)

    await expect(questions.answer(question.id, { note: ['Text'] })).rejects.toThrow('request failed')

    expect(questions.pending).toEqual([question])
  })

  it('keeps navigation and answers while switching sessions', () => {
    const questions = useQuestionStore()
    const request = {
      id: 'question-1',
      questions: [
        { id: 'format', type: 'single_choice' },
        { id: 'note', type: 'free_text', one_time: true }
      ]
    }
    const draft = questions.ensureDraft(request)
    draft.index = 1
    draft.answers.format = ['Kurz']

    expect(questions.ensureDraft(request)).toMatchObject({
      index: 1,
      answers: { format: ['Kurz'], note: [] },
      rememberForSession: { note: false }
    })
  })

  it('rejects a pending question', async () => {
    api.post.mockResolvedValue({ data: { status: 'rejected' } })
    const questions = useQuestionStore()
    questions.add({ id: 'question-1', sessionId: 'session-1' })

    await questions.reject('question-1')

    expect(api.post).toHaveBeenCalledWith('/questions/question-1/reject')
    expect(questions.pending).toEqual([])
  })
})
