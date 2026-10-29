import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { api } = vi.hoisted(() => ({
  api: { delete: vi.fn(), get: vi.fn(), post: vi.fn(), put: vi.fn() }
}))
vi.mock('src/boot/axios', () => ({ api }))

import { useMailStore } from '../src/stores/mail'

describe('mail store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('loads effective accounts and discards an older response', async () => {
    const requests = []
    api.get.mockImplementation(() => new Promise((resolve) => requests.push(resolve)))
    const store = useMailStore()

    const first = store.fetchAccounts()
    const second = store.fetchAccounts()
    requests[1]({ data: { accounts: [{ integrationId: 'new', name: 'Neu' }] } })
    await second
    requests[0]({ data: { accounts: [{ id: 'old', name: 'Alt' }] } })
    await first

    expect(api.get).toHaveBeenCalledWith('/mail/accounts')
    expect(store.accounts).toEqual([{ integrationId: 'new', id: 'new', name: 'Neu' }])
    expect(store.accountsLoaded).toBe(true)
    expect(store.accountsLoading).toBe(false)
  })

  it('keeps confirmed accounts after a failed refresh', async () => {
    const store = useMailStore()
    store.accounts = [{ id: 'mail-1', name: 'Arbeit' }]
    store.accountsLoaded = true
    api.get.mockRejectedValue(new Error('offline'))

    await expect(store.fetchAccounts()).rejects.toThrow('offline')

    expect(store.accounts).toEqual([{ id: 'mail-1', name: 'Arbeit' }])
    expect(store.accountsLoadFailed).toBe(true)
  })

  it('loads paginated messages and ignores a stale mailbox response', async () => {
    const requests = []
    api.get.mockImplementation((_url, config) => new Promise((resolve) => requests.push({ config, resolve })))
    const store = useMailStore()

    const inbox = store.fetchMessages('mail-1', 'inbox')
    const archive = store.fetchMessages('mail-1', 'archive')
    requests[1].resolve({ data: { messages: [{ token: 'archive-1' }], nextCursor: 'next' } })
    await archive
    requests[0].resolve({ data: { messages: [{ token: 'inbox-1' }] } })
    await inbox

    expect(requests[1].config).toEqual({ params: { mailboxKey: 'archive' } })
    expect(store.messages).toEqual([{
      token: 'archive-1', unread: true, isDraft: false, attachments: []
    }])
    expect(store.nextCursor).toBe('next')
  })

  it('keeps the server search query when loading the next page', async () => {
    api.get.mockResolvedValueOnce({ data: { messages: [{ token: 'first' }], nextCursor: 'next' } })
      .mockResolvedValueOnce({ data: { messages: [{ token: 'second' }], nextCursor: null } })
    const store = useMailStore()
    await store.fetchMessages('mail-1', 'INBOX', null, 'Werkstatt')
    await store.fetchMessages('mail-1', 'INBOX', store.nextCursor, store.query)

    expect(api.get).toHaveBeenLastCalledWith('/mail/accounts/mail-1/messages', {
      params: { mailboxKey: 'INBOX', query: 'Werkstatt', cursor: 'next' }
    })
    expect(store.messages.map((message) => message.token)).toEqual(['first', 'second'])
  })

  it('clears old results and cursor when the search changes', async () => {
    api.get.mockResolvedValueOnce({ data: { messages: [{ token: 'old' }], nextCursor: 'next' } })
      .mockResolvedValueOnce({ data: { messages: [{ token: 'new' }] } })
    const store = useMailStore()
    await store.fetchMessages('mail-1', 'INBOX', null, 'old')
    await store.fetchMessages('mail-1', 'INBOX', store.nextCursor, 'new')

    expect(store.messages.map((message) => message.token)).toEqual(['new'])
    expect(api.get).toHaveBeenLastCalledWith('/mail/accounts/mail-1/messages', {
      params: { mailboxKey: 'INBOX', query: 'new' }
    })
  })

  it('preserves a draft after a failed delete and removes it only after confirmation', async () => {
    const store = useMailStore()
    store.messages = [{ token: 'draft-1', isDraft: true }]
    store.detail = store.messages[0]
    api.delete.mockRejectedValueOnce(new Error('failed')).mockResolvedValueOnce({})

    await expect(store.deleteDraft('mail-1', 'draft-1')).rejects.toThrow('failed')
    expect(store.messages).toHaveLength(1)
    expect(store.detail.token).toBe('draft-1')

    await store.deleteDraft('mail-1', 'draft-1')
    expect(api.delete).toHaveBeenLastCalledWith('/mail/accounts/mail-1/drafts/draft-1')
    expect(store.messages).toEqual([])
    expect(store.detail).toBeNull()
  })

  it('sends multipart mail with retained attachments and an idempotency key', async () => {
    api.post.mockResolvedValue({ data: { status: 'sent' } })
    const store = useMailStore()
    const file = new File(['hello'], 'hello.txt', { type: 'text/plain' })

    await store.sendMessage('mail-1', {
      to: ['a@example.com', 'b@example.com'], cc: [], bcc: [], subject: 'Hallo',
      body: 'Text', files: [file],
      existingAttachments: [{ token: 'attachment-1' }, { token: 'attachment-2' }]
    }, 'send-key')

    const [path, data, config] = api.post.mock.calls[0]
    expect(path).toBe('/mail/accounts/mail-1/messages/send')
    expect(data.getAll('to')).toEqual(['a@example.com', 'b@example.com'])
    expect(data.getAll('retainedAttachmentToken')).toEqual(['attachment-1', 'attachment-2'])
    expect(data.get('attachments').name).toBe('hello.txt')
    expect(config).toEqual({ headers: { 'Idempotency-Key': 'send-key' } })
    expect(api.get).not.toHaveBeenCalled()
  })

  it('sends a draft with an idempotency key', async () => {
    api.post.mockResolvedValue({ data: { status: 'sent' } })
    const store = useMailStore()

    await store.sendDraft('mail-1', 'draft/1', {
      to: ['recipient@example.com'], subject: 'Hallo', body: 'Text'
    }, 'draft-key')

    const [path, data, config] = api.post.mock.calls[0]
    expect(path).toBe('/mail/accounts/mail-1/drafts/draft%2F1/send')
    expect(data.getAll('to')).toEqual(['recipient@example.com'])
    expect(data.get('subject')).toBe('Hallo')
    expect(config).toEqual({ headers: { 'Idempotency-Key': 'draft-key' } })
  })

  it('explicitly replaces attachments when updating a draft', async () => {
    api.put.mockResolvedValue({ data: { messageToken: 'draft-2' } })
    const store = useMailStore()

    await store.saveDraft('mail-1', 'draft-1', {
      existingAttachments: [], files: [], subject: '', body: ''
    })

    expect(api.put.mock.calls[0][1].get('replaceAttachments')).toBe('true')
  })
})
