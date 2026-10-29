<template>
  <q-dialog
    :transition-duration="0"
    :model-value="modelValue"
    :maximized="$q.screen.lt.sm"
    :persistent="saving"
    @hide="close"
  >
    <q-card class="provider-dialog model-dialog app-surface">
      <q-form class="column full-height" @submit.prevent="save">
        <q-card-section class="row items-start no-wrap q-gutter-md">
          <q-avatar color="primary" text-color="white" icon="smart_toy" />
          <div class="col">
            <div class="text-h6">{{ title }}</div>
            <div class="text-body2 app-muted">
              {{ model ? 'Anzeigename, Status und Laufzeit bearbeiten.' : `Manuelles Modell für ${provider?.name || 'den Provider'} hinzufügen.` }}
            </div>
          </div>
          <q-btn flat round dense icon="close" aria-label="Dialog schließen" :disable="saving" @click="close" />
        </q-card-section>

        <q-separator />

        <q-card-section class="provider-dialog__body col scroll">
          <div class="provider-form-grid">
            <q-input dense
              v-model="form.modelId"
              outlined
              hide-bottom-space
              label="Modell-ID"
              lazy-rules
              :readonly="isDiscovered"
              :rules="[requiredRule]"
              :hint="isDiscovered ? 'Die ID eines entdeckten Modells wird vom Provider vorgegeben.' : ''"
            />
            <q-input dense
              v-model="form.name"
              outlined
              hide-bottom-space
              label="Anzeigename (optional)"
              hint="Ohne Anzeigename wird die Modell-ID verwendet."
            />
            <q-toggle dense
              v-model="form.enabled"
              class="provider-form-section"
              color="primary"
              label="Modell aktiv"
              aria-label="Modell aktiv"
              :disable="model?.availability === 'missing'"
            />
            <AppSelect dense
              v-model="form.capabilities"
              class="provider-form-section"
              outlined
              hide-bottom-space
              multiple
              use-chips
              emit-value
              map-options
              label="Modellfähigkeiten"
              :options="MODEL_CAPABILITY_OPTIONS"
              :readonly="isDiscovered"
              :hint="capabilityHint"
              lazy-rules
              :rules="[capabilityRule]"
            />
            <ModelRuntimeFields
              v-if="supportsChat"
              :model-value="form"
              :generation-options-omitted="provider?.adapterType === 'openai-codex'"
              :reasoning-summary-available="reasoningSummaryAvailable"
              :reasoning-summary-forced="provider?.adapterType === 'openai-codex'"
              :provider-trust-level="provider?.trustLevel"
              :provider-pseudonymization-enabled="provider?.pseudonymizationEnabled"
              :model-trust-enabled="auth.modelTrustEnabled"
            />
            <q-banner v-else rounded class="provider-form-section app-surface-tonal">
              Die LLM-Laufzeitfelder gelten nur für Chatmodelle. Die ausgewählten Medienfähigkeiten bleiben davon unabhängig.
            </q-banner>
          </div>
        </q-card-section>

        <q-separator />
        <q-card-actions class="q-pa-md">
          <q-btn flat no-caps label="Abbrechen" :disable="saving" @click="close" />
          <q-space />
          <q-btn
            unelevated
            no-caps
            color="primary"
            type="submit"
            :label="model ? 'Änderungen speichern' : 'Modell hinzufügen'"
            :loading="saving"
          />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import ModelRuntimeFields from 'src/features/providers/ModelRuntimeFields.vue'
import { modelRuntimeForm, modelRuntimePayload } from 'src/utils/model-runtime'
import { MODEL_CAPABILITY_OPTIONS, modelSupportsCapability } from 'src/utils/provider-models'
import { useProviderStore } from 'stores/providers'
import { useAuthStore } from 'stores/auth'

defineOptions({ name: 'ModelConfigDialog' })

const props = defineProps({
  modelValue: { type: Boolean, required: true },
  provider: { type: Object, default: null },
  model: { type: Object, default: null }
})
const emit = defineEmits(['update:modelValue', 'saved'])
const $q = useQuasar()
const providers = useProviderStore()
const auth = useAuthStore()
const saving = ref(false)
const form = reactive(defaultForm())
const isDiscovered = computed(() => props.model?.origin === 'discovered')
const capabilitiesAreAuthoritative = computed(() => (
  isDiscovered.value && (
    ['lemonade', 'azure-speech'].includes(props.provider?.adapterType)
    || props.model?.discoveryMetadata?.capabilities_authoritative === true
  )
))
const supportsChat = computed(() => modelSupportsCapability({ capabilities: form.capabilities }, 'chat'))
const capabilityHint = computed(() => {
  if (capabilitiesAreAuthoritative.value) return 'Der Provider gibt diese Fähigkeiten bei der Modellerkennung verbindlich vor.'
  if (isDiscovered.value) return 'Die Fähigkeiten wurden bei der Modellerkennung ermittelt.'
  return 'Bestimmt, für welche Aufgaben das Modell auswählbar ist.'
})
const title = computed(() => props.model ? `${props.model.name || props.model.modelId} bearbeiten` : 'Modell hinzufügen')
const reasoningSummaryAvailable = computed(() => props.provider?.adapterType === 'openai-codex'
  || ['responses'].includes(props.provider?.config?.apiType || props.provider?.config?.api_type))
function defaultForm() {
  return {
    modelId: '',
    name: '',
    enabled: true,
    ...modelRuntimeForm()
  }
}

function initialize() {
  Object.assign(form, defaultForm(), props.model ? {
    modelId: props.model.modelId,
    name: props.model.name === props.model.modelId ? '' : props.model.name || '',
    enabled: props.model.enabled,
    ...modelRuntimeForm(props.model)
  } : {})
}

function requiredRule(value) {
  return Boolean(String(value || '').trim()) || 'Pflichtfeld'
}

function capabilityRule(value) {
  return Array.isArray(value) && value.length > 0 || 'Wähle mindestens eine Modellfähigkeit aus.'
}

async function save() {
  if (!props.provider) return
  saving.value = true
  try {
    const runtimePayload = modelRuntimePayload(form)
    if (capabilitiesAreAuthoritative.value) delete runtimePayload.capabilities
    const payload = {
      model_id: form.modelId.trim(),
      name: form.name.trim() || null,
      enabled: form.enabled,
      ...runtimePayload
    }
    if (props.model) await providers.updateModel(props.model.id, payload)
    else await providers.createModel(props.provider.id, payload)
    $q.notify({ type: 'positive', message: props.model ? 'Modell gespeichert' : 'Modell hinzugefügt' })
    emit('saved')
    close()
  } finally {
    saving.value = false
  }
}

function close() {
  emit('update:modelValue', false)
}

watch(() => props.modelValue, (open) => {
  if (open) initialize()
})
</script>
