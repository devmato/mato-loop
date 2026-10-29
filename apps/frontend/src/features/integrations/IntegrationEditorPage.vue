<template>
  <section :aria-busy="loading">
      <q-toolbar class="settings-detail-toolbar q-px-none q-mb-md">
        <q-btn flat round dense icon="arrow_back" aria-label="Zurück zu den Integrationen" :disable="saving || testing || accessRef?.saving" :to="{ name: listRoute, query: { tab: 'management' } }" />
        <q-avatar color="primary" text-color="white" :icon="dialogIcon" />
        <div class="col q-px-sm">
          <div class="text-h6 text-weight-bold">{{ integration?.name || 'Integration hinzufügen' }}</div>
          <div class="text-body2 app-muted">{{ providerDefaults[form.integration_type]?.label }} · {{ scope === 'instance' ? 'Globale Verbindung' : 'Persönliche Verbindung' }}</div>
        </div>
        <q-btn unelevated no-caps color="primary" icon="save" label="Speichern" :loading="saving || accessRef?.saving" :disable="blocked || loading || loadFailed || testing || (section === 'permissions' ? !accessRef?.canSave : !dirty)" @click="section === 'permissions' ? accessRef?.save() : save()" />
      </q-toolbar>

      <q-tabs v-model="section" dense no-caps align="left" active-color="primary" indicator-color="primary" class="q-mb-md">
        <q-tab name="settings" icon="settings" label="Einstellungen" :disable="saving || testing || accessRef?.saving" />
        <q-tab name="permissions" icon="security" label="Berechtigungen" :disable="!integration || loading || blocked || saving || testing || accessRef?.saving" />
      </q-tabs>

      <q-banner v-if="loadFailed" rounded class="app-surface-tonal q-mb-md">
        Die Integration konnte nicht geladen werden.
        <template #action><q-btn flat no-caps label="Erneut laden" @click="load" /></template>
      </q-banner>
      <q-banner v-else-if="blocked && !loading" rounded class="app-surface-tonal q-mb-md">Diese Integration kann hier nicht bearbeitet werden.</q-banner>
      <q-form v-if="!loading && !loadFailed && !blocked" v-show="section === 'settings'" ref="formRef" :inert="saving || testing" @submit.prevent="save">
        <q-card flat class="app-surface">
        <q-card-section class="q-gutter-md">
            <q-banner v-if="['search', 'webfetch'].includes(form.integration_type)" rounded class="app-surface-tonal">
              Der Anbieter wird in den allgemeinen Tool-Einstellungen festgelegt.
              <template #action><q-btn flat no-caps color="primary" label="Tool-Einstellungen öffnen" :to="{ name: 'admin-tools' }" /></template>
            </q-banner>
            <q-input dense
              v-model="form.name"
              outlined
              label="Name"
              maxlength="160"
              lazy-rules
              :rules="[requiredRule]"
            />
            <q-input dense
              v-if="!['openscad', 'mail', 'browser'].includes(form.integration_type)"
              v-model="form.base_url"
              outlined
              :label="baseUrlLabel"
              :placeholder="baseUrlPlaceholder"
              lazy-rules
              :rules="[requiredRule, urlRule]"
            />
            <template v-if="form.integration_type === 'mail'">
              <div class="text-subtitle2 text-weight-medium">Posteingang (IMAP)</div>
              <div class="row q-col-gutter-sm">
                <q-input dense v-model="form.imap_host" outlined label="IMAP-Host" class="col-12 col-sm" lazy-rules :rules="[requiredRule]" />
                <q-input dense v-model.number="form.imap_port" outlined type="number" label="IMAP-Port" class="col-5 col-sm-3" lazy-rules :rules="[portRule]" />
                <AppSelect dense v-model="form.imap_security" outlined emit-value map-options label="IMAP-Sicherheit" class="col-7 col-sm-4" :options="securityOptions" />
              </div>
              <div class="text-subtitle2 text-weight-medium">Postausgang (SMTP)</div>
              <div class="row q-col-gutter-sm">
                <q-input dense v-model="form.smtp_host" outlined label="SMTP-Host" class="col-12 col-sm" lazy-rules :rules="[requiredRule]" />
                <q-input dense v-model.number="form.smtp_port" outlined type="number" label="SMTP-Port" class="col-5 col-sm-3" lazy-rules :rules="[portRule]" />
                <AppSelect dense v-model="form.smtp_security" outlined emit-value map-options label="SMTP-Sicherheit" class="col-7 col-sm-4" :options="securityOptions" />
              </div>
              <q-input dense v-model="form.username" outlined label="Benutzername" lazy-rules :rules="[requiredRule]" />
              <q-input dense v-model="form.from_address" outlined type="email" label="Fester Absender" hint="Diese Absenderadresse wird für ausgehende E-Mails verwendet." lazy-rules :rules="[requiredRule, emailRule]" />
              <q-input dense
                v-model="form.password"
                outlined
                type="password"
                :label="integration?.hasCredential ? 'Neues Passwort (optional)' : 'Passwort'"
                :hint="integration?.hasCredential ? 'Leer lassen, um das gespeicherte Passwort beizubehalten.' : ''"
                lazy-rules
                :rules="isEditing ? [] : [requiredRule]"
              />
            </template>
            <q-banner
              v-if="['recipes', 'coding', 'infrastructure', 'webfetch'].includes(form.integration_type) && form.base_url.trim().toLowerCase().startsWith('http://')"
              dense
              rounded
              class="app-surface-tonal"
              role="alert"
            >
              <template #avatar><q-icon name="warning_amber" color="warning" /></template>
              HTTP überträgt Zugangsdaten unverschlüsselt. Verwende es nur in einem vertrauenswürdigen lokalen Netz.
            </q-banner>
            <q-banner v-if="form.integration_type === 'music'" rounded class="app-surface-tonal">
              <template #avatar><q-icon name="library_music" color="primary" /></template>
              <div class="text-weight-medium">SonoBin verbinden</div>
              <div class="text-caption q-mt-xs">Gib die SonoBin-Basis-URL ein. Der MCP-Endpunkt wird automatisch ergänzt.</div>
              <div class="text-caption">Verwende einen in SonoBin erstellten MCP-Token oder den konfigurierten statischen Bearer-Token. Für Download-Links muss <code>SONOBIN_MCP_PUBLIC_BASE_URL</code> in SonoBin gesetzt sein.</div>
            </q-banner>
            <div v-if="['openscad', 'browser'].includes(form.integration_type)" class="row items-start q-col-gutter-sm">
              <AppSelect dense
                v-model="form.executionAgentId"
                class="col"
                outlined
                emit-value
                map-options
                label="Computer"
                :options="executionAgentOptions"
                :loading="executionAgents.loading"
                lazy-rules
                :rules="[requiredRule]"
                @update:model-value="invalidateDiscovery"
              >
                <template #no-option>
                  <q-item>
                    <q-item-section class="app-muted">Kein Computer eingerichtet</q-item-section>
                  </q-item>
                </template>
              </AppSelect>
              <div v-if="form.integration_type === 'openscad'" class="col-auto q-pt-sm">
                <q-btn
                  outline
                  dense
                  no-caps
                  color="primary"
                  icon="search"
                  label="Erkennen"
                  :disable="!form.executionAgentId"
                  :loading="discovering"
                  @click="discoverOpenScad"
                />
              </div>
            </div>
            <q-input dense
              v-if="form.integration_type === 'openscad'"
              v-model="form.binaryPath"
              outlined
              label="Binary-Pfad"
              hint="Absoluter Pfad zur OpenSCAD-Binärdatei."
              lazy-rules
              :rules="[requiredRule, absolutePathRule]"
            />
            <q-banner
              v-if="form.integration_type === 'openscad' && discovery"
              dense
              rounded
              class="app-surface-tonal"
              role="status"
              aria-live="polite"
            >
              <div class="text-weight-medium">{{ discoveryStatus }}</div>
              <div v-if="discovery.version" class="text-caption q-mt-xs">Version: {{ discovery.version }}</div>
              <div class="text-caption">
                Headless-Vorschau {{ discovery.headlessPreview ? 'verfügbar' : 'nicht verfügbar' }}
              </div>
            </q-banner>
            <q-input dense
              v-if="['calendar', 'support'].includes(form.integration_type)"
              v-model="form.username"
              outlined
              label="Benutzername"
              lazy-rules
              :rules="[requiredRule]"
            />
            <q-input dense
              v-if="['calendar', 'support'].includes(form.integration_type)"
              v-model="form.password"
              outlined
              type="password"
              :label="integration?.hasCredential ? 'Neues Passwort (optional)' : 'Passwort'"
              :hint="integration?.hasCredential ? 'Leer lassen, um das gespeicherte Passwort beizubehalten.' : ''"
              lazy-rules
              :rules="isEditing ? [] : [requiredRule]"
            />
            <template v-if="form.integration_type === 'coding'">
              <q-input dense
                v-model="form.username"
                outlined
                label="Benutzername"
                lazy-rules
                :rules="[requiredRule]"
              />
              <q-input dense
                v-model="form.password"
                outlined
                type="password"
                :label="integration?.hasCredential ? 'Neues Passwort (optional)' : 'Passwort (optional)'"
                :hint="integration?.hasCredential ? 'Leer lassen, um das gespeicherte Passwort beizubehalten.' : ''"
                :disable="form.removePassword"
              />
              <q-toggle dense
                v-if="isEditing && integration?.hasCredential"
                v-model="form.removePassword"
                color="negative"
                label="Gespeichertes Passwort entfernen"
                @update:model-value="form.password = ''"
              />
            </template>
            <q-input dense
              v-if="form.integration_type === 'knowledge'"
              v-model="form.token_id"
              outlined
              :label="integration?.hasCredential ? 'Neue Token-ID (optional)' : 'Token-ID'"
              :hint="integration?.hasCredential ? 'Leer lassen, um den gespeicherten API-Token beizubehalten.' : 'API-Token aus dem BookStack-Benutzerprofil.'"
              lazy-rules
              :rules="isEditing ? [tokenPairRule] : [requiredRule]"
            />
            <q-input dense
              v-if="form.integration_type === 'recipes'"
              v-model="form.token"
              outlined
              type="password"
              :label="integration?.hasCredential ? 'Neuer API-Token (optional)' : 'API-Token'"
              :hint="integration?.hasCredential ? 'Leer lassen, um den gespeicherten API-Token beizubehalten.' : 'Langlebiger API-Token aus dem Mealie-Benutzerprofil.'"
              lazy-rules
              :rules="isEditing ? [] : [requiredRule]"
            />
            <q-input dense
              v-if="['infrastructure', 'webfetch'].includes(form.integration_type)"
              v-model="form.apiKey"
              outlined
              type="password"
              :label="hasApiKey ? 'Neuer API-Schlüssel (optional)' : 'API-Schlüssel'"
              :hint="form.integration_type === 'webfetch' ? (hasApiKey ? 'Leer lassen, um den gespeicherten Schlüssel beizubehalten.' : 'Optional, beispielsweise für höhere Jina-Kontingente.') : 'Unter Arcane → Einstellungen → API Keys erstellen. Für die gewünschten Umgebungen Docker-Rechte vergeben.'"
              :disable="form.integration_type === 'webfetch' && form.removeApiKey"
              lazy-rules
              :rules="isEditing || form.integration_type === 'webfetch' ? [] : [requiredRule]"
            />
            <q-toggle v-if="form.integration_type === 'webfetch' && hasApiKey" v-model="form.removeApiKey" color="negative" label="Gespeicherten API-Schlüssel entfernen" />
            <template v-if="form.integration_type === 'webfetch'">
              <q-input v-model.number="form.readerTimeout" outlined type="number" min="1" max="120" label="Standardzeitlimit" suffix="Sekunden" :rules="[value => Number.isInteger(value) && value >= 1 && value <= 120 || '1 bis 120 Sekunden eingeben.']" />
              <q-toggle v-model="form.removeOverlay" color="primary" label="Overlays standardmäßig entfernen" />
              <div class="text-caption app-muted">Entfernt überlagernde Elemente, erteilt aber keine Zustimmung. Sprache: persönliche Grundeinstellungen; Abrufmodus: automatisch, vom Agenten je Abruf änderbar.</div>
              <q-separator />
              <div class="text-subtitle2">Cookies</div>
              <div class="text-body2 app-muted">Verschlüsselt gespeichert und nur für den exakten Zielhost über HTTPS verwendet. Die Reader-Instanz erhält diese Cookies; alle berechtigten Benutzer dieser globalen Verbindung können sie nutzen. Keine persönlichen Login-Sitzungen hinterlegen.</div>
              <div class="text-caption app-muted">Passende Cookies verwenden den Browser-Modus ohne Cache. „Automatisch“ wählt ihn dafür; „curl“ ist mit diesen Cookies nicht verfügbar.</div>
              <div class="text-caption app-muted">Reader kann rohe Zeichen wie +, / oder = in Cookie-Werten nicht unverändert übertragen. Solche Werte werden abgelehnt. Werte nicht selbst umkodieren.</div>
              <div v-if="integration?.hasCookies" class="text-caption">Gespeicherte Zielhosts: {{ integration.config.cookieDomains.join(', ') }}. Werte werden nicht angezeigt. Neue Cookies ersetzen alle gespeicherten Cookies; ohne neue Angaben bleiben sie erhalten.</div>
              <q-toggle v-if="integration?.hasCookies" v-model="form.removeCookies" color="negative" label="Gespeicherte Cookies entfernen" />
              <div v-for="(cookie, index) in form.cookies" :key="index" class="row items-start q-col-gutter-sm">
                <q-input v-model.trim="cookie.domain" class="col-12 col-md-4" outlined label="Zielhost" placeholder="www.golem.de" maxlength="253" :rules="[requiredRule]" :disable="form.removeCookies" />
                <q-input v-model.trim="cookie.name" class="col-12 col-md-3" outlined label="Cookie-Name" maxlength="256" :rules="[requiredRule]" :disable="form.removeCookies" />
                <q-input v-model="cookie.value" class="col-10 col-md-4" outlined type="password" label="Cookie-Wert" maxlength="8192" :rules="[requiredRule]" :disable="form.removeCookies" autocomplete="off" />
                <div class="col-2 col-md-1"><q-btn flat round icon="delete_outline" aria-label="Neuen Cookie verwerfen" @click="form.cookies.splice(index, 1)" /></div>
              </div>
              <q-btn outline no-caps icon="add" label="Cookie hinzufügen" :disable="form.removeCookies || form.cookies.length >= 50" @click="form.cookies.push({ domain: '', name: '', value: '' })" />
            </template>
            <q-input dense
              v-if="form.integration_type === 'music'"
              v-model="form.bearerToken"
              outlined
              type="password"
              :label="integration?.hasCredential ? 'Neuer Bearer-Token (optional)' : 'Bearer-Token'"
              :hint="integration?.hasCredential ? 'Leer lassen, um den gespeicherten Token beizubehalten.' : ''"
              lazy-rules
              :rules="isEditing ? [] : [requiredRule]"
            />
            <q-input dense
              v-if="form.integration_type === 'knowledge'"
              v-model="form.token_secret"
              outlined
              type="password"
              :label="integration?.hasCredential ? 'Neues Token-Secret (optional)' : 'Token-Secret'"
              lazy-rules
              :rules="isEditing ? [tokenPairRule] : [requiredRule]"
            />
            <q-toggle dense v-model="form.enabled" color="primary" label="Integration aktivieren" />
            <template v-if="form.integration_type === 'browser'">
              <p class="text-caption app-muted">Jeder Chat erhält einen eigenen Browser mit getrennten Tabs und Anmeldungen. Beim Schließen des Browsers gehen diese verloren. Agenten können ihn auch ohne geöffnete Browser-App nutzen; Integrationsauswahl und Werkzeugrechte bleiben maßgeblich.</p>
              <q-btn v-if="integration?.id" outline no-caps icon="terminal" label="Browser einrichten / Status anzeigen" @click="setupOpen = true" />
              <div v-else class="text-caption">Nach dem Speichern kannst du die Einrichtung öffnen.</div>
            </template>
        </q-card-section>
        <q-separator />
        <q-card-section>
          <q-btn outline no-caps icon="network_check" label="Verbindung testen" :loading="testing" :disable="!integration || saving || dirty" @click="testConnection" />
          <div v-if="!integration || dirty" class="text-caption app-muted q-mt-sm">Speichere die Konfiguration vor dem Verbindungstest.</div>
          <div v-if="testResult" class="text-body2 q-mt-sm" role="status">{{ testResult }}</div>
        </q-card-section>
        </q-card>
      </q-form>
      <IntegrationAccessPage v-if="integration && accessOpened && !loading && !loadFailed && !blocked" v-show="section === 'permissions'" ref="accessRef" :key="integration.id" embedded />
      <q-inner-loading :showing="loading" />
  <BrowserSetupDialog v-if="setupOpen && integration?.id" v-model="setupOpen" :integration-id="integration.id" />
  </section>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { useConfirm } from 'src/composables/confirm'
import { useAuthStore } from 'stores/auth'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useIntegrationStore } from 'stores/integrations'
import BrowserSetupDialog from 'src/features/browser/BrowserSetupDialog.vue'
import IntegrationAccessPage from './IntegrationAccessPage.vue'
import { agentDisplayName } from 'src/features/computers/display'
import { providerDefaults, integrationTypes as allIntegrationTypes } from './configuration'

defineOptions({ name: 'IntegrationEditorPage' })

const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { confirm } = useConfirm()
const integrations = useIntegrationStore()
const integration = ref(null)
const loading = ref(false)
const loadFailed = ref(false)
const saved = ref('')
const testing = ref(false)
const testResult = ref('')
const scope = computed(() => String(route.name).startsWith('admin-') ? 'instance' : 'personal')
const listRoute = computed(() => scope.value === 'instance' ? 'admin-integrations' : 'settings-integrations')
const isEditing = computed(() => Boolean(route.params.integrationId))
const section = computed({
  get: () => isEditing.value && route.query.tab === 'permissions' ? 'permissions' : 'settings',
  set: (tab) => router.replace({ query: { ...route.query, tab } })
})
const accessRef = ref(null)
const accessOpened = ref(false)
const blocked = computed(() => scope.value === 'instance' && !auth.isAdmin || !integrations.adapterAvailable(form.provider_type, scope.value) || isEditing.value && !integration.value)
const dirty = computed(() => Boolean(saved.value) && JSON.stringify(form) !== saved.value)
let loadVersion = 0
const executionAgents = useExecutionAgentStore()
const formRef = ref(null)
const saving = ref(false)
const setupOpen = ref(false)
const baseUrlLabel = computed(
  () => `${providerDefaults[form.integration_type]?.label}-Basis-URL`
)
const baseUrlPlaceholder = computed(
  () => providerDefaults[form.integration_type]?.placeholder
)
const requiresHttps = computed(() => !['search', 'recipes', 'coding', 'support', 'infrastructure', 'webfetch'].includes(form.integration_type))
const dialogIcon = computed(() => ({ coding: 'code', mail: 'mail', support: 'confirmation_number', music: 'library_music', infrastructure: 'dns', openscad: 'view_in_ar', browser: 'web' })[form.integration_type] || 'extension')
const securityOptions = [
  { label: 'TLS', value: 'tls' },
  { label: 'STARTTLS', value: 'starttls' }
]
const executionAgentOptions = computed(() => {
  const options = executionAgents.items.map((agent) => ({ label: agentDisplayName(agent), value: agent.id }))
  if (form.executionAgentId && !executionAgents.byId(form.executionAgentId)) {
    options.unshift({ label: 'Nicht verfügbarer Computer', value: form.executionAgentId })
  }
  return options
})
const form = reactive(defaultForm())
const hasApiKey = computed(() => form.integration_type === 'webfetch' ? integration.value?.hasApiKey ?? integration.value?.hasCredential : integration.value?.hasCredential)
const discovering = ref(false)
const discovery = ref(null)
let discoveryVersion = 0
const discoveryStatus = computed(() => {
  const availability = discovery.value?.availability ?? discovery.value?.available
  const available = availability === true || availability === 'available'
  return available ? 'OpenSCAD wurde gefunden.' : 'OpenSCAD wurde nicht gefunden.'
})

function defaultForm() {
  return {
    integration_type: 'search',
    provider_type: 'searxng',
    name: 'SearXNG',
    base_url: '',
    username: '',
    password: '',
    removePassword: false,
    imap_host: '',
    imap_port: 993,
    imap_security: 'tls',
    smtp_host: '',
    smtp_port: 465,
    smtp_security: 'tls',
    from_address: '',
    token_id: '',
    token_secret: '',
    token: '',
    apiKey: '',
    removeApiKey: false,
    readerTimeout: 90,
    removeOverlay: false,
    cookies: [],
    removeCookies: false,
    bearerToken: '',
    executionAgentId: null,
    binaryPath: '/usr/bin/openscad',
    enabled: true
  }
}

async function fetchComputers() {
  try {
    await executionAgents.fetchAll()
  } catch {
    // The central API interceptor displays the error as a toast.
  }
}

function requiredRule(value) {
  return Boolean(String(value || '').trim()) || 'Pflichtfeld'
}

function absolutePathRule(value) {
  return String(value || '').trim().startsWith('/') || 'Bitte gib einen absoluten Pfad ein.'
}

function portRule(value) {
  const port = Number(value)
  return (Number.isInteger(port) && port >= 1 && port <= 65535) || 'Bitte gib einen gültigen Port ein.'
}

function emailRule(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value || '')) || 'Bitte gib eine gültige E-Mail-Adresse ein.'
}

async function discoverOpenScad() {
  if (!form.executionAgentId || discovering.value) return
  const version = ++discoveryVersion
  const executionAgentId = form.executionAgentId
  discovering.value = true
  try {
    const result = await integrations.discoverOpenScad(executionAgentId)
    if (
      version !== discoveryVersion
      || blocked.value
      || form.integration_type !== 'openscad'
      || form.executionAgentId !== executionAgentId
    ) return
    discovery.value = result
    const path = result?.executablePath
    if (path) form.binaryPath = path
  } catch {
    if (version === discoveryVersion) discovery.value = null
  } finally {
    if (version === discoveryVersion) discovering.value = false
  }
}

function invalidateDiscovery() {
  discoveryVersion += 1
  discovering.value = false
  discovery.value = null
}

function tokenPairRule() {
  const hasId = Boolean(String(form.token_id || '').trim())
  const hasSecret = Boolean(form.token_secret)
  if (hasId === hasSecret) return true
  return 'Bitte Token-ID und Token-Secret gemeinsam angeben oder beide leer lassen.'
}

function urlRule(value) {
  const label = providerDefaults[form.integration_type].label
  try {
    const protocol = new URL(value).protocol
    const allowed = requiresHttps.value
      ? protocol === 'https:'
      : ['http:', 'https:'].includes(protocol)
    return allowed || (requiresHttps.value
      ? `${label} erfordert eine HTTPS-URL.`
      : 'Bitte gib eine gültige HTTP-URL ein.')
  } catch {
    return requiresHttps.value
      ? `${label} erfordert eine gültige HTTPS-URL.`
      : 'Bitte gib eine gültige HTTP-URL ein.'
  }
}

async function save() {
  if (blocked.value || loading.value || saving.value || testing.value || loadFailed.value) return
  if (!await formRef.value?.validate()) return
  integrations.selectScope(scope.value)
  const previousIds = new Set(integrations.configuredIntegrations.map((item) => item.id))
  saving.value = true
  try {
    if (form.integration_type === 'browser') {
      await integrations.saveBrowser(integration.value?.id, {
        name: form.name.trim(), executionAgentId: form.executionAgentId, enabled: form.enabled, config: {}
      })
    } else if (form.integration_type === 'openscad') {
      const payload = {
        executionAgentId: form.executionAgentId,
        name: form.name.trim(),
        config: { binaryPath: form.binaryPath.trim() },
        enabled: form.enabled
      }
      if (integration.value) await integrations.updateOpenScad(integration.value.id, payload)
      else await integrations.createOpenScad(payload)
    } else if (form.integration_type === 'mail') {
      const payload = {
        integration_type: 'mail',
        provider_type: 'imap_smtp',
        name: form.name.trim(),
        imap_host: form.imap_host.trim(),
        imap_port: Number(form.imap_port),
        imap_security: form.imap_security,
        smtp_host: form.smtp_host.trim(),
        smtp_port: Number(form.smtp_port),
        smtp_security: form.smtp_security,
        username: form.username.trim(),
        from_address: form.from_address.trim(),
        password: form.password,
        enabled: form.enabled
      }
      if (integration.value) await integrations.updateMail(integration.value.id, payload)
      else await integrations.createMail(payload)
    } else if (form.integration_type === 'coding') {
      const payload = {
        name: form.name.trim(),
        baseUrl: form.base_url.trim(),
        username: form.username.trim(),
        password: form.password,
        removePassword: form.removePassword,
        enabled: form.enabled
      }
      if (integration.value) await integrations.updateOpenCode(integration.value.id, payload)
      else await integrations.createOpenCode(payload)
    } else if (form.integration_type === 'support') {
      const payload = {
        name: form.name.trim(),
        baseUrl: form.base_url.trim(),
        username: form.username.trim(),
        password: form.password,
        enabled: form.enabled
      }
      if (integration.value) await integrations.updateTbsDesk(integration.value.id, payload)
      else await integrations.createTbsDesk(payload)
    } else if (form.integration_type === 'music') {
      const payload = {
        integration_type: 'music',
        provider_type: 'sonobin',
        name: form.name.trim(),
        baseUrl: form.base_url.trim(),
        bearerToken: form.bearerToken,
        enabled: form.enabled
      }
      if (integration.value) await integrations.updateSonoBin(integration.value.id, payload)
      else await integrations.createSonoBin(payload)
    } else if (form.integration_type === 'webfetch') {
      if (integration.value?.hasCookies && (form.removeCookies || form.cookies.length) && !await confirm('Die gespeicherten Cookies werden durch diese Angaben ersetzt. Fortfahren?', 'Cookies ersetzen?', { okLabel: 'Ersetzen', okColor: 'negative' })) return
      const payload = {
        name: form.name.trim(), base_url: form.base_url.trim(), api_key: form.removeApiKey ? '' : form.apiKey || null, enabled: form.enabled,
        timeout_seconds: form.readerTimeout, remove_overlay: form.removeOverlay,
        cookies: form.removeCookies ? [] : form.cookies.length ? form.cookies : null
      }
      if (integration.value) await integrations.updateWebfetch(integration.value.id, payload)
      else await integrations.createWebfetch(payload)
    } else if (form.integration_type === 'infrastructure') {
      const payload = { name: form.name.trim(), base_url: form.base_url.trim(), api_key: form.apiKey, enabled: form.enabled }
      if (integration.value) await integrations.updateArcane(integration.value.id, payload)
      else await integrations.createArcane(payload)
    } else {
      const payload = {
        integration_type: form.integration_type,
        provider_type: form.provider_type,
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        enabled: form.enabled
      }
      if (form.integration_type === 'calendar') {
        payload.username = form.username.trim()
        payload.password = form.password
        if (integration.value) await integrations.updateCalendar(integration.value.id, payload)
        else await integrations.createCalendar(payload)
      } else if (form.integration_type === 'knowledge') {
        payload.token_id = form.token_id.trim()
        payload.token_secret = form.token_secret
        if (integration.value) await integrations.updateKnowledge(integration.value.id, payload)
        else await integrations.createKnowledge(payload)
      } else if (form.integration_type === 'recipes') {
        payload.token = form.token
        if (integration.value) await integrations.updateRecipes(integration.value.id, payload)
        else await integrations.createRecipes(payload)
      } else if (integration.value) await integrations.updateSearch(integration.value.id, payload)
      else await integrations.createSearch(payload)
    }
    const stored = integration.value
      ? integrations.configuredIntegrations.find((item) => item.id === integration.value.id)
      : integrations.configuredIntegrations.find((item) => !previousIds.has(item.id) && item.integrationType === form.integration_type)
    saved.value = JSON.stringify(form)
    testResult.value = ''
    $q.notify({ type: 'positive', message: 'Integration gespeichert' })
    if (stored && !isEditing.value) {
      saving.value = false
      await router.replace({ name: scope.value === 'instance' ? 'admin-integration-edit' : 'settings-integration-edit', params: { integrationId: stored.id } })
    } else if (stored) {
      assignForm(stored)
      if (accessRef.value && !accessRef.value.dirty) await accessRef.value.load()
    }
    else {
      saving.value = false
      await router.replace({ name: listRoute.value, query: { tab: 'management' } })
    }
  } catch {
    // The central API interceptor displays the error; keep the current draft.
  } finally {
    saving.value = false
  }
}

function assignForm(value = null) {
  invalidateDiscovery()
  integration.value = value
  Object.assign(form, defaultForm(), value ? {
    integration_type: value.integrationType,
    provider_type: value.providerType,
    name: value.name,
    base_url: value.config?.baseUrl || '',
    username: value.config?.username || '',
    imap_host: value.config?.imapHost || '',
    imap_port: value.config?.imapPort || 993,
    imap_security: value.config?.imapSecurity || 'tls',
    smtp_host: value.config?.smtpHost || '',
    smtp_port: value.config?.smtpPort || 465,
    smtp_security: value.config?.smtpSecurity || 'tls',
    from_address: value.config?.fromAddress || '',
    password: '',
    removePassword: false,
    token_id: '',
    token_secret: '',
    token: '',
    apiKey: '',
    bearerToken: '',
    readerTimeout: value.config?.timeoutSeconds ?? 90,
    removeOverlay: value.config?.removeOverlay ?? false,
    executionAgentId: value.executionAgentId
      || value.config?.executionAgentId
      || null,
    binaryPath: value.config?.binaryPath || '/usr/bin/openscad',
    enabled: value.enabled
  } : {})
  if (!value) {
    const type = allIntegrationTypes.find((item) => item.value === route.query.type)?.value
    form.integration_type = type || ''
    form.provider_type = providerDefaults[type]?.provider || ''
    form.name = typeof route.query.name === 'string' ? route.query.name : providerDefaults[type]?.label || ''
    if (type === 'coding') form.username = 'opencode'
    if (type === 'webfetch') form.base_url = 'https://r.jina.ai'
  }
  saved.value = JSON.stringify(form)
  if (['openscad', 'browser'].includes(form.integration_type)) void fetchComputers()
}

async function load() {
  const version = ++loadVersion
  loading.value = true
  loadFailed.value = false
  saved.value = ''
  integration.value = null
  accessOpened.value = section.value === 'permissions'
  setupOpen.value = false
  testResult.value = ''
  invalidateDiscovery()
  try {
    if (scope.value === 'instance' && !auth.isAdmin) return
    integrations.selectScope(scope.value)
    await integrations.fetchCatalog()
    if (version !== loadVersion) return
    await integrations.fetchAll()
    if (version !== loadVersion) return
    const stored = isEditing.value ? integrations.configuredIntegrations.find((item) => item.id === route.params.integrationId) : null
    if (isEditing.value && !stored) { loadFailed.value = true; return }
    assignForm(stored)
  } catch { if (version === loadVersion) loadFailed.value = true }
  finally { if (version === loadVersion) loading.value = false }
}

async function testConnection() {
  if (!integration.value || dirty.value || blocked.value || saving.value || testing.value) return
  integrations.selectScope(scope.value)
  testing.value = true
  testResult.value = ''
  try {
    testResult.value = (await integrations.testIntegration(integration.value)).message
  } catch { testResult.value = 'Verbindungstest fehlgeschlagen.' }
  finally { testing.value = false }
}

async function confirmDiscardedChanges() {
  if (saving.value || testing.value || accessRef.value?.saving) return false
  if (!dirty.value && !accessRef.value?.dirty) return true
  return confirm('Sollen die ungespeicherten Änderungen verworfen werden?', 'Änderungen verwerfen?', { okLabel: 'Verwerfen' })
}

watch([() => route.name, () => route.params.integrationId, () => route.query.type, () => route.query.name], load, { immediate: true })
watch(section, (tab) => { if (tab === 'permissions') accessOpened.value = true }, { immediate: true })
watch(form, () => { testResult.value = '' })
onBeforeRouteLeave(confirmDiscardedChanges)
onBeforeRouteUpdate((to, from) => (
  to.name === from.name && to.params.integrationId === from.params.integrationId
  && to.query.type === from.query.type && to.query.name === from.query.name
    ? true : confirmDiscardedChanges()
))
onBeforeUnmount(() => { loadVersion += 1; invalidateDiscovery() })
</script>
