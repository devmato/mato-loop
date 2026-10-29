import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'

function list(value, key) {
  return Array.isArray(value) ? value : value?.[key] || value?.items || []
}

function normalizeAccount(account) {
  return { ...account, id: account.id || account.integrationId }
}

function normalizeMailbox(mailbox) {
  const flags = (mailbox.flags || []).map((flag) => flag.toLowerCase())
  const role = mailbox.role
    || (flags.some((flag) => flag.includes('inbox')) ? 'inbox' : flags.some((flag) => flag.includes('draft')) ? 'drafts' : null)
  return { ...mailbox, key: mailbox.key || mailbox.name, label: mailbox.label || mailbox.name, role }
}

function normalizeMessage(message) {
  const flags = (message.flags || []).map((flag) => flag.toLowerCase())
  return {
    ...message,
    token: message.token || message.messageToken,
    unread: message.unread ?? !flags.some((flag) => flag.includes('seen')),
    isDraft: message.isDraft ?? flags.some((flag) => flag.includes('draft')),
    attachments: (message.attachments || []).map((attachment) => ({
      ...attachment,
      token: attachment.token || attachment.attachmentToken
    }))
  }
}

function multipart(payload, replaceAttachments = false) {
  const data = new FormData()
  for (const field of ['to', 'cc', 'bcc']) {
    for (const value of payload[field] || []) data.append(field, value)
  }
  data.append('subject', payload.subject || '')
  data.append('body', payload.body || '')
  for (const attachment of payload.existingAttachments || []) {
    data.append('retainedAttachmentToken', attachment.token)
  }
  for (const file of payload.files || []) data.append('attachments', file, file.name)
  if (replaceAttachments) data.append('replaceAttachments', 'true')
  return data
}

export const useMailStore = defineStore('mail', {
  state: () => ({
    accounts: [],
    accountsLoaded: false,
    accountsLoading: false,
    accountsLoadFailed: false,
    accountId: null,
    mailboxKey: null,
    query: null,
    mailboxes: [],
    mailboxesLoading: false,
    mailboxesLoadFailed: false,
    messages: [],
    nextCursor: null,
    messagesLoading: false,
    messagesLoadFailed: false,
    detail: null,
    detailLoading: false,
    detailLoadFailed: false,
    accountsVersion: 0,
    mailboxesVersion: 0,
    messagesVersion: 0,
    detailVersion: 0
  }),
  actions: {
    async fetchAccounts() {
      const version = ++this.accountsVersion
      this.accountsLoading = true
      this.accountsLoadFailed = false
      try {
        const { data } = await api.get('/mail/accounts')
        if (version !== this.accountsVersion) return
        this.accounts = list(data, 'accounts').map(normalizeAccount)
        this.accountsLoaded = true
        return this.accounts
      } catch (error) {
        if (version === this.accountsVersion) this.accountsLoadFailed = true
        throw error
      } finally {
        if (version === this.accountsVersion) this.accountsLoading = false
      }
    },
    async fetchMailboxes(accountId) {
      const version = ++this.mailboxesVersion
      if (this.accountId !== accountId) {
        this.mailboxes = []
        this.messages = []
        this.nextCursor = null
        this.clearDetail()
      }
      this.accountId = accountId
      this.mailboxesLoading = true
      this.mailboxesLoadFailed = false
      try {
        const { data } = await api.get(`/mail/accounts/${accountId}/mailboxes`)
        if (version !== this.mailboxesVersion || this.accountId !== accountId) return
        this.mailboxes = list(data, 'mailboxes').map(normalizeMailbox)
        return this.mailboxes
      } catch (error) {
        if (version === this.mailboxesVersion) this.mailboxesLoadFailed = true
        throw error
      } finally {
        if (version === this.mailboxesVersion) this.mailboxesLoading = false
      }
    },
    async fetchMessages(accountId, mailboxKey, cursor = null, query = null) {
      query = query?.trim() || null
      const version = ++this.messagesVersion
      const append = Boolean(cursor && this.accountId === accountId && this.mailboxKey === mailboxKey && this.query === query)
      if (!append && (this.accountId !== accountId || this.mailboxKey !== mailboxKey || this.query !== query)) {
        this.messages = []
        this.nextCursor = null
        this.clearDetail()
      }
      this.accountId = accountId
      this.mailboxKey = mailboxKey
      this.query = query
      this.messagesLoading = true
      this.messagesLoadFailed = false
      try {
        const { data } = await api.get(`/mail/accounts/${accountId}/messages`, {
          params: { mailboxKey, ...(append ? { cursor } : {}), ...(query ? { query } : {}) }
        })
        if (
          version !== this.messagesVersion
          || this.accountId !== accountId
          || this.mailboxKey !== mailboxKey
          || this.query !== query
        ) return
        const items = list(data, 'messages').map(normalizeMessage)
        this.messages = append ? [...this.messages, ...items] : items
        this.nextCursor = data?.nextCursor ?? null
        return items
      } catch (error) {
        if (version === this.messagesVersion) this.messagesLoadFailed = true
        throw error
      } finally {
        if (version === this.messagesVersion) this.messagesLoading = false
      }
    },
    async fetchMessage(accountId, token) {
      const version = ++this.detailVersion
      this.detailLoading = true
      this.detailLoadFailed = false
      try {
        const { data } = await api.get(`/mail/accounts/${accountId}/messages/${encodeURIComponent(token)}`)
        if (version !== this.detailVersion || this.accountId !== accountId) return
        this.detail = normalizeMessage(data?.message || data)
        return this.detail
      } catch (error) {
        if (version === this.detailVersion) this.detailLoadFailed = true
        throw error
      } finally {
        if (version === this.detailVersion) this.detailLoading = false
      }
    },
    clearDetail() {
      this.detailVersion += 1
      this.detailLoading = false
      this.detailLoadFailed = false
      this.detail = null
    },
    async saveDraft(accountId, token, payload) {
      const path = `/mail/accounts/${accountId}/drafts${token ? `/${encodeURIComponent(token)}` : ''}`
      const body = multipart(payload, Boolean(token))
      const { data } = token
        ? await api.put(path, body)
        : await api.post(path, body)
      const draft = normalizeMessage(data?.draft || data?.message || data)
      if (this.accountId === accountId) this.detail = draft
      return draft
    },
    async deleteDraft(accountId, token) {
      await api.delete(`/mail/accounts/${accountId}/drafts/${encodeURIComponent(token)}`)
      this.removeConfirmed(token)
    },
    async sendDraft(accountId, token, payload, idempotencyKey) {
      const { data } = await api.post(
        `/mail/accounts/${accountId}/drafts/${encodeURIComponent(token)}/send`,
        multipart(payload),
        { headers: { 'Idempotency-Key': idempotencyKey } }
      )
      this.removeConfirmed(token)
      return data
    },
    async sendMessage(accountId, payload, idempotencyKey) {
      return (await api.post(
        `/mail/accounts/${accountId}/messages/send`,
        multipart(payload),
        { headers: { 'Idempotency-Key': idempotencyKey } }
      )).data
    },
    async deleteMessage(accountId, token) {
      await api.delete(`/mail/accounts/${accountId}/messages/${encodeURIComponent(token)}`)
      this.removeConfirmed(token)
    },
    removeConfirmed(token) {
      this.messages = this.messages.filter((message) => message.token !== token)
      if (this.detail?.token === token) this.detail = null
    }
  }
})
