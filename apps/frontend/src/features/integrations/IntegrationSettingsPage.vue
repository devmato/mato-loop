<template>
  <router-view v-if="showDetailPage" />
  <section v-else>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Integrationen</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">
          Externe Dienste für zusätzliche Agent-Funktionen verwalten.
        </p>
      </div>
      <div v-if="showInstances" class="settings-scope-actions row items-center col-auto">
        <q-btn
          class="col-auto"
          color="primary"
          unelevated
          no-caps
          no-wrap
          icon="add"
          label="Integration hinzufügen"
          :disable="integrations.loading || integrations.catalogLoading || !canCreateIntegration"
          @click="openCreateDialog"
        />
      </div>
    </header>

    <q-tabs
      v-model="section"
      dense
      no-caps
      align="left"
      active-color="primary"
      indicator-color="primary"
      class="q-mb-lg"
    >
      <q-tab name="general" icon="settings" label="Einstellungen" />
      <q-tab name="management" icon="extension" label="Verwaltung" />
    </q-tabs>

    <template v-if="admin && section === 'general'">
      <q-banner v-if="integrations.catalogLoadFailed" rounded class="app-surface-tonal q-mb-md">
        Die verfügbaren Integrationen konnten nicht geladen werden.
        <template #action>
          <q-btn flat no-caps color="primary" label="Erneut laden" @click="loadCatalog" />
        </template>
      </q-banner>

      <q-card flat class="app-surface">
        <q-list separator>
          <q-item
            v-if="integrations.catalogLoaded && !integrations.catalog.length"
            class="q-py-lg"
          >
            <q-item-section avatar>
              <q-avatar color="grey-7" text-color="white" icon="extension" />
            </q-item-section>
            <q-item-section>
              <q-item-label>Keine Integrationsadapter registriert</q-item-label>
            </q-item-section>
          </q-item>

          <q-item v-for="adapter in integrations.catalog" :key="adapter.adapterType" class="q-py-md">
            <q-item-section>
              <div class="row items-center q-col-gutter-md">
                <div class="col-12 col-sm">
                  <div class="row items-center no-wrap q-gutter-sm">
                    <q-avatar color="primary" text-color="white" :icon="typeIcon(adapter.integrationType)" />
                    <div>
                      <div class="row items-center q-gutter-sm">
                        <q-item-label class="text-weight-medium">{{ providerLabel(adapter.adapterType) }}</q-item-label>
                        <q-chip
                          dense
                          outline
                          color="grey-6"
                          :icon="executionLocationIcon(adapter.executionLocation)"
                        >
                          {{ executionLocationLabel(adapter.executionLocation) }}
                        </q-chip>
                      </div>
                      <q-item-label caption>{{ typeLabel(adapter.integrationType) }}</q-item-label>
                    </div>
                  </div>
                </div>
                <div class="col-12 col-sm-5">
                  <AppSelect
                    :model-value="adapter.availability"
                    outlined
                    dense
                    emit-value
                    map-options
                    label="Verfügbarkeit"
                    :options="availabilityOptions(adapter)"
                    :disable="integrations.catalogLoading"
                    :loading="integrations.catalogLoading"
                    @update:model-value="setAdapterAvailability(adapter.adapterType, $event)"
                  />
                </div>
              </div>
            </q-item-section>
          </q-item>
        </q-list>
        <q-inner-loading :showing="integrations.catalogLoading" />
      </q-card>
    </template>

    <q-banner v-if="showInstances && !personalCreatable" rounded class="app-surface-tonal q-mb-md">
      <template #avatar><q-icon name="person" color="primary" /></template>
      Die Verfügbarkeit wird je Integrationstyp festgelegt. Nur persönlich verfügbare Typen können hinzugefügt oder bearbeitet werden. Vorhandene Integrationen anderer Typen bleiben sichtbar und können gelöscht werden.
    </q-banner>

    <q-banner v-if="showInstances && integrations.loadFailed" rounded class="app-surface-tonal q-mb-md">
      Die Integrationen konnten nicht geladen werden.
      <template #action>
        <q-btn flat no-caps color="primary" label="Erneut laden" @click="reload" />
      </template>
    </q-banner>

    <q-banner v-if="showInstances && integrations.catalogLoadFailed" rounded class="app-surface-tonal q-mb-md">
      Die Verfügbarkeit der Integrationen konnte nicht geladen werden. Es können derzeit keine Integrationen bearbeitet oder hinzugefügt werden.
      <template #action>
        <q-btn flat no-caps color="primary" label="Erneut laden" @click="loadCatalog" />
      </template>
    </q-banner>

    <q-banner v-if="section === 'general'" rounded class="app-surface-tonal q-mb-md">
      Die Anbieter für Websuche und Webabrufe werden instanzweit in den allgemeinen Tool-Einstellungen festgelegt.
      <template v-if="auth.isAdmin" #action><q-btn flat no-caps color="primary" label="Tool-Einstellungen öffnen" :to="{ name: 'admin-tools' }" /></template>
    </q-banner>

    <q-card v-show="showInstances" flat class="app-surface integration-list-card">
      <q-list separator>
        <q-item v-if="integrations.loaded && !configuredIntegrations.length && !integrations.loading" class="q-py-lg">
          <q-item-section avatar>
            <q-avatar color="grey-7" text-color="white" icon="extension" />
          </q-item-section>
          <q-item-section>
            <q-item-label>Noch keine Integration konfiguriert</q-item-label>
            <q-item-label caption>Füge einen externen Dienst wie SearXNG, CalDAV, Mealie oder BookStack hinzu.</q-item-label>
          </q-item-section>
        </q-item>

        <q-item v-for="integration in configuredIntegrations" :key="integration.id" class="settings-list-item q-py-md">
          <q-item-section avatar top>
            <q-avatar
              color="primary"
              text-color="white"
              :icon="typeIcon(integration.integrationType)"
            />
          </q-item-section>
          <q-item-section>
            <div class="row items-center q-gutter-sm">
              <q-item-label class="settings-list-name text-weight-medium">{{ integration.name }}</q-item-label>
              <q-badge outline color="grey-7" :label="integration.scope === 'instance' ? 'Global' : 'Persönlich'" />
              <q-chip dense :color="integrationAvailable(integration) && integration.enabled ? 'positive' : 'grey-7'" text-color="white">
                {{ integrationAvailable(integration) ? (integration.enabled ? 'Aktiv' : 'Deaktiviert') : 'Nicht verfügbar' }}
              </q-chip>
            </div>
            <q-item-label caption>
              {{ typeLabel(integration.integrationType) }} ·
              {{ providerLabel(integration.providerType) }}
            </q-item-label>
            <q-item-label v-if="integration.integrationType === 'openscad'" caption lines="1">
              Computer: {{ executionAgentName(integration) }} ·
              {{ integration.config.binaryPath }}
            </q-item-label>
            <q-item-label v-else-if="integration.integrationType === 'mail' && integration.config?.imapHost" caption lines="1">
              IMAP: {{ integration.config.imapHost }}:{{ integration.config.imapPort }} · SMTP: {{ integration.config.smtpHost }}:{{ integration.config.smtpPort }}
            </q-item-label>
            <template v-else-if="integration.integrationType === 'browser'">
              <q-item-label caption>Computer: {{ executionAgentName(integration) }}</q-item-label>
              <q-item-label caption role="status">{{ browserStatusLabel(integrations.browserStatuses[integration.id]) }}</q-item-label>
            </template>
            <q-item-label v-else caption lines="1">
              {{ integration.config?.baseUrl || (integration.scope === 'instance' ? 'Globale Verbindung' : '') }}{{ integration.config?.username ? ` · ${integration.config.username}` : '' }}
            </q-item-label>
          </q-item-section>
          <q-item-section side top class="settings-list-actions">
            <div class="row items-center no-wrap q-gutter-sm">
              <q-toggle
                dense
                :model-value="integration.enabled"
                :aria-label="`${integration.name} ${integration.enabled ? 'deaktivieren' : 'aktivieren'}`"
                :disable="!canEditIntegration(integration) || togglingId !== null"
                @update:model-value="toggleIntegration(integration, $event)"
              />
              <div class="row items-center no-wrap q-gutter-xs">
                <q-btn flat round dense icon="edit" :aria-label="`${integration.name} bearbeiten`" :disable="!canEditIntegration(integration)" :to="{ name: admin ? 'admin-integration-edit' : 'settings-integration-edit', params: { integrationId: integration.id } }" />
                <q-btn flat round dense icon="delete_outline" color="negative" :aria-label="`${integration.name} löschen`" :loading="removingId === integration.id" :disable="removingId !== null || (integration.scope === 'instance' && !auth.isAdmin)" @click="removeIntegration(integration)" />
              </div>
            </div>
          </q-item-section>
        </q-item>
      </q-list>
      <q-inner-loading :showing="integrations.loading || (integrations.catalogLoading && !integrations.catalogLoaded)" />
    </q-card>

    <IntegrationCreateDialog v-if="showInstances" v-model="dialogOpen" :scope="admin ? 'instance' : 'personal'" @continue="router.push({ name: admin ? 'admin-integration-create' : 'settings-integration-create', query: $event })" />
  </section>
</template>

<script setup>
import { agentDisplayName } from 'src/features/computers/display'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useRoute, useRouter } from 'vue-router'
import { api } from 'src/boot/axios'
import IntegrationCreateDialog from 'src/features/integrations/IntegrationCreateDialog.vue'
import { browserStatusLabel } from 'src/utils/browser'
import { useConfirm } from 'src/composables/confirm'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useIntegrationStore } from 'stores/integrations'
import { useAuthStore } from 'stores/auth'

defineOptions({ name: 'IntegrationSettingsPage' })
const { admin } = defineProps({ admin: { type: Boolean, default: false } })

const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const { confirm } = useConfirm()
const integrations = useIntegrationStore()
const auth = useAuthStore()
const executionAgents = useExecutionAgentStore()
const dialogOpen = ref(false)
const removingId = ref(null)
const togglingId = ref(null)
const section = ref(route.query.tab === 'management' ? 'management' : 'general')
const instanceIntegrations = computed(() => [
  ...integrations.webfetch.providers,
  ...integrations.search.providers, ...integrations.calendar.accounts,
  ...integrations.mail.accounts, ...integrations.knowledge.bases,
  ...integrations.recipes.accounts, ...integrations.arcane.instances, ...integrations.music.instances
])
const storedPersonalIntegrations = ref([])
const showDetailPage = computed(() => route.name !== (admin ? 'admin-integrations' : 'settings-integrations'))
const showInstances = computed(() => section.value === 'management')
const personalIntegrations = computed(
  () => [
    ...instanceIntegrations.value,
    ...integrations.tbsDesk.accounts,
    ...integrations.openscad.instances,
    ...integrations.coding.instances,
    ...integrations.browser.instances.map((item) => ({ ...item, integrationType: 'browser', providerType: 'browser' }))
  ]
)
const configuredIntegrations = computed(() => (admin ? instanceIntegrations.value.map((item) => ({ ...item, scope: 'instance' })) : [
  ...personalIntegrations.value.map((item) => ({ ...item, scope: 'personal' })),
  ...storedPersonalIntegrations.value.filter((item) => !personalIntegrations.value.some((own) => own.id === item.id)),
]))
const backendAvailabilityOptions = [
  { label: 'Global und persönlich', value: 'full' },
  { label: 'Nur global', value: 'instance_only' },
  { label: 'Nicht verfügbar', value: 'disabled' }
]
const agentAvailabilityOptions = [
  { label: 'Persönlich verfügbar', value: 'full' },
  { label: 'Nicht verfügbar', value: 'disabled' }
]
const personalCreatable = computed(() => integrations.catalog.some((adapter) => integrations.adapterAvailable(adapter.adapterType, 'personal')))
const canCreateIntegration = computed(() => (
  integrations.catalogLoaded
   && integrations.catalog.some((adapter) => integrations.adapterAvailable(adapter.adapterType, admin ? 'instance' : 'personal'))
))

function availabilityOptions(adapter) {
  if (adapter.instanceOnly) return backendAvailabilityOptions.filter(option => option.value !== 'full')
  return adapter.executionLocation === 'agent' || adapter.personalOnly
    ? agentAvailabilityOptions
    : backendAvailabilityOptions
}

function canEditIntegration(integration) {
  return (integration.scope === 'personal' || auth.isAdmin) && integrationAvailable(integration)
}

function integrationAvailable(integration) {
  return integrations.adapterAvailable(integration.providerType, integration.scope)
}

async function toggleIntegration(integration, enabled) {
  togglingId.value = integration.id
  try {
    await integrations.setIntegrationEnabled(integration.id, enabled, integration.scope)
    await reload()
    $q.notify({ type: 'positive', message: enabled ? 'Integration aktiviert' : 'Integration deaktiviert' })
  } catch {
    // The central API interceptor displays the error.
  } finally {
    togglingId.value = null
  }
}

function openCreateDialog() {
  integrations.selectScope(admin ? 'instance' : 'personal')
  dialogOpen.value = true
}

function providerLabel(providerType) {
  return {
    jina: 'Jina Reader', searxng: 'SearXNG', caldav: 'CalDAV', imap_smtp: 'IMAP/SMTP', 'tbs-desk': 'tbs.Desk', bookstack: 'BookStack', mealie: 'Mealie', arcane: 'Arcane', sonobin: 'SonoBin', openscad: 'OpenSCAD', opencode: 'OpenCode', browser: 'Chromium'
  }[providerType] || providerType
}

function typeLabel(integrationType) {
  return {
    search: 'Websuche',
    webfetch: 'Webabruf',
    calendar: 'Kalender & Aufgaben',
    mail: 'E-Mail',
    support: 'Support',
    knowledge: 'Wissensdatenbank',
    recipes: 'Rezepte & Essensplanung',
    infrastructure: 'Docker-Verwaltung',
    music: 'Musikbibliothek',
    openscad: '3D-Modellierung',
    coding: 'Programmierung',
    browser: 'Browser'
  }[integrationType] || integrationType || 'Unbekannter Integrationstyp'
}

function typeIcon(integrationType) {
  return {
    search: 'travel_explore', calendar: 'calendar_month', mail: 'mail', support: 'confirmation_number', knowledge: 'menu_book', recipes: 'restaurant_menu', infrastructure: 'dns', music: 'library_music', openscad: 'view_in_ar', coding: 'code', browser: 'web'
  }[integrationType] || 'extension'
}

function executionLocationLabel(executionLocation) {
  if (executionLocation === 'agent') return 'Computer'
  if (executionLocation === 'backend') return 'Backend'
  return 'Unbekannt'
}

function executionLocationIcon(executionLocation) {
  if (executionLocation === 'agent') return 'computer'
  if (executionLocation === 'backend') return 'dns'
  return 'help_outline'
}

function executionAgentName(integration) {
  const agentId = integration.executionAgentId || integration.config?.executionAgentId || null
  return agentDisplayName(executionAgents.byId(agentId))
    || integration.executionAgentName
    || integration.config?.executionAgentName
    || 'Nicht verfügbarer Computer'
}

async function removeIntegration(integration) {
  const confirmed = await confirm(
    `Integration „${integration.name}“ vollständig löschen?`,
    'Integration löschen',
    { okLabel: 'Löschen' }
  )
  if (!confirmed) return
  integrations.selectScope(integration.scope)
  removingId.value = integration.id
  try {
    if (integration.integrationType === 'calendar') await integrations.removeCalendar(integration.id)
    else if (integration.integrationType === 'webfetch') await integrations.removeWebfetch(integration.id)
    else if (integration.integrationType === 'mail') await integrations.removeMail(integration.id)
    else if (integration.integrationType === 'support') await integrations.removeTbsDesk(integration.id)
    else if (integration.integrationType === 'knowledge') await integrations.removeKnowledge(integration.id)
    else if (integration.integrationType === 'recipes') await integrations.removeRecipes(integration.id)
    else if (integration.integrationType === 'infrastructure') await integrations.removeArcane(integration.id)
    else if (integration.integrationType === 'music') await integrations.removeSonoBin(integration.id)
    else if (integration.integrationType === 'openscad') await integrations.removeOpenScad(integration.id)
    else if (integration.integrationType === 'coding') await integrations.removeOpenCode(integration.id)
    else if (integration.integrationType === 'browser') await integrations.removeBrowser(integration.id)
    else await integrations.removeSearch(integration.id)
    $q.notify({ type: 'positive', message: 'Integration gelöscht' })
  } finally {
    removingId.value = null
    await reload()
  }
}

async function reload() {
  try {
    integrations.selectScope(admin ? 'instance' : 'personal')
    await Promise.all([
      integrations.fetchCatalog(),
      integrations.fetchAll(),
      executionAgents.fetchAll()
    ])
    const visible = (await api.get('/integrations/visible')).data
    storedPersonalIntegrations.value = visible.filter((item) => item.scope === 'personal')
  } catch {
    // The central API interceptor displays the error as a toast.
  }
}

async function loadCatalog() {
  try {
    await integrations.fetchCatalog()
  } catch {
    // The central API interceptor displays the error as a toast.
  }
}

async function setAdapterAvailability(adapterType, availability) {
  try {
    await integrations.setAdapterAvailability(adapterType, availability)
  } catch {
    // The central API interceptor displays the error as a toast.
  }
}

onMounted(() => { if (!showDetailPage.value) void reload() })
watch(showDetailPage, (detail) => {
  if (!detail) {
    if (route.query.tab === 'management') section.value = 'management'
    void reload()
  }
})
watch(() => admin, reload)
</script>
