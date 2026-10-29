<template>
  <q-dialog
    :transition-duration="0"
    :model-value="modelValue"
    :maximized="$q.screen.lt.sm"
    :persistent="saving || pairingLoading"
    @hide="close"
  >
    <q-card class="integration-dialog app-surface column">
      <q-card-section class="row items-start no-wrap q-gutter-md">
        <q-avatar color="primary" text-color="white" :icon="dialogIcon" />
        <div class="col">
          <div class="text-h6">{{ endpoint ? `${endpoint.name} bearbeiten` : 'Kanal hinzufügen' }}</div>
          <div class="text-body2 app-muted">Wähle einen Anbieter und konfiguriere deine Kanalinstanz.</div>
        </div>
        <q-btn
          flat round dense icon="close" aria-label="Dialog schließen"
          :disable="saving || pairingLoading"
          @click="close"
        />
      </q-card-section>

      <q-separator />
      <q-form ref="formRef" class="col column no-wrap" @submit.prevent="save">
        <q-card-section class="col scroll q-gutter-md">
          <AppSelect dense
            v-model="form.provider"
            outlined
            emit-value
            map-options
            label="Anbieter"
            :options="providerOptions"
            :disable="Boolean(endpoint) || createdEndpointId !== null"
            lazy-rules
            :rules="[requiredRule]"
            @update:model-value="selectProvider"
          />
          <template v-if="form.provider === 'telegram'">
          <q-input dense
            v-model="form.name"
            outlined
            label="Name"
            maxlength="160"
            lazy-rules
            :disable="createdEndpointId !== null"
            :rules="[requiredRule]"
          />
          <q-input dense
            v-model="form.token"
            outlined
            type="password"
            :label="endpoint ? 'Neuer Bot-Token (optional)' : 'Bot-Token'"
            :hint="endpoint ? 'Leer lassen, um den gespeicherten Bot-Token beizubehalten.' : 'Bot-Token von BotFather.'"
            lazy-rules
            :disable="createdEndpointId !== null"
            :rules="endpoint ? [] : [requiredRule]"
          />
          <q-toggle dense
            v-model="form.enabled"
            color="primary"
            label="Kanal aktivieren"
            :disable="createdEndpointId !== null"
          />

          <q-banner v-if="pairing" rounded class="app-surface-tonal" role="status" aria-live="polite">
            <div class="row items-center q-gutter-sm q-mb-sm">
              <div class="text-subtitle2 text-weight-medium">Telegram koppeln</div>
              <q-chip dense :color="pairingColor" :text-color="pairing.pairingState === 'pending' ? 'black' : 'white'">
                {{ pairingLabel }}
              </q-chip>
            </div>
            <template v-if="pairing.pairingCode">
              <q-input dense :model-value="pairing.pairingCode" outlined readonly label="Kopplungscode">
                <template #append>
                  <q-btn flat round dense icon="content_copy" aria-label="Kopplungscode kopieren" @click="copyPairingCode" />
                </template>
              </q-input>
              <div v-if="pairing.pairingExpiresAt" class="text-caption q-mt-sm">
                Gültig bis {{ formatAbsoluteDateTime(pairing.pairingExpiresAt) }}
              </div>
              <q-btn
                v-if="telegramLink"
                class="q-mt-md"
                outline
                no-caps
                color="primary"
                icon="send"
                label="Telegram öffnen"
                :href="telegramLink"
                target="_blank"
                rel="noopener noreferrer"
              />
            </template>
            <div v-else-if="pairing.pairingState === 'paired'" class="text-body2">
              Verbunden<template v-if="pairing.pairedDisplayName"> mit {{ pairing.pairedDisplayName }}</template>.
            </div>
            <div v-else class="text-body2">Starte eine neue Kopplung, um diesen Bot zu verbinden.</div>
          </q-banner>

          <q-btn
            v-if="endpointId"
            outline
            no-caps
            color="primary"
            icon="link"
            label="Neue Kopplung starten"
            :loading="pairingLoading"
            @click="startPairing"
          />
          </template>
        </q-card-section>

        <q-separator />
        <q-card-actions class="q-pa-md">
          <q-btn
            flat no-caps :label="createdEndpointId ? 'Schließen' : 'Abbrechen'"
            :disable="saving || pairingLoading"
            @click="close"
          />
          <q-space />
          <q-btn
            v-if="!createdEndpointId && !pairing?.pairingCode"
            unelevated
            no-caps
            color="primary"
            type="submit"
            :label="endpoint ? 'Änderungen speichern' : 'Kanal hinzufügen'"
            :loading="saving"
          />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { copyToClipboard, useQuasar } from 'quasar'
import { formatAbsoluteDateTime } from 'src/utils/date-time'
import { useChannelStore } from 'stores/channels'

defineOptions({ name: 'ChannelConfigDialog' })

const props = defineProps({
  modelValue: { type: Boolean, required: true },
  endpoint: { type: Object, default: null }
})
const emit = defineEmits(['update:modelValue'])

const $q = useQuasar()
const channels = useChannelStore()
const formRef = ref(null)
const form = reactive({ provider: null, name: '', token: '', enabled: true })
const saving = ref(false)
const pairingLoading = ref(false)
const pairing = ref(null)
const createdEndpointId = ref(null)
let pairingTimer = null

const providerOptions = computed(() => channels.providers
  .filter((provider) => provider.allowed || provider.provider === props.endpoint?.provider)
  .map((provider) => ({ label: provider.label, value: provider.provider })))
const dialogIcon = computed(() => form.provider === 'telegram' ? 'send' : 'forum')
const endpointId = computed(() => props.endpoint?.id || createdEndpointId.value)
const telegramLink = computed(() => {
  const username = pairing.value?.externalHandle
  return username && pairing.value?.pairingCode
    ? `https://t.me/${username}?start=${encodeURIComponent(pairing.value.pairingCode)}`
    : null
})
const pairingLabel = computed(() => ({
  paired: 'Verbunden',
  pending: 'Kopplung ausstehend',
  unpaired: 'Nicht gekoppelt'
})[pairing.value?.pairingState] || 'Nicht gekoppelt')
const pairingColor = computed(() => ({
  paired: 'positive',
  pending: 'warning',
  unpaired: 'grey-7'
})[pairing.value?.pairingState] || 'grey-7')

function requiredRule(value) {
  return Boolean(String(value || '').trim()) || 'Pflichtfeld'
}

function selectProvider(provider) {
  form.name = channels.providers.find((item) => item.provider === provider)?.label || ''
}

function stopPolling() {
  if (pairingTimer) window.clearInterval(pairingTimer)
  pairingTimer = null
}

function pairingExpired() {
  const expiresAt = Date.parse(pairing.value?.pairingExpiresAt || '')
  return Number.isFinite(expiresAt) && expiresAt <= Date.now()
}

function pollPairing(id) {
  stopPolling()
  pairingTimer = window.setInterval(async () => {
    try {
      await channels.fetchEndpoints({ suppressErrorToast: true })
      const endpoint = channels.endpoints.find((item) => item.id === id)
      if (!endpoint) return
      const pending = endpoint.pairingState === 'pending' && !pairingExpired()
      pairing.value = {
        ...pairing.value,
        ...endpoint,
        pairingState: endpoint.pairingState === 'pending' && !pending ? 'unpaired' : endpoint.pairingState,
        pairingCode: pending ? pairing.value?.pairingCode : null
      }
      if (!pending) {
        stopPolling()
        if (endpoint.pairingState === 'paired') {
          $q.notify({ type: 'positive', message: 'Telegram-Kanal verbunden' })
        }
      }
    } catch {
      // A failed background check must not interrupt the active pairing flow.
      if (pairingExpired()) {
        pairing.value = { ...pairing.value, pairingState: 'unpaired', pairingCode: null }
        stopPolling()
      }
    }
  }, 2000)
}

async function save() {
  if (!await formRef.value?.validate()) return
  if (form.provider !== 'telegram') {
    $q.notify({ type: 'warning', message: 'Dieser Kanalanbieter wird noch nicht unterstützt.' })
    return
  }
  saving.value = true
  try {
    const payload = { name: form.name.trim(), token: form.token, enabled: form.enabled }
    if (props.endpoint) {
      await channels.updateTelegram(props.endpoint.id, payload)
      $q.notify({ type: 'positive', message: 'Kanal gespeichert' })
      close()
      return
    }
    const result = await channels.createTelegram(payload)
    createdEndpointId.value = result.id
    pairing.value = result
    $q.notify({ type: 'positive', message: 'Kanal hinzugefügt' })
    if (result.pairingCode) pollPairing(result.id)
    else close()
  } catch {
    // The central Axios interceptor displays the error.
  } finally {
    saving.value = false
  }
}

async function startPairing() {
  if (!endpointId.value || pairingLoading.value) return
  pairingLoading.value = true
  try {
    pairing.value = await channels.startTelegramPairing(endpointId.value)
    $q.notify({ type: 'positive', message: 'Neue Telegram-Kopplung gestartet' })
    pollPairing(endpointId.value)
  } catch {
    // The central Axios interceptor displays the error.
  } finally {
    pairingLoading.value = false
  }
}

async function copyPairingCode() {
  if (!pairing.value?.pairingCode) return
  try {
    await copyToClipboard(pairing.value.pairingCode)
    $q.notify({ type: 'positive', message: 'Kopplungscode kopiert' })
  } catch {
    $q.notify({ type: 'negative', message: 'Kopplungscode konnte nicht kopiert werden' })
  }
}

function close() {
  stopPolling()
  emit('update:modelValue', false)
}

watch([() => props.modelValue, () => props.endpoint?.id], ([open]) => {
  stopPolling()
  pairing.value = null
  createdEndpointId.value = null
  if (!open) return
  Object.assign(form, {
    provider: props.endpoint?.provider || null,
    name: props.endpoint?.name || '',
    token: '',
    enabled: props.endpoint?.enabled ?? true
  })
  pairing.value = props.endpoint ? { ...props.endpoint } : null
  if (pairing.value?.pairingCode && props.endpoint?.id) pollPairing(props.endpoint.id)
})

onBeforeUnmount(stopPolling)
</script>
