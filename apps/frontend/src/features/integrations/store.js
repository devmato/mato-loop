import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'
import { notifyError } from 'src/utils/notifications'
import { useAuthStore } from 'stores/auth'
import { browserStatusLabel } from 'src/utils/browser'

const emptySearch = () => ({
  configured: false,
  enabled: true,
  effectiveEnabled: true,
  unavailableReason: null,
  providers: []
})

const emptyCalendar = () => ({
  configured: false,
  accounts: []
})

const emptyMail = () => ({
  configured: false,
  accounts: []
})

const emptyTbsDesk = () => ({
  configured: false,
  accounts: []
})

const emptyKnowledge = () => ({
  configured: false,
  bases: []
})

const emptyRecipes = () => ({
  configured: false,
  accounts: []
})

const emptyArcane = () => ({ configured: false, instances: [] })

const emptyMusic = () => ({
  configured: false,
  instances: []
})

const emptyOpenScad = () => ({
  configured: false,
  instances: []
})

const emptyCoding = () => ({
  configured: false,
  instances: []
})
const unavailableCodingCodes = new Set([
  'integration.adapterDisabled',
  'integration.personalAdapterDisabled',
  'integration.unavailable'
])

function scopeConfig(scope, config = {}) {
  return { ...config, params: { ...(config.params || {}), scope } }
}

function scopeContext(store) {
  return { scope: store.scope, generation: store.scopeGeneration }
}

function contextIsCurrent(store, context) {
  return store.scope === context.scope && store.scopeGeneration === context.generation
}

function clearIntegrations(store) {
  store.webfetch = { providers: [] }
  store.search = emptySearch()
  store.calendar = emptyCalendar()
  store.mail = emptyMail()
  store.tbsDesk = emptyTbsDesk()
  store.knowledge = emptyKnowledge()
  store.recipes = emptyRecipes()
  store.arcane = emptyArcane()
  store.music = emptyMusic()
  store.openscad = emptyOpenScad()
  store.coding = emptyCoding()
  store.browser = { configured: false, instances: [] }
  store.browserLoaded = false
  store.browserLoadVersion += 1
  store.browserStatuses = {}
}

export const useIntegrationStore = defineStore('integrations', {
  state: () => ({
    webfetch: { providers: [] },
    search: emptySearch(),
    calendar: emptyCalendar(),
    mail: emptyMail(),
    tbsDesk: emptyTbsDesk(),
    knowledge: emptyKnowledge(),
    recipes: emptyRecipes(),
    arcane: emptyArcane(),
    music: emptyMusic(),
    openscad: emptyOpenScad(),
    coding: emptyCoding(),
    browser: { configured: false, instances: [] },
    browserLoaded: false,
    browserStatuses: {},
    browserLoadVersion: 0,
    scope: 'instance',
    catalog: [],
    catalogLoading: false,
    catalogLoaded: false,
    catalogLoadFailed: false,
    pendingLoads: 0,
    loading: false,
    loaded: false,
    loadFailed: false,
    scopeGeneration: 0,
    loadVersions: {},
    effectiveLoadVersion: 0
  }),
  getters: {
    configuredIntegrations: (state) => [
      ...state.webfetch.providers,
      ...state.search.providers, ...state.calendar.accounts, ...state.mail.accounts,
      ...state.knowledge.bases, ...state.recipes.accounts, ...state.arcane.instances,
      ...state.music.instances, ...state.openscad.instances, ...state.coding.instances,
      ...state.tbsDesk.accounts,
      ...state.browser.instances.map((item) => ({ ...item, integrationType: 'browser', providerType: 'browser' }))
    ],
    adapterAvailable: (state) => (adapterType, scope = state.scope) => {
      const adapter = state.catalog.find((item) => item.adapterType === adapterType)
      if (!adapter || state.catalogLoadFailed) return false
      if (scope === 'personal' && adapter.instanceOnly) return false
      if (scope === 'personal') return adapter.availability === 'full' || useAuthStore().isAdmin && adapter.availability === 'instance_only'
      return !adapter.personalOnly && adapter.executionLocation === 'backend'
        && ['full', 'instance_only'].includes(adapter.availability)
    }
  },
  actions: {
    async testIntegration(integration) {
      if (integration.integrationType === 'browser') {
        const result = await this.browserStatus(integration.id, true)
        return { type: result.state === 'ready' ? 'positive' : 'warning', message: browserStatusLabel(result) }
      }
      const method = { webfetch: 'testWebfetch', search: 'testSearch', calendar: 'testCalendar', mail: 'testMail', support: 'testTbsDesk', knowledge: 'testKnowledge', recipes: 'testRecipes', infrastructure: 'testArcane', music: 'testSonoBin', openscad: 'testOpenScad', coding: 'testOpenCode' }[integration.integrationType]
      const result = await this[method](integration.id)
      return integration.integrationType === 'openscad' && result?.headlessPreview === false
        ? { type: 'warning', message: 'OpenSCAD ist erreichbar, unterstützt aber keine Headless-Vorschau.' }
        : { type: 'positive', message: 'Verbindung erfolgreich' }
    },
    async fetchProfileAvailability(id, scope) {
      return (await api.get(`/integrations/profiles/${id}/availability`, { params: { scope } })).data
    },
    async saveProfileAvailability(id, scope, value) {
      return (await api.put(`/integrations/profiles/${id}/availability`, value, { params: { scope } })).data
    },
    async fetchPermissions(id, profileId = null) {
      return (await api.get(`/integrations/${id}/permissions`, { params: { scope: this.scope, profile_id: profileId } })).data
    },
    async savePermissions(id, value, profileId = null) {
      return (await api.put(`/integrations/${id}/permissions`, value, { params: { scope: this.scope, profile_id: profileId } })).data
    },
    async setIntegrationEnabled(id, enabled, scope) {
      const { data } = await api.patch(`/integrations/${id}/enabled`, { enabled }, scopeConfig(scope))
      return data
    },
    selectScope(scope) {
      const next = scope === 'personal' ? 'personal' : 'instance'
      if (next === this.scope) return
      this.scope = next
      this.scopeGeneration += 1
      this.loadVersions = {}
      this.pendingLoads = 0
      this.loading = false
      this.loaded = false
      this.loadFailed = false
      clearIntegrations(this)
    },
    async fetchCatalog() {
      this.catalogLoading = true
      this.catalogLoadFailed = false
      try {
        const { data } = await api.get('/integrations/catalog')
        this.catalog = data.adapters
        this.catalogLoaded = true
        return this.catalog
      } catch (error) {
        this.catalogLoadFailed = true
        throw error
      } finally {
        this.catalogLoading = false
      }
    },
    async setAdapterAvailability(adapterType, availability) {
      this.catalogLoading = true
      try {
        const { data } = await api.put(`/integrations/catalog/${adapterType}`, { availability })
        const index = this.catalog.findIndex((adapter) => adapter.adapterType === adapterType)
        if (index !== -1) this.catalog[index] = data
        return data
      } finally {
        this.catalogLoading = false
      }
    },
    async fetchAll() {
      const context = scopeContext(this)
      this.loadFailed = false
      try {
        await Promise.all([
          this.fetchSearch(),
          this.fetchCalendar(),
          this.fetchMail(),
          this.fetchKnowledge(),
          this.fetchRecipes(),
          this.fetchArcane(),
          this.fetchMusic(),
          this.fetchOpenScad(),
          ...(context.scope === 'instance' ? [this.fetchWebfetch()] : []),
          ...(context.scope === 'personal' ? [this.fetchCoding(), this.fetchBrowser(), this.fetchTbsDesk()] : [])
        ])
      } catch (error) {
        if (contextIsCurrent(this, context)) this.loadFailed = true
        throw error
      }
    },
    async fetchResource(field, path, config) {
      const context = scopeContext(this)
      const loadVersion = (this.loadVersions[field] || 0) + 1
      this.loadVersions[field] = loadVersion
      this.pendingLoads += 1
      this.loading = true
      this.loadFailed = false
      try {
        const data = (await api.get(path, scopeConfig(context.scope, config))).data
        if (!contextIsCurrent(this, context) || this.loadVersions[field] !== loadVersion) return
        this[field] = data
        this.loaded = true
        return data
      } catch (error) {
        if (contextIsCurrent(this, context) && this.loadVersions[field] === loadVersion) {
          this.loadFailed = true
        }
        throw error
      } finally {
        if (contextIsCurrent(this, context)) {
          this.pendingLoads = Math.max(0, this.pendingLoads - 1)
          this.loading = this.pendingLoads > 0
        }
      }
    },
    fetchSearch() {
      return this.fetchResource('search', '/integrations/search')
    },
    fetchWebfetch() {
      return this.fetchResource('webfetch', '/integrations/webfetch')
    },
    async createWebfetch(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/webfetch/providers', payload)
      if (contextIsCurrent(this, context)) await this.fetchWebfetch()
    },
    async updateWebfetch(id, payload) {
      const context = scopeContext(this)
      await api.put(`/integrations/webfetch/providers/${id}`, payload)
      if (contextIsCurrent(this, context)) await this.fetchWebfetch()
    },
    async removeWebfetch(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/webfetch/providers/${id}`)
      if (contextIsCurrent(this, context)) await this.fetchWebfetch()
    },
    async testWebfetch(id) {
      return (await api.post(`/integrations/webfetch/providers/${id}/test`)).data
    },
    async createSearch(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/search/providers', payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchSearch()
    },
    async updateSearch(id, payload) {
      const context = scopeContext(this)
      await api.put(`/integrations/search/providers/${id}`, payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchSearch()
    },
    async removeSearch(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/search/providers/${id}`, scopeConfig(context.scope))
      if (!contextIsCurrent(this, context)) return
      await this.fetchSearch()
    },
    async testSearch(id) {
      const scope = this.scope
      return (await api.post(`/integrations/search/providers/${id}/test`, null, scopeConfig(scope))).data
    },
    fetchCalendar() {
      return this.fetchResource('calendar', '/integrations/calendar')
    },
    async createCalendar(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/calendar/accounts', payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchCalendar()
    },
    async updateCalendar(id, payload) {
      const request = { ...payload }
      if (request.password === '') delete request.password
      const context = scopeContext(this)
      await api.put(`/integrations/calendar/accounts/${id}`, request, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchCalendar()
    },
    async removeCalendar(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/calendar/accounts/${id}`, scopeConfig(context.scope))
      if (!contextIsCurrent(this, context)) return
      await this.fetchCalendar()
    },
    async testCalendar(id) {
      const scope = this.scope
      return (await api.post(`/integrations/calendar/accounts/${id}/test`, null, scopeConfig(scope))).data
    },
    fetchMail() {
      return this.fetchResource('mail', '/integrations/mail')
    },
    async createMail(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/mail/accounts', payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchMail()
    },
    async updateMail(id, payload) {
      const request = { ...payload }
      if (request.password === '') delete request.password
      const context = scopeContext(this)
      await api.put(`/integrations/mail/accounts/${id}`, request, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchMail()
    },
    async removeMail(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/mail/accounts/${id}`, scopeConfig(context.scope))
      if (!contextIsCurrent(this, context)) return
      await this.fetchMail()
    },
    async testMail(id) {
      const scope = this.scope
      return (await api.post(`/integrations/mail/accounts/${id}/test`, null, scopeConfig(scope))).data
    },
    async fetchTbsDesk() {
      const context = scopeContext(this)
      const loadVersion = (this.loadVersions.tbsDesk || 0) + 1
      this.loadVersions.tbsDesk = loadVersion
      this.pendingLoads += 1
      this.loading = true
      this.loadFailed = false
      try {
        const { data } = await api.get('/integrations/support/tbs-desk')
        if (!contextIsCurrent(this, context) || this.loadVersions.tbsDesk !== loadVersion) return
        this.tbsDesk = data
        this.loaded = true
        return data
      } catch (error) {
        if (contextIsCurrent(this, context) && this.loadVersions.tbsDesk === loadVersion) this.loadFailed = true
        throw error
      } finally {
        if (contextIsCurrent(this, context)) {
          this.pendingLoads = Math.max(0, this.pendingLoads - 1)
          this.loading = this.pendingLoads > 0
        }
      }
    },
    async createTbsDesk(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/support/tbs-desk', payload)
      if (contextIsCurrent(this, context)) await this.fetchTbsDesk()
    },
    async updateTbsDesk(id, payload) {
      const request = { ...payload }
      if (request.password === '') delete request.password
      const context = scopeContext(this)
      await api.put(`/integrations/support/tbs-desk/${id}`, request)
      if (contextIsCurrent(this, context)) await this.fetchTbsDesk()
    },
    async removeTbsDesk(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/support/tbs-desk/${id}`)
      if (contextIsCurrent(this, context)) await this.fetchTbsDesk()
    },
    async testTbsDesk(id) {
      return (await api.post(`/integrations/support/tbs-desk/${id}/test`)).data
    },
    fetchKnowledge() {
      return this.fetchResource('knowledge', '/integrations/knowledge')
    },
    async createKnowledge(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/knowledge/bases', payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchKnowledge()
    },
    async updateKnowledge(id, payload) {
      const request = { ...payload }
      if (!request.token_id || !request.token_secret) {
        delete request.token_id
        delete request.token_secret
      }
      const context = scopeContext(this)
      await api.put(`/integrations/knowledge/bases/${id}`, request, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchKnowledge()
    },
    async removeKnowledge(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/knowledge/bases/${id}`, scopeConfig(context.scope))
      if (!contextIsCurrent(this, context)) return
      await this.fetchKnowledge()
    },
    async testKnowledge(id) {
      const scope = this.scope
      return (await api.post(`/integrations/knowledge/bases/${id}/test`, null, scopeConfig(scope))).data
    },
    fetchRecipes() {
      return this.fetchResource('recipes', '/integrations/recipes')
    },
    async createRecipes(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/recipes/accounts', payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchRecipes()
    },
    async updateRecipes(id, payload) {
      const request = { ...payload }
      if (typeof request.token === 'string' && request.token.trim() === '') delete request.token
      const context = scopeContext(this)
      await api.put(`/integrations/recipes/accounts/${id}`, request, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchRecipes()
    },
    async removeRecipes(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/recipes/accounts/${id}`, scopeConfig(context.scope))
      if (!contextIsCurrent(this, context)) return
      await this.fetchRecipes()
    },
    async testRecipes(id) {
      const scope = this.scope
      return (await api.post(`/integrations/recipes/accounts/${id}/test`, null, scopeConfig(scope))).data
    },
    fetchArcane() {
      return this.fetchResource('arcane', '/integrations/infrastructure/arcane')
    },
    async createArcane(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/infrastructure/arcane', payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchArcane()
    },
    async updateArcane(id, payload) {
      const context = scopeContext(this)
      const request = { ...payload }
      if (!request.api_key) delete request.api_key
      await api.put(`/integrations/infrastructure/arcane/${id}`, request, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchArcane()
    },
    async removeArcane(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/infrastructure/arcane/${id}`, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchArcane()
    },
    async testArcane(id) {
      return (await api.post(`/integrations/infrastructure/arcane/${id}/test`, null, scopeConfig(this.scope))).data
    },
    fetchMusic() {
      return this.fetchResource('music', '/integrations/music/sonobin')
    },
    async createSonoBin(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/music/sonobin', payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchMusic()
    },
    async updateSonoBin(id, payload) {
      const request = { ...payload }
      if (!request.bearerToken) delete request.bearerToken
      const context = scopeContext(this)
      await api.put(`/integrations/music/sonobin/${id}`, request, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchMusic()
    },
    async removeSonoBin(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/music/sonobin/${id}`, scopeConfig(context.scope))
      if (!contextIsCurrent(this, context)) return
      await this.fetchMusic()
    },
    async testSonoBin(id) {
      const scope = this.scope
      return (await api.post(`/integrations/music/sonobin/${id}/test`, null, scopeConfig(scope))).data
    },
    fetchOpenScad() {
      return this.fetchResource('openscad', '/integrations/openscad')
    },
    async createOpenScad(payload) {
      const context = scopeContext(this)
      await api.post('/integrations/openscad', payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchOpenScad()
    },
    async updateOpenScad(id, payload) {
      const context = scopeContext(this)
      await api.put(`/integrations/openscad/${id}`, payload, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchOpenScad()
    },
    async removeOpenScad(id) {
      const context = scopeContext(this)
      await api.delete(`/integrations/openscad/${id}`, scopeConfig(context.scope))
      if (contextIsCurrent(this, context)) await this.fetchOpenScad()
    },
    async discoverOpenScad(executionAgentId) {
      const scope = this.scope
      return (await api.post(
        '/integrations/openscad/discover',
        { executionAgentId },
        scopeConfig(scope)
      )).data
    },
    async testOpenScad(id) {
      const scope = this.scope
      return (await api.post(`/integrations/openscad/${id}/test`, null, scopeConfig(scope))).data
    },
    async fetchCoding({ personal = false } = {}) {
      if (personal && this.scope !== 'personal') {
        try {
          return (await api.get('/integrations/coding', scopeConfig('personal', {
            suppressErrorToast: true
          }))).data
        } catch (error) {
          if (!unavailableCodingCodes.has(error?.response?.data?.error?.code)) throw error
          return emptyCoding()
        }
      }
      const context = scopeContext(this)
      if (context.scope !== 'personal') return emptyCoding()
      if (this.catalogLoaded && !this.adapterAvailable('opencode', 'personal')) {
        this.coding = emptyCoding()
        return this.coding
      }
      try {
        return await this.fetchResource(
          'coding',
          '/integrations/coding',
          { suppressErrorToast: true }
        )
      } catch (error) {
        if (!unavailableCodingCodes.has(error?.response?.data?.error?.code)) throw error
        if (contextIsCurrent(this, context)) {
          this.coding = emptyCoding()
          this.loadFailed = false
          this.loaded = true
        }
        return this.coding
      }
    },
    async createOpenCode(payload) {
      const context = scopeContext(this)
      this.loadVersions.coding = (this.loadVersions.coding || 0) + 1
      const request = { ...payload }
      if (!request.password) delete request.password
      const { data } = await api.post('/integrations/coding/opencode', request)
      if (contextIsCurrent(this, context)) this.coding = data
      return data
    },
    async updateOpenCode(id, payload) {
      const context = scopeContext(this)
      this.loadVersions.coding = (this.loadVersions.coding || 0) + 1
      const request = { ...payload }
      if (!request.password) delete request.password
      const { data } = await api.put(`/integrations/coding/opencode/${id}`, request)
      if (contextIsCurrent(this, context)) this.coding = data
      return data
    },
    async removeOpenCode(id) {
      const context = scopeContext(this)
      this.loadVersions.coding = (this.loadVersions.coding || 0) + 1
      await api.delete(`/integrations/coding/opencode/${id}`)
      if (contextIsCurrent(this, context)) await this.fetchCoding()
    },
    async testOpenCode(id) {
      return (await api.post(`/integrations/coding/opencode/${id}/test`)).data
    },
    async fetchBrowser() {
      const version = ++this.browserLoadVersion
      let data = { configured: false, instances: [] }
      if (!this.catalogLoaded || this.catalogLoadFailed || this.adapterAvailable('browser', 'personal')) {
        try {
          data = (await api.get('/integrations/browser', scopeConfig('personal', { suppressErrorToast: true }))).data
        } catch (error) {
          if (!['integration.adapterDisabled', 'integration.personalAdapterDisabled'].includes(error?.response?.data?.error?.code)) {
            notifyError(error)
            throw error
          }
        }
      }
      if (version === this.browserLoadVersion) {
        this.browser = data
        this.browserLoaded = true
      }
      return data
    },
    async saveBrowser(id, payload) {
      const version = ++this.browserLoadVersion
      const { data } = id
        ? await api.patch(`/integrations/browser/${id}`, payload, scopeConfig('personal'))
        : await api.post('/integrations/browser', payload, scopeConfig('personal'))
      if (version === this.browserLoadVersion) {
        this.browser = data
        this.browserLoaded = true
        if (id) delete this.browserStatuses[id]
      }
      return data
    },
    async removeBrowser(id) {
      const version = ++this.browserLoadVersion
      const { data } = await api.delete(`/integrations/browser/${id}`, scopeConfig('personal'))
      if (version === this.browserLoadVersion) {
        this.browser = data
        this.browserLoaded = true
        delete this.browserStatuses[id]
      }
    },
    async browserStatus(id, test = false) {
      const version = this.browserLoadVersion
      const { data } = test
        ? await api.post(`/integrations/browser/${id}/test`, null, scopeConfig('personal'))
        : await api.get(`/integrations/browser/${id}/status`, scopeConfig('personal'))
      if (version === this.browserLoadVersion) this.browserStatuses[id] = data
      return data
    },
    async fetchAccess(id) {
      const context = scopeContext(this)
      const { data } = await api.get(`/integrations/${id}/access`, scopeConfig(context.scope))
      return contextIsCurrent(this, context) ? data : null
    },
    async saveAccess(id, payload) {
      const context = scopeContext(this)
      const { data } = await api.put(`/integrations/${id}/access`, payload, scopeConfig(context.scope))
      return contextIsCurrent(this, context) ? data : null
    },
    async fetchEffectiveSearch() {
      const loadVersion = ++this.effectiveLoadVersion
      this.loadFailed = false
      try {
        const personal = await api.get('/integrations/search', scopeConfig('personal'))
        if (this.effectiveLoadVersion !== loadVersion) return
        this.search = personal.data
        this.loaded = true
      } catch (error) {
        if (this.effectiveLoadVersion === loadVersion) this.loadFailed = true
        throw error
      }
    }
  }
})
