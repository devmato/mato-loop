import { defineStore } from 'pinia'
import { api } from 'src/boot/axios'

const defaultMediaSettings = () => ({ timeoutSeconds: 300 })
const defaultAnalysisSettings = () => ({ timeoutSeconds: 1800 })
const defaultConfigurationWebAccess = () => ({
  userAgent: '',
  maxResponseMiB: 64,
  publicAddressesOnly: true
})

export const useToolSettingsStore = defineStore('tool-settings', {
  state: () => ({
    groups: [],
    generateImage: defaultMediaSettings(),
    editImage: defaultMediaSettings(),
    generateSpeech: defaultMediaSettings(),
    generateAudio: defaultMediaSettings(),
    transcribeAudio: defaultMediaSettings(),
    analyzeVideo: defaultAnalysisSettings(),
    analyzeAudio: defaultAnalysisSettings(),
    transcribeAudioAvailable: true,
    toolAvailability: {},
    loaded: false,
    loadFailed: false,
    loading: false,
    saving: false,
    loadVersion: 0,
    configurationGroups: [],
    presetOptions: [],
    configurationWebAccess: defaultConfigurationWebAccess(),
    configurationLoaded: false,
    configurationLoadFailed: false,
    configurationLoading: false,
    configurationSaving: false,
    configurationLoadVersion: 0,
    configurationSaveVersion: 0
  }),
  actions: {
    async fetchProfilePermissions(id, scope) {
      return (await api.get(`/tools/profiles/${id}/permissions`, { params: { scope } })).data
    },
    async saveProfilePermissions(id, scope, value) {
      return (await api.put(`/tools/profiles/${id}/permissions`, value, { params: { scope } })).data
    },
    async fetchAvailability() {
      const loadVersion = ++this.loadVersion
      this.loading = true
      this.loadFailed = false
      try {
        const { data } = await api.get('/tools/availability')
        if (this.loadVersion !== loadVersion) return
        this.assignAvailability(data)
        this.loaded = true
      } catch (error) {
        if (this.loadVersion === loadVersion) this.loadFailed = true
        throw error
      } finally {
        if (this.loadVersion === loadVersion) this.loading = false
      }
    },
    async fetchConfiguration() {
      const loadVersion = ++this.configurationLoadVersion
      this.configurationLoading = true
      this.configurationLoadFailed = false
      try {
        const { data } = await api.get('/tools/configuration')
        if (this.configurationLoadVersion !== loadVersion) return
        this.assignConfiguration(data)
        this.configurationLoaded = true
        return data
      } catch (error) {
        if (this.configurationLoadVersion === loadVersion) this.configurationLoadFailed = true
        throw error
      } finally {
        if (this.configurationLoadVersion === loadVersion) this.configurationLoading = false
      }
    },
    async saveConfiguration(configuration) {
      const saveVersion = ++this.configurationSaveVersion
      this.configurationLoadVersion += 1
      this.configurationSaving = true
      try {
        const { data } = await api.put('/tools/configuration', configuration)
        if (this.configurationSaveVersion !== saveVersion) return
        this.assignConfiguration(data)
        this.configurationLoaded = true
        return data
      } finally {
        if (this.configurationSaveVersion === saveVersion) this.configurationSaving = false
      }
    },
    assignConfiguration(data) {
      this.presetOptions = data?.presetOptions || []
      this.configurationGroups = Array.isArray(data?.groups)
        ? data.groups.map((group) => ({
            ...group,
            tools: (group.tools || []).map((tool) => ({ ...tool }))
          }))
        : []
      this.configurationWebAccess = {
        ...defaultConfigurationWebAccess(),
        ...data?.webAccess
      }
      this.assignAvailability({
        tools: data?.toolAvailability
      })
      this.assignSettings(data)
    },
    assignAvailability(data) {
      this.toolAvailability = { ...data?.tools }
      this.groups = this.groups.map((group) => ({
        ...group,
        tools: group.tools.map((tool) => ({
          ...tool,
          available: this.toolAvailability[tool.name] !== false,
          actions: (tool.actions || []).map((action) => ({ ...action }))
        }))
      }))
      this.transcribeAudioAvailable = this.toolAvailability.transcribe_audio !== false
    },
    assignSettings(data) {
      this.groups = (Array.isArray(data?.groups) ? data.groups : [])
        .map((group) => ({
          ...group,
          tools: (group.tools || [])
            .filter((tool) => tool.source?.type !== 'integration')
            .map((tool) => ({
              ...tool,
              available: tool.available !== false && this.toolAvailability[tool.name] !== false,
              actions: (tool.actions || []).map((action) => ({ ...action }))
            }))
        }))
        .filter((group) => group.tools.length)
      for (const field of ['generateImage', 'generateSpeech', 'generateAudio', 'transcribeAudio']) {
        this[field] = { ...defaultMediaSettings(), ...data?.[field] }
      }
      this.editImage = { ...defaultMediaSettings(), ...data?.editImage }
      this.analyzeVideo = { ...defaultAnalysisSettings(), ...data?.analyzeVideo }
      this.analyzeAudio = { ...defaultAnalysisSettings(), ...data?.analyzeAudio }
    }
  }
})
