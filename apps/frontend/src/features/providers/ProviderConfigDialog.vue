<template>
  <q-dialog :model-value="modelValue" :transition-duration="0" @hide="handleHide">
    <q-card class="provider-dialog app-surface">
      <q-card-section class="row items-start no-wrap q-gutter-md">
        <q-avatar color="primary" text-color="white" :icon="dialogIcon" />
        <div class="col">
          <div class="text-h6">{{ dialogTitle }}</div>
          <div class="text-body2 app-muted">
            {{ isEditing ? 'Verbindung und Zugang bearbeiten.' : 'Wähle einen Provider für Chat- oder Medienmodelle.' }}
          </div>
        </div>
        <q-btn flat round dense icon="close" aria-label="Dialog schließen" @click="close" />
      </q-card-section>

      <q-separator />

      <q-card-section class="provider-dialog__body q-gutter-md scroll">
        <AppSelect dense
          v-model="providerType"
          outlined
          emit-value
          map-options
          label="Provider"
          :options="providerOptions"
          :disable="isEditing"
        />

        <div v-if="providerType" class="provider-form-grid">
          <AppSelect dense
            v-if="auth.modelTrustEnabled"
            v-model="form.trust_level"
            outlined
            emit-value
            map-options
            label="Vertrauenslevel"
            :options="trustLevelOptions"
            @update:model-value="selectTrust"
          />
          <q-toggle dense
            v-model="form.pseudonymization_enabled"
            color="primary"
            label="Kennungen und Secrets pseudonymisieren"
          />
          <AppSelect dense
            v-model="form.memory_policy"
            class="provider-form-section"
            outlined
            emit-value
            map-options
            label="Memory-Policy"
            :options="memoryPolicyOptions"
            hint="Kann Memory für alle Modelle dieses Providers erlauben oder sperren."
          />
        </div>

        <q-form v-if="isConfiguredProvider" ref="formRef" class="provider-form-grid">
          <q-input dense
            v-model="form.name"
            outlined
            label="Name"
            lazy-rules
            :rules="[requiredRule]"
          />
          <q-input dense
            v-model="form.base_url"
            outlined
            :label="isAzureSpeech ? 'Azure-Speech-Endpunkt' : 'Base URL'"
            :placeholder="endpointPlaceholder"
            :hint="isAzureSpeech ? 'HTTPS-Ressourcenadresse oder regionaler Endpunkt, ohne API-Pfad oder Abfrageparameter.' : ''"
            lazy-rules
            :rules="[requiredRule, urlRule]"
          />
          <AppSelect dense
            v-if="providerType === 'openai-compatible'"
            v-model="form.api_type"
            outlined
            emit-value
            map-options
            label="API-Protokoll"
            :options="apiTypes"
          />
          <q-input dense
            v-model="form.api_key"
            outlined
            type="password"
            :label="provider?.hasCredential ? 'Neuer API-Key (optional)' : isAzureSpeech ? 'Azure-Speech-API-Key' : 'API-Key (optional)'"
            :hint="apiKeyHint"
            :disable="form.remove_api_key"
            lazy-rules
            :rules="isAzureSpeech && !provider?.hasCredential ? [requiredRule] : []"
          />
          <q-checkbox dense
            v-if="provider?.hasCredential"
            v-model="form.remove_api_key"
            class="provider-form-section"
            label="Gespeicherten API-Key entfernen"
          />

          <q-banner v-if="isAzureSpeech" rounded class="provider-form-section app-surface-tonal">
            Azure Speech Fast Transcription transkribiert abgeschlossene Sprachnachrichten und Audiodateien.
            Keine Live-Transkription, kein Chat und keine Tool-Aufrufe.
            Das Originalaudio wird ohne Pseudonymisierung an Azure gesendet – auch wenn der Schutz für Kennungen und Secrets aktiviert ist.
            Nach der Modellerkennung kannst du das Modell im Agentenprofil für die Transkription auswählen.
          </q-banner>
        </q-form>

        <q-form
          v-else-if="providerType === 'openai-codex'"
          ref="formRef"
          class="provider-form-grid"
        >
          <q-banner rounded class="app-surface-tonal">
            ChatGPT Codex nutzt dein ChatGPT-Abonnement ausschließlich als LLM in der MatoLoop-Harness.
          </q-banner>

          <div class="row items-center q-gutter-sm provider-form-section">
            <q-chip :color="providers.codex.authenticated ? 'positive' : 'grey-7'" text-color="white">
              {{ providers.codex.authenticated ? 'Verbunden' : 'Nicht verbunden' }}
            </q-chip>
          </div>

          <div v-if="codexLogin" class="q-gutter-md">
            <q-banner rounded class="app-surface-tonal">
              Öffne die ChatGPT-Anmeldeseite und gib dort den folgenden einmaligen Code ein. Der Code läuft nach 15 Minuten ab.
            </q-banner>
            <q-input dense
              :model-value="codexLogin.userCode"
              outlined
              readonly
              label="Einmalcode"
              input-class="text-h6 text-weight-bold text-center"
            >
              <template #append>
                <q-btn flat round dense icon="content_copy" aria-label="Einmalcode kopieren" @click="copyUserCode" />
              </template>
            </q-input>
            <div class="text-caption app-muted">MatoLoop wartet auf den Abschluss der Anmeldung.</div>
          </div>
        </q-form>
      </q-card-section>

      <q-separator />
      <q-card-actions class="q-pa-md">
        <q-btn
          flat
          no-caps
          :label="providerType === 'openai-codex' ? 'Schließen' : 'Abbrechen'"
          @click="close"
        />
        <q-space />
        <template v-if="isConfiguredProvider">
          <q-btn
            unelevated
            no-caps
            color="primary"
            :label="isEditing ? 'Änderungen speichern' : 'Provider hinzufügen'"
            :loading="saving"
            @click="saveConfiguredProvider"
          />
        </template>
        <template v-else-if="providerType === 'openai-codex'">
          <q-btn
            v-if="isEditing"
            unelevated
            no-caps
            color="primary"
            label="Einstellungen speichern"
            :loading="saving"
            @click="saveProviderPolicy"
          />
          <q-btn
            v-if="providers.codex.authenticated"
            flat
            no-caps
            color="negative"
            label="Verbindung trennen"
            :loading="codexLoading"
            @click="logoutCodex"
          />
          <q-btn
            v-else
            unelevated
            no-caps
            color="primary"
            :label="codexLogin ? 'ChatGPT-Anmeldeseite öffnen' : 'Mit ChatGPT verbinden'"
            :loading="codexLoading"
            @click="codexLogin ? openAuthorization() : startCodexLogin()"
          />
        </template>
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { copyToClipboard, useQuasar } from 'quasar'
import { useConfirm } from 'src/composables/confirm'
import { notifyError } from 'src/utils/notifications'
import { useProviderStore } from 'stores/providers'
import { useAuthStore } from 'stores/auth'

defineOptions({ name: 'ProviderConfigDialog' })

const props = defineProps({
  modelValue: { type: Boolean, required: true },
  provider: { type: Object, default: null }
})
const emit = defineEmits(['update:modelValue', 'provider-created', 'provider-updated'])

const $q = useQuasar()
const { confirm } = useConfirm()
const providers = useProviderStore()
const auth = useAuthStore()
const providerType = ref(null)
const formRef = ref(null)
const saving = ref(false)
const codexLoading = ref(false)
const codexLogin = ref(null)
const form = reactive(defaultForm())
let pollTimer = null
let pollInFlight = false

const apiTypes = [
  { label: 'Chat Completions', value: 'chat_completions' },
  { label: 'Responses API', value: 'responses' }
]
const trustLevelOptions = [
  { label: 'Öffentlich', value: 'public' },
  { label: 'Privat', value: 'private' }
]
const memoryPolicyOptions = [
  { label: 'Globale Einstellung übernehmen', value: 'inherit' },
  { label: 'Erlauben', value: 'allow' },
  { label: 'Sperren', value: 'deny' }
]
const isEditing = computed(() => Boolean(props.provider))
const hasCodexProvider = computed(() => providers.providers.some((item) => item.adapterType === 'openai-codex'))
const isAzureSpeech = computed(() => providerType.value === 'azure-speech')
const isConfiguredProvider = computed(() => ['openai-compatible', 'lemonade', 'azure-speech'].includes(providerType.value))
const endpointPlaceholder = computed(() => {
  if (isAzureSpeech.value) return 'https://my-speech.cognitiveservices.azure.com'
  return providerType.value === 'lemonade' ? 'http://lemonade-host:13305/v1' : 'https://api.example.com/v1'
})
const apiKeyHint = computed(() => {
  if (isAzureSpeech.value && form.remove_api_key) return 'Ohne API-Key ist keine Transkription möglich.'
  if (props.provider?.hasCredential) return 'Leer lassen, um den gespeicherten API-Key beizubehalten.'
  return isAzureSpeech.value ? 'Für die Transkription ist ein API-Key der Azure-Speech-Ressource erforderlich.' : ''
})
const providerOptions = computed(() => [
  {
    label: 'ChatGPT Codex',
    value: 'openai-codex',
    icon: 'key',
    description: 'ChatGPT-Abonnement über Gerätecode-Anmeldung',
    disable: !isEditing.value && hasCodexProvider.value
  },
  {
    label: 'OpenAI-kompatibel',
    value: 'openai-compatible',
    icon: 'hub',
    description: 'Chat Completions oder Responses API'
  },
  {
    label: 'Lemonade',
    value: 'lemonade',
    icon: 'developer_board',
    description: 'Lokaler Lemonade-Server für Chat- und Medienmodelle'
  },
  {
    label: 'Azure Speech',
    value: 'azure-speech',
    icon: 'graphic_eq',
    description: 'Fast Transcription für fertige Sprachnachrichten und Audiodateien'
  }
])
const dialogTitle = computed(() => isEditing.value ? `${props.provider.name} bearbeiten` : 'Provider hinzufügen')
const dialogIcon = computed(() => ({
  'openai-codex': 'key',
  lemonade: 'developer_board',
  'azure-speech': 'graphic_eq'
})[providerType.value] || 'hub')

function defaultForm(type = null) {
  const isLemonade = type === 'lemonade'
  return {
    name: '',
    base_url: '',
    api_type: 'chat_completions',
    api_key: '',
    remove_api_key: false,
    trust_level: isLemonade ? 'private' : 'public',
    pseudonymization_enabled: !isLemonade,
    memory_policy: 'inherit'
  }
}

function selectTrust(value) {
  if (value === 'public') form.pseudonymization_enabled = true
}

function requiredRule(value) {
  return Boolean(String(value || '').trim()) || 'Pflichtfeld'
}

function urlRule(value) {
  const message = isAzureSpeech.value
    ? 'Bitte gib einen HTTPS-Endpunkt ohne Zugangsdaten, API-Pfad oder Abfrageparameter ein.'
    : 'Bitte gib eine gültige HTTP-URL ein.'
  try {
    const url = new URL(value)
    if (isAzureSpeech.value) {
      return (url.protocol === 'https:' && !url.username && !url.password
        && url.pathname === '/' && !url.search && !url.hash) || message
    }
    return ['http:', 'https:'].includes(url.protocol) || message
  } catch {
    return message
  }
}

function initialize() {
  clearPolling()
  codexLogin.value = null
  providerType.value = props.provider?.adapterType || null
  Object.assign(form, defaultForm(providerType.value), props.provider ? {
    name: props.provider.name,
    base_url: props.provider.config?.baseUrl || props.provider.config?.base_url || '',
    api_type: props.provider.config?.apiType || props.provider.config?.api_type || 'chat_completions',
    trust_level: props.provider.trustLevel || 'public',
    pseudonymization_enabled: props.provider.pseudonymizationEnabled === true,
    memory_policy: props.provider.memoryPolicy || 'inherit'
  } : {})
}

async function saveConfiguredProvider() {
  if (!await formRef.value?.validate()) return
  saving.value = true
  try {
    const payload = {
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      api_key: form.remove_api_key ? null : form.api_key.trim() || null,
      remove_api_key: form.remove_api_key,
      trust_level: form.trust_level,
      pseudonymization_enabled: form.pseudonymization_enabled,
      memory_policy: form.memory_policy
    }
    if (providerType.value === 'openai-compatible') payload.api_type = form.api_type
    let providerId = props.provider?.id
    if (props.provider) {
      await providers.updateProvider(props.provider.id, payload)
    } else {
      providerId = await providers.createProvider(providerType.value, payload)
    }
    $q.notify({ type: 'positive', message: 'Provider gespeichert' })
    close()
    if (!props.provider && providerId) emit('provider-created', providerId)
    else if (props.provider) emit('provider-updated', props.provider.id)
  } finally {
    saving.value = false
  }
}

async function saveProviderPolicy() {
  if (!props.provider) return
  saving.value = true
  try {
    await providers.setProviderPolicy(props.provider.id, providerPolicy())
    $q.notify({ type: 'positive', message: 'Provider gespeichert' })
    emit('provider-updated', props.provider.id)
    close()
  } finally {
    saving.value = false
  }
}

async function startCodexLogin() {
  codexLoading.value = true
  try {
    clearPolling()
    const login = await providers.startCodexLogin()
    codexLogin.value = { ...login, status: 'pending' }
    openAuthorization()
    pollTimer = window.setInterval(
      pollCodex,
      Math.max(1000, (login.intervalSeconds || 5) * 1000)
    )
  } finally {
    codexLoading.value = false
  }
}

function openAuthorization() {
  if (codexLogin.value?.verificationUrl) {
    window.open(codexLogin.value.verificationUrl, '_blank', 'noopener,noreferrer')
  }
}

async function copyUserCode() {
  if (!codexLogin.value?.userCode) return
  try {
    await copyToClipboard(codexLogin.value.userCode)
    $q.notify({ type: 'positive', message: 'Einmalcode kopiert' })
  } catch {
    $q.notify({ type: 'negative', message: 'Einmalcode konnte nicht kopiert werden' })
  }
}

async function pollCodex() {
  if (!codexLogin.value || pollInFlight) return
  pollInFlight = true
  try {
    const result = await providers.fetchCodexLogin(codexLogin.value.loginId)
    if (!props.modelValue || !codexLogin.value) return
    codexLogin.value.status = result.status
    if (result.status !== 'authenticated' && result.status !== 'failed') return
    clearPolling()
    if (result.status === 'authenticated') {
      await providers.refreshAfterCodexLogin(result.providerId).catch(() => {})
      if (result.providerId) {
        await providers.setProviderPolicy(result.providerId, providerPolicy())
      }
      $q.notify({ type: 'positive', message: 'ChatGPT Codex verbunden' })
      close()
    } else {
      notifyError(result.error, 'Codex-Anmeldung fehlgeschlagen')
      codexLogin.value = null
    }
  } catch {
    clearPolling()
    codexLogin.value = null
  } finally {
    pollInFlight = false
  }
}

async function logoutCodex() {
  const confirmed = await confirm(
    'Die gespeicherte ChatGPT-Verbindung trennen?',
    'Verbindung trennen',
    { okLabel: 'Trennen' }
  )
  if (!confirmed) return
  codexLoading.value = true
  try {
    await providers.logoutCodex()
    $q.notify({ type: 'positive', message: 'ChatGPT Codex getrennt' })
  } finally {
    codexLoading.value = false
  }
}

function clearPolling() {
  window.clearInterval(pollTimer)
  pollTimer = null
}

function providerPolicy() {
  return {
    trustLevel: form.trust_level,
    pseudonymizationEnabled: form.pseudonymization_enabled,
    memoryPolicy: form.memory_policy
  }
}

function close() {
  clearPolling()
  emit('update:modelValue', false)
}

function handleHide() {
  clearPolling()
  emit('update:modelValue', false)
}

watch(() => props.modelValue, (open) => {
  if (open) initialize()
})
watch(providerType, (type, previousType) => {
  if (!props.provider && type !== previousType) Object.assign(form, defaultForm(type))
})
onUnmounted(clearPolling)
</script>
