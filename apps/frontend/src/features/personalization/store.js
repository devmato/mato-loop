import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'

export const userProfileFields = [
  'fullName',
  'preferredName',
  'formOfAddress',
  'pronouns',
  'primaryLanguage',
  'primaryResidence'
]
export const userProfileAudiences = ['both', 'privateOnly', 'publicOnly']

function emptyValue() {
  return { both: null, privateOnly: null, publicOnly: null }
}

function normalizeProfile(value) {
  const result = {}
  for (const field of userProfileFields) {
    result[field] = emptyValue()
    for (const audience of userProfileAudiences) {
      result[field][audience] = typeof value?.[field]?.[audience] === 'string'
        ? value[field][audience]
        : null
    }
  }
  result.phoneNumbers = Array.isArray(value?.phoneNumbers)
    ? value.phoneNumbers.map(({ label, number }) => ({ label, number }))
    : []
  return result
}

export function profileFactDraft(value = emptyValue()) {
  return {
    privateValue: value.privateOnly ?? value.both ?? null,
    publicValue: value.publicOnly ?? value.both ?? null,
    shared: value.both != null && value.privateOnly == null && value.publicOnly == null
  }
}

export function profileFactPayload(value) {
  const privateValue = value.privateValue?.trim() || null
  const publicValue = value.publicValue?.trim() || null
  return value.shared
    ? { both: privateValue, privateOnly: null, publicOnly: null }
    : { both: null, privateOnly: privateValue, publicOnly: publicValue }
}

export const useUserProfileStore = defineStore('user-profile', {
  state: () => ({
    ...normalizeProfile(),
    loaded: false,
    loadFailed: false,
    loading: false,
    saving: false,
    requestRevision: 0
  }),
  actions: {
    assignProfile(value, invalidateRequests = true) {
      if (invalidateRequests) this.requestRevision += 1
      Object.assign(this, normalizeProfile(value))
      this.loaded = true
      this.loadFailed = false
    },
    snapshot() {
      return normalizeProfile(this)
    },
    async fetchProfile(options) {
      const requestRevision = ++this.requestRevision
      this.loading = true
      this.loadFailed = false
      try {
        const { data } = await api.get('/user-profile', options)
        if (requestRevision === this.requestRevision) this.assignProfile(data, false)
        return this.snapshot()
      } catch (error) {
        if (requestRevision === this.requestRevision) this.loadFailed = true
        throw error
      } finally {
        if (requestRevision === this.requestRevision) this.loading = false
      }
    },
    async saveProfile(changes) {
      const requestRevision = ++this.requestRevision
      this.saving = true
      try {
        const { data } = await api.patch('/user-profile', changes)
        if (requestRevision === this.requestRevision) this.assignProfile(data, false)
        return this.snapshot()
      } finally {
        if (requestRevision === this.requestRevision) this.saving = false
      }
    }
  }
})
