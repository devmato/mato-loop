<template>
  <section>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Provider</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">
          {{ admin ? 'Globale KI-Verbindungen und ihre Chat- und Medienmodelle verwalten.' : 'Persönliche KI-Verbindungen und ihre Chat- und Medienmodelle verwalten.' }}
        </p>
      </div>
      <div v-if="showManagement" class="settings-scope-actions row items-center col-auto">
        <q-btn
          class="col-auto"
          color="primary"
          unelevated
          no-caps
          no-wrap
          icon="add"
          label="Provider hinzufügen"
          :disable="pageLoading || (!admin && personalBlocked)"
          @click="openCreateDialog"
        />
      </div>
    </header>

    <q-tabs
      v-if="admin"
      v-model="section"
      dense
      no-caps
      align="left"
      active-color="primary"
      indicator-color="primary"
      class="q-mb-lg"
    >
      <q-tab name="general" icon="settings" label="Einstellungen" />
      <q-tab name="management" icon="dns" label="Verwaltung" />
    </q-tabs>

    <PersonalSettingsGate v-if="admin && section === 'general'" :model-value="providers.personalProvidersEnabled" title="Persönliche Provider erlauben" description="Benutzer können eigene Provider und Modelle verwalten." :disabled="gateLoading || pageLoading || loadFailed" @update:model-value="togglePersonalProviders" />

    <q-card v-if="admin && section === 'general'" flat class="app-surface q-mt-md">
      <q-card-section class="row items-center q-col-gutter-md">
        <div class="col-12 col-sm row items-center no-wrap q-gutter-md">
          <q-avatar color="primary" text-color="white" icon="security" />
          <div class="col">
            <div class="text-subtitle1 text-weight-bold">Modelltrennung</div>
            <div class="text-caption app-muted">Lege für jeden Provider fest, ob seine Modelle als privat oder öffentlich gelten. Bei aktiver Modelltrennung gelten dafür getrennte Tool-Rechte, Erinnerungen und persönliche Profilangaben.</div>
          </div>
        </div>
        <q-toggle dense :model-value="generalSettings.modelTrustEnabled" class="col-12 col-sm-auto" color="primary" aria-label="Modelltrennung" :disable="pageLoading || generalSettings.saving || loadFailed" @update:model-value="toggleModelTrust" />
      </q-card-section>
    </q-card>

    <q-banner v-if="loadFailed" rounded class="app-surface-tonal q-mb-md">
      Die Provider konnten nicht geladen werden.
      <template #action><q-btn flat no-caps color="primary" label="Erneut laden" @click="loadPage" /></template>
    </q-banner>

    <q-card v-if="showManagement && !llmProviders.length && !pageLoading && !loadFailed" flat class="app-surface q-pa-lg">
      <div class="row items-center no-wrap q-gutter-md">
        <q-avatar color="grey-7" text-color="white" icon="hub" />
        <div>
          <div class="text-weight-medium">Noch kein Provider konfiguriert</div>
          <div class="text-body2 app-muted">Füge ChatGPT Codex, Lemonade, Azure Speech oder einen OpenAI-kompatiblen Endpunkt hinzu.</div>
        </div>
      </div>
    </q-card>

    <div v-if="showManagement" class="provider-card-list">
      <q-card
        v-for="provider in llmProviders"
        :key="provider.id"
        flat
        class="app-surface provider-card"
      >
        <q-expansion-item v-model="expandedProviders[provider.id]" :duration="0" expand-separator switch-toggle-side header-class="settings-list-item items-start" :toggle-aria-label="`Modelle für ${provider.name} anzeigen`">
          <template #header>
            <q-item-section avatar>
              <q-avatar color="primary" text-color="white" :icon="providerIcon(provider)" />
            </q-item-section>
            <q-item-section class="provider-card__identity">
              <div class="row items-center q-gutter-sm">
                <q-item-label class="settings-list-name text-weight-bold">{{ provider.name }}</q-item-label>
                <q-chip dense :color="providerStatus(provider).color" text-color="white">
                  {{ providerStatus(provider).label }}
                </q-chip>
                <q-badge outline color="grey-7" :label="provider.scope === 'instance' ? 'Global' : 'Persönlich'" />
              </div>
              <q-item-label caption class="ellipsis">{{ providerDescription(provider) }}</q-item-label>
              <q-item-label caption>{{ activeModelCount(provider) }} aktiv · {{ providerModels(provider).length }} Modelle</q-item-label>
            </q-item-section>
            <q-item-section side top class="settings-list-actions">
              <div class="row items-center no-wrap q-gutter-sm" @click.stop>
                <q-toggle dense
                  :model-value="provider.enabled"
                  color="primary"
                  :aria-label="`${provider.name}: Provider aktiv`"
                  :disable="!canEdit(provider) || providerToggleLoading[provider.id]"
                  @update:model-value="toggleProvider(provider, $event)"
                />
                <div class="row items-center no-wrap q-gutter-xs">
                  <q-btn flat round dense icon="edit" :aria-label="`${provider.name} bearbeiten`" :disable="!canEdit(provider)" @click="openEditDialog(provider)" />
                  <q-btn flat round dense icon="more_vert" :aria-label="`Aktionen für ${provider.name}`">
                    <AppMenu>
                      <q-list dense style="min-width: 190px">
                        <q-item v-close-popup clickable :disable="!canEdit(provider) || !canTest(provider)" @click="testProvider(provider.id)">
                          <q-item-section avatar><q-icon name="network_check" /></q-item-section>
                          <q-item-section>Verbindung testen</q-item-section>
                          <q-inner-loading :showing="testingProviderId === provider.id" />
                        </q-item>
                        <q-separator />
                        <q-item v-close-popup clickable :disable="!canEdit(provider)" class="text-negative" @click="removeProvider(provider)">
                          <q-item-section avatar><q-icon name="delete" /></q-item-section>
                          <q-item-section>Löschen</q-item-section>
                        </q-item>
                      </q-list>
                    </AppMenu>
                  </q-btn>
                </div>
              </div>
            </q-item-section>
          </template>

          <q-linear-progress v-if="syncing[provider.id]" indeterminate color="primary" />
          <q-card-section class="provider-models-section">
            <div class="provider-models-toolbar">
              <div>
                <div class="text-subtitle1 text-weight-bold">Modelle</div>
                <div v-if="provider.modelsLastSyncedAt" class="text-caption app-muted">
                  Zuletzt aktualisiert: {{ formatDate(provider.modelsLastSyncedAt) }}
                </div>
              </div>
              <q-space />
              <q-btn
                flat
                round
                dense
                icon="refresh"
                aria-label="Modelle aktualisieren"
                :loading="syncing[provider.id]"
                :disable="!canEdit(provider) || !supportsDiscovery(provider)"
                @click="syncProviderModels(provider)"
              >
                <q-tooltip>{{ supportsDiscovery(provider) ? 'Modelle vom Provider aktualisieren' : 'Automatische Erkennung wird nicht unterstützt' }}</q-tooltip>
              </q-btn>
              <q-btn
                v-if="supportsManualModels(provider) && showProminentManualAction(provider) && providerModels(provider).length"
                outline
                no-caps
                color="primary"
                icon="add"
                label="Modell manuell hinzufügen"
                :disable="!canEdit(provider)"
                @click="openModelDialog(provider)"
              />
              <q-btn v-else-if="supportsManualModels(provider)" flat round dense icon="more_vert" aria-label="Weitere Modellaktionen">
                <AppMenu>
                  <q-list dense style="min-width: 230px">
                    <q-item v-close-popup clickable :disable="!canEdit(provider)" @click="openModelDialog(provider)">
                      <q-item-section avatar><q-icon name="add" /></q-item-section>
                      <q-item-section>Modell manuell hinzufügen</q-item-section>
                    </q-item>
                  </q-list>
                </AppMenu>
              </q-btn>
            </div>

            <q-banner v-if="syncFailed[provider.id]" rounded class="app-surface-tonal q-mt-md">
              <template #avatar><q-icon name="sync_problem" color="warning" /></template>
              Die Modelle konnten nicht automatisch ermittelt werden. Bestehende Einträge bleiben erhalten.
            </q-banner>

            <q-input
              v-if="providerModels(provider).length > 6"
              v-model="modelSearch[provider.id]"
              outlined
              dense
              clearable
              class="q-mt-md"
              label="Modelle durchsuchen"
            >
              <template #prepend><q-icon name="search" /></template>
            </q-input>

            <AppStateMessage
              v-if="!providerModels(provider).length"
              class="provider-models-empty app-surface-tonal q-mt-md"
              icon="view_in_ar"
              title="Keine Modelle verfügbar"
              :description="supportsManualModels(provider) ? 'Starte die Erkennung erneut oder hinterlege ein Modell manuell.' : 'Aktualisiere die Modelle, um Azure Speech Fast Transcription zu erkennen.'"
            >
              <template #actions>
                <q-btn
                  v-if="supportsManualModels(provider)"
                  unelevated
                  no-caps
                  color="primary"
                  icon="add"
                  label="Modell manuell hinzufügen"
                  :disable="!canEdit(provider)"
                  @click="openModelDialog(provider)"
                />
              </template>
            </AppStateMessage>

            <q-list v-else separator class="provider-model-list q-mt-md">
              <q-item v-for="model in filteredModels(provider)" :key="model.id" class="provider-model-item settings-list-item">
                <q-item-section avatar>
                  <q-avatar rounded color="primary" text-color="white" :icon="modelIcon(model)" size="38px" />
                </q-item-section>
                <q-item-section>
                  <q-item-label class="settings-list-name text-weight-medium">{{ model.name || model.modelId }}</q-item-label>
                  <q-item-label caption class="model-technical-id">{{ model.modelId }}</q-item-label>
                  <div class="row items-center q-gutter-xs q-mt-xs">
                    <q-chip dense outline>{{ model.origin === 'manual' ? 'Manuell' : 'Entdeckt' }}</q-chip>
                    <q-chip
                      v-for="capability in normalizeModelCapabilities(model.capabilities)"
                      :key="capability"
                      dense
                      outline
                      :icon="capabilityIcon(capability)"
                    >
                      {{ modelCapabilityLabel(capability) }}
                    </q-chip>
                    <q-chip v-if="!isModelPresent(model)" dense color="warning" text-color="dark">Nicht verfügbar</q-chip>
                  </div>
                </q-item-section>
                <q-item-section side top class="settings-list-actions">
                  <div class="row items-center no-wrap q-gutter-sm">
                    <q-toggle dense
                      :model-value="model.enabled"
                      color="primary"
                      :aria-label="`${model.name || model.modelId}: Modell aktiv`"
                      :disable="!canEdit(provider) || !provider.enabled || !isModelPresent(model) || modelToggleLoading[model.id]"
                      @update:model-value="toggleModel(model, provider, $event)"
                    />
                    <div class="row items-center no-wrap q-gutter-xs">
                      <q-btn flat round dense icon="tune" :aria-label="`${model.name || model.modelId}: ${modelSupportsCapability(model, 'chat') ? 'Laufzeit bearbeiten' : 'Modell bearbeiten'}`" :disable="!canEdit(provider)" @click="openModelDialog(provider, model)" />
                      <q-btn flat round dense icon="more_vert" :aria-label="`Aktionen für ${model.name || model.modelId}`">
                        <AppMenu>
                          <q-list dense style="min-width: 210px">
                            <q-item
                              v-if="modelSupportsCapability(model, 'chat')"
                              v-close-popup
                              clickable
                              :disable="!canEdit(provider) || !canTestModel(model, provider)"
                              @click="testModel(model, provider)"
                            >
                              <q-item-section avatar><q-icon name="network_check" /></q-item-section>
                              <q-item-section>Testen</q-item-section>
                            </q-item>
                            <q-separator v-if="modelSupportsCapability(model, 'chat')" />
                            <q-item v-close-popup clickable :disable="!canEdit(provider)" class="text-negative" @click="removeModel(model, provider)">
                              <q-item-section avatar><q-icon name="delete" /></q-item-section>
                              <q-item-section>Löschen</q-item-section>
                            </q-item>
                          </q-list>
                        </AppMenu>
                      </q-btn>
                    </div>
                  </div>
                </q-item-section>
              </q-item>
              <q-item v-if="!filteredModels(provider).length">
                <q-item-section class="text-center app-muted q-py-lg">Keine passenden Modelle gefunden.</q-item-section>
              </q-item>
            </q-list>
          </q-card-section>
        </q-expansion-item>
      </q-card>
    </div>

    <q-inner-loading :showing="pageLoading && !llmProviders.length" />
    <ProviderConfigDialog
      v-model="dialogOpen"
      :provider="selectedProvider"
      @provider-created="handleProviderCreated"
      @provider-updated="handleProviderUpdated"
      @update:model-value="onDialogChange"
    />
    <ModelConfigDialog
      v-model="modelDialogOpen"
      :provider="selectedModelProvider"
      :model="selectedModel"
      @update:model-value="onDialogChange"
    />
  </section>
</template>

<script setup>
import AppMenu from 'src/shared/ui/AppMenu.vue'
import { computed, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useRoute, useRouter } from 'vue-router'
import { api } from 'src/boot/axios'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import ModelConfigDialog from 'src/features/providers/ModelConfigDialog.vue'
import ProviderConfigDialog from 'src/features/providers/ProviderConfigDialog.vue'
import { useConfirm } from 'src/composables/confirm'
import {
  discoveryState,
  isModelEffective,
  isModelPresent,
  isProviderEffective,
  MODEL_CAPABILITY_OPTIONS,
  modelCapabilityLabel,
  modelSupportsCapability,
  normalizeModelCapabilities
} from 'src/utils/provider-models'
import { useProviderStore } from 'stores/providers'
import { useAuthStore } from 'stores/auth'
import PersonalSettingsGate from 'src/features/settings/PersonalSettingsGate.vue'
import { useGeneralSettingsStore } from 'src/features/settings/general-settings-store'

defineOptions({ name: 'ProviderSettingsPage' })

const { admin } = defineProps({ admin: { type: Boolean, default: false } })
const $q = useQuasar()
const router = useRouter()
const route = useRoute()
const { confirm } = useConfirm()
const providers = useProviderStore()
const auth = useAuthStore()
const generalSettings = useGeneralSettingsStore()
const dialogOpen = ref(false)
const modelDialogOpen = ref(false)
const selectedProvider = ref(null)
const selectedModelProvider = ref(null)
const selectedModel = ref(null)
const testingProviderId = ref(null)
const gateLoading = ref(false)
const section = ref('general')
const expandedProviders = reactive({})
const syncing = reactive({})
const syncFailed = reactive({})
const providerToggleLoading = reactive({})
const modelToggleLoading = reactive({})
const modelSearch = reactive({})
const pageLoading = ref(true)
const loadFailed = ref(false)
const listedProviders = ref([])
const listedModels = ref([])
const scope = computed(() => admin ? 'instance' : 'personal')
const showManagement = computed(() => !admin || section.value === 'management')
const llmProviders = computed(() => listedProviders.value.filter((item) => (
  ['openai-codex', 'openai-compatible', 'lemonade', 'azure-speech'].includes(item.adapterType)
)))
const personalBlocked = computed(() => !providers.personalProvidersEnabled && !auth.isAdmin)
const canEdit = (provider) => provider.scope === scope.value && (admin ? auth.isAdmin : !personalBlocked.value)
const codexFor = (provider) => ({ authenticated: provider.hasCredential })

async function prepareScope(scope) {
  providers.selectScope(scope)
  await providers.fetchAll()
}

async function loadPage() {
  const requestedScope = scope.value
  pageLoading.value = true
  loadFailed.value = false
  try {
    if (admin) await generalSettings.fetchSettings()
    if (scope.value !== requestedScope) return
    await providers.fetchPersonalPolicy()
    if (scope.value !== requestedScope) return
    if (!admin && personalBlocked.value) {
      await router.replace({ name: 'settings-personalization' })
      return
    }
    const data = (await api.get('/providers', { params: { scope: requestedScope } })).data
    if (scope.value !== requestedScope) return
    listedProviders.value = data.providers.map((provider) => ({ ...provider, scope: requestedScope }))
    listedModels.value = data.models
  } catch {
    if (scope.value === requestedScope) loadFailed.value = true
    // The central API interceptor displays the error.
  } finally {
    if (scope.value === requestedScope) pageLoading.value = false
  }
}

function onDialogChange(open) { if (!open) void loadPage() }

async function togglePersonalProviders(enabled) {
  gateLoading.value = true
  try {
    await providers.setPersonalProvidersEnabled(enabled)
  } finally {
    gateLoading.value = false
  }
}

async function toggleModelTrust(enabled) {
  try {
    await generalSettings.saveSettings({ modelTrustEnabled: enabled })
    $q.notify({ type: 'positive', message: 'Einstellung gespeichert.' })
  } catch { /* Central API toast. */ }
}

function openCreateDialog() {
  selectedProvider.value = null
  void prepareScope(scope.value).then(() => { dialogOpen.value = true }).catch(() => {})
}

function openEditDialog(provider) {
  selectedProvider.value = provider
  void prepareScope(provider.scope).then(() => { dialogOpen.value = true }).catch(() => {})
}

function openModelDialog(provider, model = null) {
  selectedModelProvider.value = provider
  selectedModel.value = model
  void prepareScope(provider.scope).then(() => { modelDialogOpen.value = true }).catch(() => {})
}

function providerModels(provider) {
  return listedModels.value.filter((model) => model.providerId === provider.id)
}

function filteredModels(provider) {
  const query = (modelSearch[provider.id] || '').trim().toLocaleLowerCase('de')
  if (!query) return providerModels(provider)
  return providerModels(provider).filter((model) => (
    `${model.name || ''} ${model.modelId}`.toLocaleLowerCase('de').includes(query)
  ))
}

function activeModelCount(provider) {
  return providerModels(provider).filter((model) => (
    isModelEffective(model, provider, codexFor(provider))
  )).length
}

function providerIcon(provider) {
  if (provider.adapterType === 'openai-codex') return 'key'
  if (provider.adapterType === 'lemonade') return 'developer_board'
  if (provider.adapterType === 'azure-speech') return 'graphic_eq'
  return 'hub'
}

function providerStatus(provider) {
  if (!provider.enabled) return { label: 'Deaktiviert', color: 'grey-7' }
  if (provider.adapterType === 'azure-speech' && !provider.hasCredential) {
    return { label: 'API-Key fehlt', color: 'warning' }
  }
  if (provider.adapterType === 'openai-codex' && !provider.hasCredential) {
    return { label: 'Nicht verbunden', color: 'warning' }
  }
  return { label: 'Aktiv', color: 'positive' }
}

function providerDescription(provider) {
  if (provider.adapterType === 'openai-codex') return 'ChatGPT-Abonnement · Codex OAuth'
  if (provider.scope === 'instance' && !Object.keys(provider.config || {}).length) return 'Globale Verbindung'
  if (provider.adapterType === 'lemonade') {
    return `${provider.config?.baseUrl || provider.config?.base_url || 'Keine URL'} · Lemonade`
  }
  if (provider.adapterType === 'azure-speech') {
    return `${provider.config?.baseUrl || provider.config?.base_url || 'Keine URL'} · Azure Speech Fast Transcription`
  }
  const apiType = provider.config?.apiType || provider.config?.api_type
  const protocol = apiType === 'responses' ? 'Responses API' : 'Chat Completions'
  return `${provider.config?.baseUrl || provider.config?.base_url || 'Keine URL'} · ${protocol}`
}

function canTest(provider) {
  return providerModels(provider).some((model) => (
    modelSupportsCapability(model, 'chat')
    && isModelEffective(model, provider, codexFor(provider))
  ))
}

function canTestModel(model, provider) {
  return isModelPresent(model)
    && isProviderEffective(provider, codexFor(provider))
    && modelSupportsCapability(model, 'chat')
}

function supportsDiscovery(provider) {
  if (provider.adapterType === 'azure-speech') return provider.hasCredential === true
  return ['openai-compatible', 'lemonade'].includes(provider.adapterType)
    || (provider.adapterType === 'openai-codex' && providers.codex.authenticated === true)
}

function supportsManualModels(provider) {
  return provider.adapterType !== 'azure-speech'
}

function capabilityIcon(capability) {
  return MODEL_CAPABILITY_OPTIONS.find((option) => option.value === capability)?.icon || 'extension'
}

function modelIcon(model) {
  const capability = normalizeModelCapabilities(model.capabilities).find((item) => item !== 'chat') || 'chat'
  return capabilityIcon(capability)
}

function showProminentManualAction(provider) {
  return discoveryState(provider, syncing[provider.id], syncFailed[provider.id]) !== 'synced'
    || providerModels(provider).length === 0
}

function formatDate(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('de', {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(date)
}

async function handleProviderCreated(id) {
  expandedProviders[id] = true
  const found = providers.providers.find((item) => item.id === id)
  const provider = found && { ...found, scope: providers.scope }
  if (provider) await syncProviderModels(provider)
  await loadPage()
}

async function handleProviderUpdated(id) {
  expandedProviders[id] = true
  const found = providers.providers.find((item) => item.id === id)
  const provider = found && { ...found, scope: providers.scope }
  if (provider && supportsDiscovery(provider)) await syncProviderModels(provider)
  await loadPage()
}

async function toggleProvider(provider, enabled) {
  providerToggleLoading[provider.id] = true
  try {
    await prepareScope(provider.scope)
    await providers.setProviderEnabled(provider.id, enabled)
  } catch {
    await providers.fetchAll().catch(() => {})
  } finally {
    providerToggleLoading[provider.id] = false
    await loadPage()
  }
}

async function toggleModel(model, provider, enabled) {
  modelToggleLoading[model.id] = true
  try {
    await prepareScope(provider.scope)
    await providers.setModelEnabled(model.id, enabled)
  } catch {
    await providers.fetchAll().catch(() => {})
  } finally {
    modelToggleLoading[model.id] = false
    await loadPage()
  }
}

async function syncProviderModels(provider) {
  if (!supportsDiscovery(provider)) return
  await prepareScope(provider.scope)
  syncing[provider.id] = true
  syncFailed[provider.id] = false
  try {
    await providers.syncModels(provider.id)
  } catch {
    syncFailed[provider.id] = true
  } finally {
    syncing[provider.id] = false
    await loadPage()
  }
}

async function testProvider(id) {
  testingProviderId.value = id
  try {
    await prepareScope(listedProviders.value.find((item) => item.id === id).scope)
    await providers.testProvider(id)
    $q.notify({ type: 'positive', message: 'Verbindung erfolgreich' })
  } finally {
    testingProviderId.value = null
  }
}

async function testModel(model, provider) {
  await prepareScope(provider.scope)
  await providers.testModel(model.id)
  $q.notify({ type: 'positive', message: 'Modell erfolgreich getestet' })
}

async function removeModel(model, provider) {
  const confirmed = await confirm(
    `Modell „${model.name || model.modelId}“ löschen? Zugewiesene Sessions verwenden danach das Standardmodell.`,
    'Modell löschen',
    { okLabel: 'Löschen' }
  )
  if (!confirmed) return
  await prepareScope(provider.scope)
  await providers.removeModel(model.id)
  await loadPage()
  $q.notify({ type: 'positive', message: 'Modell gelöscht' })
}

async function removeProvider(provider) {
  const confirmed = await confirm(
    `Provider „${provider.name}“ einschließlich aller Modelle und Zugangsdaten löschen?`,
    'Provider löschen',
    { okLabel: 'Löschen' }
  )
  if (!confirmed) return
  await prepareScope(provider.scope)
  await providers.removeProvider(provider.id)
  await loadPage()
  $q.notify({ type: 'positive', message: 'Provider gelöscht' })
}

watch(scope, () => {
  section.value = route.query.tab === 'management' ? 'management' : 'general'
  dialogOpen.value = false
  modelDialogOpen.value = false
  listedProviders.value = []
  listedModels.value = []
  void loadPage()
}, { immediate: true })
watch(() => route.query.tab, (tab) => { section.value = tab === 'management' ? 'management' : 'general' })
</script>
