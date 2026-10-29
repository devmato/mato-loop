<template>
  <section class="column full-height no-wrap" aria-label="OpenCode">
    <AppStateMessage
      v-if="!project"
      class="col q-pa-xl"
      icon="code_off"
      title="Kein Projekt ausgewählt"
      description="Öffne eine Session in einem Projekt, um OpenCode zu verbinden."
    />
    <AppStateMessage
      v-else-if="!bound"
      class="col q-pa-xl"
      icon="link"
      icon-color="primary"
      title="OpenCode mit diesem Projekt verbinden"
      description="Wähle einen OpenCode-Server und das zugehörige Arbeitsverzeichnis."
    >
      <template #actions>
        <q-btn unelevated no-caps color="primary" icon="link" label="OpenCode verbinden" @click="openBindingDialog" />
      </template>
    </AppStateMessage>
    <AppStateMessage
      v-else-if="coding.loadFailed && !coding.loaded"
      class="col q-pa-xl"
      icon="cloud_off"
      title="Coding-Daten konnten nicht geladen werden"
    >
      <template #actions>
        <q-btn outline no-caps color="primary" label="Erneut laden" @click="refresh" />
        <q-btn flat no-caps color="primary" label="Zuordnung ändern" @click="openBindingDialog" />
        <q-btn flat no-caps color="negative" label="Trennen" :loading="bindingSaving" @click="clearBinding" />
      </template>
    </AppStateMessage>
    <template v-else>
      <q-banner dense class="app-surface-tonal">
        <template #avatar><q-icon name="code" color="primary" /></template>
        <div class="text-weight-medium">Arbeitsverzeichnis {{ project.codingService.workspaceKey }}</div>
        <div v-if="bindingMutable" class="text-caption app-muted">
          Die Zuordnung kann bis zum ersten gespeicherten Plan geändert werden.
        </div>
        <div v-else-if="coding.loaded" class="text-caption app-muted">
          Die Zuordnung ist gesperrt, da bereits ein Plan gespeichert wurde.
        </div>
        <template #action>
          <q-btn flat round dense icon="refresh" aria-label="Coding-Daten aktualisieren" :loading="coding.loading" @click="refresh">
            <q-tooltip>Coding-Daten aktualisieren</q-tooltip>
          </q-btn>
          <template v-if="bindingMutable">
            <q-btn flat dense no-caps color="primary" label="Ändern" @click="openBindingDialog" />
            <q-btn
              flat dense no-caps color="negative" label="Trennen"
              :loading="bindingSaving"
              @click="clearBinding"
            />
          </template>
        </template>
      </q-banner>
      <q-separator />
      <q-expansion-item :duration="0" icon="tune" label="Modelle" header-class="text-weight-medium">
        <q-card-section class="row q-col-gutter-sm q-pt-none">
          <div class="col-12 col-sm-6">
            <AppSelect
              :model-value="analysisModel"
              outlined dense emit-value map-options
              label="Analysemodell"
              :options="modelOptions"
              :disable="savingModels"
              @update:model-value="updateAnalysisModel"
            />
            <AppSelect
              v-if="analysisVariantVisible"
              :model-value="analysisVariant"
              outlined dense emit-value map-options class="q-mt-sm"
              label="Denken (Analyse)"
              :options="analysisVariantOptions"
              :disable="savingModels"
              @update:model-value="updateAnalysisVariant"
            />
          </div>
          <div class="col-12 col-sm-6">
            <AppSelect
              :model-value="executionModel"
              outlined dense emit-value map-options
              label="Ausführungsmodell"
              :options="modelOptions"
              :disable="savingModels"
              @update:model-value="updateExecutionModel"
            />
            <AppSelect
              v-if="executionVariantVisible"
              :model-value="executionVariant"
              outlined dense emit-value map-options class="q-mt-sm"
              label="Denken (Ausführung)"
              :options="executionVariantOptions"
              :disable="savingModels"
              @update:model-value="updateExecutionVariant"
            />
          </div>
          <div class="col-12">
            <q-btn
              unelevated no-caps color="primary" label="Speichern" class="full-width"
              :disable="!modelSelectionDirty || !modelSelectionValid"
              :loading="savingModels"
              @click="saveModels"
            />
          </div>
        </q-card-section>
      </q-expansion-item>
      <q-separator />

      <q-tabs v-model="tab" dense no-caps align="left" active-color="primary" indicator-color="primary">
        <q-tab name="files" icon="folder_open" label="Dateien" />
        <q-tab name="plans" icon="description" label="Pläne" />
        <q-tab name="executions" icon="playlist_play" label="Ausführungen" />
      </q-tabs>
      <q-separator />

      <q-tab-panels v-model="tab" class="col bg-transparent">
        <q-tab-panel name="files" class="q-pa-none column no-wrap">
          <template v-if="coding.content">
            <q-toolbar>
              <q-btn flat round dense icon="arrow_back" aria-label="Zurück zur Dateiliste" @click="coding.closeFile()" />
              <q-toolbar-title class="text-subtitle2 ellipsis">{{ coding.content.path }}</q-toolbar-title>
            </q-toolbar>
            <q-separator />
            <q-scroll-area class="col">
              <pre v-if="coding.content.type === 'text'" class="coding-text q-ma-none q-pa-md">{{ coding.content.content }}</pre>
              <div v-else-if="binaryImage" class="q-pa-md"><q-img :src="binaryImage" fit="contain" /></div>
              <AppStateMessage v-else class="q-pa-xl" icon="insert_drive_file" description="Für diese Binärdatei ist keine Vorschau verfügbar." />
            </q-scroll-area>
          </template>
          <template v-else>
            <q-toolbar>
              <q-btn
                flat round dense icon="arrow_upward" aria-label="Übergeordneten Ordner öffnen"
                :disable="coding.currentPath === '.'"
                @click="coding.loadFiles(parentPath(coding.currentPath))"
              />
              <q-toolbar-title class="text-subtitle2 ellipsis">{{ coding.currentPath }}</q-toolbar-title>
            </q-toolbar>
            <q-separator />
            <q-scroll-area class="col">
              <q-list separator>
                <q-item v-if="!coding.files.length && !coding.loading" class="q-pa-lg">
                  <q-item-section class="text-center app-muted">Dieser Ordner ist leer.</q-item-section>
                </q-item>
                <q-item
                  v-for="entry in coding.files"
                  :key="entry.path"
                  clickable v-ripple
                  @click="entry.type === 'directory' ? coding.loadFiles(entry.path) : coding.readFile(entry.path)"
                >
                  <q-item-section avatar><q-icon :name="entry.type === 'directory' ? 'folder' : 'description'" /></q-item-section>
                  <q-item-section><q-item-label lines="1">{{ entry.name }}</q-item-label></q-item-section>
                  <q-item-section side>
                    <div class="row q-gutter-xs">
                      <q-chip v-if="entry.ignored" dense outline color="grey-7">Ignoriert</q-chip>
                      <q-chip v-if="statusByPath.get(entry.path)" dense :color="statusColor(statusByPath.get(entry.path).status)" :text-color="statusTextColor(statusByPath.get(entry.path).status)">
                        {{ statusLabel(statusByPath.get(entry.path).status) }}
                      </q-chip>
                    </div>
                  </q-item-section>
                </q-item>
                <q-item v-for="entry in deletedStatuses" :key="`deleted:${entry.path}`">
                  <q-item-section avatar><q-icon name="delete_outline" /></q-item-section>
                  <q-item-section><q-item-label lines="1">{{ fileName(entry.path) }}</q-item-label></q-item-section>
                  <q-item-section side>
                    <q-chip dense color="negative" text-color="white">Gelöscht</q-chip>
                  </q-item-section>
                </q-item>
              </q-list>
            </q-scroll-area>
          </template>
        </q-tab-panel>

        <q-tab-panel name="plans" class="q-pa-none column no-wrap">
          <q-toolbar>
            <q-toolbar-title class="text-subtitle2">Gespeicherte Pläne</q-toolbar-title>
            <q-btn
              unelevated no-caps color="primary" icon="playlist_add" label="Ausgewählte einreihen"
              :disable="!selectedPlans.length"
              :loading="enqueueing"
              @click="enqueue"
            />
          </q-toolbar>
          <q-separator />
          <q-scroll-area class="col">
            <q-list separator>
              <q-item v-if="!coding.plans.length" class="q-pa-lg">
                <q-item-section class="text-center app-muted">Noch keine Pläne gespeichert. Erstelle sie im Projektchat.</q-item-section>
              </q-item>
              <q-expansion-item v-for="plan in coding.plans" :key="plan.id" :duration="0" group="coding-plans">
                <template #header>
                  <q-item-section avatar>
                    <q-checkbox dense v-model="selectedPlans" :val="plan.id" :aria-label="`Plan ${plan.title} auswählen`" @click.stop />
                  </q-item-section>
                  <q-item-section>
                    <q-item-label>{{ plan.title }}</q-item-label>
                    <q-item-label caption>{{ formatAbsoluteDateTime(plan.createdAt) }}</q-item-label>
                  </q-item-section>
                </template>
                <q-card-section><MessageContent :content="plan.markdown" :allow-images="false" :interactive="false" /></q-card-section>
              </q-expansion-item>
            </q-list>
          </q-scroll-area>
        </q-tab-panel>

        <q-tab-panel name="executions" class="q-pa-none column no-wrap">
          <q-banner v-if="coding.queuePauseReason" dense class="app-surface-tonal">
            Warteschlange pausiert: {{ pauseReasonLabel(coding.queuePauseReason) }}
          </q-banner>
          <q-toolbar>
            <q-toolbar-title class="text-subtitle2">FIFO-Warteschlange</q-toolbar-title>
            <q-btn
              flat no-caps :icon="coding.queuePauseReason ? 'play_arrow' : 'pause'"
              :label="coding.queuePauseReason ? 'Fortsetzen' : 'Pausieren'"
              :loading="queueChanging"
              @click="toggleQueue"
            />
          </q-toolbar>
          <q-separator />
          <q-scroll-area class="col">
            <q-list separator>
              <q-item v-if="!coding.executions.length" class="q-pa-lg">
                <q-item-section class="text-center app-muted">Die Warteschlange ist leer.</q-item-section>
              </q-item>
              <q-item v-for="execution in coding.executions" :key="execution.id" class="q-py-md">
                <q-item-section>
                  <div class="row items-center q-gutter-sm">
                    <q-item-label class="text-weight-medium">Position {{ execution.queuePosition }}</q-item-label>
                    <q-chip dense :color="executionColor(execution.status)" :text-color="executionTextColor(execution.status)">{{ executionLabel(execution.status) }}</q-chip>
                  </div>
                  <q-item-label caption lines="1">Plan {{ execution.planId }} · {{ execution.modelKey }} · {{ execution.modelVariant || 'OpenCode-Standard' }}</q-item-label>
                  <q-item-label v-if="execution.resultSummary" class="q-mt-sm" lines="3">{{ execution.resultSummary }}</q-item-label>
                  <q-item-label v-if="execution.errorCode" class="text-negative q-mt-sm">{{ apiErrorMessage({ error: { code: execution.errorCode } }) }}</q-item-label>
                  <q-banner v-if="execution.pendingPermission" dense rounded class="app-surface-tonal q-mt-sm">
                    <div class="text-weight-medium">{{ execution.pendingPermission.title }}</div>
                    <div class="text-caption coding-patterns">{{ execution.pendingPermission.patterns.join(', ') }}</div>
                    <div class="row q-gutter-sm q-mt-sm">
                      <q-btn dense unelevated no-caps color="primary" label="Einmal erlauben" :loading="permissionReplyingId === execution.id" :disable="permissionReplyingId === execution.id" @click="reply(execution.id, 'once')" />
                      <q-btn dense outline no-caps color="primary" label="Immer erlauben" :disable="permissionReplyingId === execution.id" @click="reply(execution.id, 'always')" />
                      <q-btn dense flat no-caps color="negative" label="Ablehnen" :disable="permissionReplyingId === execution.id" @click="reply(execution.id, 'reject')" />
                    </div>
                  </q-banner>
                </q-item-section>
                <q-item-section v-if="cancellable(execution.status)" side top>
                  <q-btn flat round dense color="negative" icon="cancel" aria-label="Ausführung abbrechen" @click="cancel(execution)" />
                </q-item-section>
              </q-item>
            </q-list>
          </q-scroll-area>
        </q-tab-panel>
      </q-tab-panels>
      <q-inner-loading :showing="coding.loading && !coding.loaded" />
    </template>

    <q-dialog v-model="bindingDialogOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="bindingSaving">
      <q-card class="integration-dialog app-surface column">
        <q-form class="col column no-wrap" @submit.prevent="saveBinding">
          <q-card-section>
            <div class="text-h6">{{ bound ? 'OpenCode-Zuordnung ändern' : 'OpenCode verbinden' }}</div>
          </q-card-section>
          <q-card-section class="col scroll q-pt-none">
            <AppSelect dense
              v-model="bindingIntegrationId"
              outlined
              emit-value
              map-options
              label="OpenCode-Server"
              :options="bindingIntegrationOptions"
              :loading="bindingLoading"
              lazy-rules
              :rules="[requiredRule]"
              @update:model-value="loadBindings"
            >
              <template #no-option>
                <q-item><q-item-section class="app-muted">Keine aktive OpenCode-Integration verfügbar</q-item-section></q-item>
              </template>
            </AppSelect>
            <AppSelect dense
              v-model="bindingIndex"
              outlined
              emit-value
              map-options
              label="OpenCode-Projekt und Arbeitsverzeichnis"
              :options="bindingOptions"
              :loading="bindingsLoading"
              :disable="!bindingIntegrationId"
              lazy-rules
              :rules="[requiredRule]"
            >
              <template #no-option>
                <q-item><q-item-section class="app-muted">Keine Projekte gefunden</q-item-section></q-item>
              </template>
            </AppSelect>
            <div v-if="bound" class="text-caption app-muted">
              Beim Wechsel wird die bisherige Modellauswahl zurückgesetzt.
            </div>
          </q-card-section>
          <q-card-actions align="right" class="q-pa-md q-pt-none">
            <q-btn flat no-caps label="Abbrechen" :disable="bindingSaving" v-close-popup />
            <q-btn
              unelevated no-caps color="primary" label="Speichern" type="submit"
              :loading="bindingSaving"
            />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import MessageContent from 'src/features/chat/components/MessageContent.vue'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import { useConfirm } from 'src/composables/confirm'
import { parentPath } from 'src/features/files/state'
import { apiErrorMessage } from 'src/utils/api-errors'
import { formatAbsoluteDateTime } from 'src/utils/date-time'
import { useChatStore } from 'src/features/chat/store'
import { useControlCenterStore } from 'stores/control-center'
import { useIntegrationStore } from 'stores/integrations'
import { useProjectCodingStore } from 'stores/project-coding'
import { useProjectStore } from 'stores/projects'

defineOptions({ name: 'CodingApp' })
const $q = useQuasar()
const { confirm } = useConfirm()
const chat = useChatStore()
const controlCenter = useControlCenterStore()
const integrations = useIntegrationStore()
const projects = useProjectStore()
const coding = useProjectCodingStore()
const tab = ref('files')
const analysisModel = ref(null)
const analysisVariant = ref(null)
const executionModel = ref(null)
const executionVariant = ref(null)
const modelSelectionDirty = ref(false)
const savingModels = ref(false)
const enqueueing = ref(false)
const queueChanging = ref(false)
const selectedPlans = ref([])
const permissionReplyingId = ref(null)
const bindingDialogOpen = ref(false)
const bindingLoading = ref(false)
const bindingsLoading = ref(false)
const bindingSaving = ref(false)
const bindingIntegrationId = ref(null)
const bindingIndex = ref(null)
const bindingIntegrations = ref([])
const bindings = ref([])
let bindingsLoadVersion = 0
let pollTimer = null

const projectId = computed(() => (chat.current ? chat.current.projectId : chat.sessionsProjectId) || null)
const project = computed(() => projects.items.find((item) => item.id === projectId.value))
const bound = computed(() => project.value?.codingService?.adapterType === 'opencode')
const bindingKey = computed(() => bound.value
  ? `${project.value.codingService.integrationId}:${project.value.codingService.projectKey}:${project.value.codingService.workspaceKey}`
  : null)
const bindingMutable = computed(() => coding.loaded && !coding.loadFailed && coding.plans.length === 0)
const bindingIntegrationOptions = computed(() => bindingIntegrations.value.map((item) => ({
  label: item.name,
  value: item.id
})))
const bindingOptions = computed(() => bindings.value.map((item, index) => ({
  label: `${item.label} · ${item.workspaceKey}`,
  value: String(index)
})))
const modelOptions = computed(() => coding.models.map((model) => ({
  label: `${model.providerLabel} · ${model.label}`,
  value: model.key
})))
const analysisModelConfig = computed(() => coding.models.find((model) => model.key === analysisModel.value))
const executionModelConfig = computed(() => coding.models.find((model) => model.key === executionModel.value))
const analysisVariantOptions = computed(() => variantOptions(analysisModelConfig.value, analysisVariant.value))
const executionVariantOptions = computed(() => variantOptions(executionModelConfig.value, executionVariant.value))
const analysisVariantVisible = computed(() => modelVariants(analysisModelConfig.value).length > 0 || analysisVariant.value !== null)
const executionVariantVisible = computed(() => modelVariants(executionModelConfig.value).length > 0 || executionVariant.value !== null)
const modelSelectionValid = computed(() => (
  analysisModelConfig.value
  && executionModelConfig.value
  && variantAvailable(analysisModelConfig.value, analysisVariant.value)
  && variantAvailable(executionModelConfig.value, executionVariant.value)
))
const statusByPath = computed(() => new Map(coding.statuses.map((item) => [item.path, item])))
const deletedStatuses = computed(() => coding.statuses.filter((item) => (
  item.status === 'deleted'
  && parentPath(item.path) === coding.currentPath
  && !coding.files.some((file) => file.path === item.path)
)))
const binaryImage = computed(() => (
  coding.content?.type === 'binary' && coding.content.mimeType?.startsWith('image/')
    ? `data:${coding.content.mimeType};base64,${coding.content.content}`
    : null
))

function statusLabel(status) {
  return { added: 'Neu', deleted: 'Gelöscht', modified: 'Geändert' }[status] || status
}

function statusColor(status) {
  return { added: 'positive', deleted: 'negative', modified: 'warning' }[status] || 'grey-7'
}

function statusTextColor(status) {
  return status === 'modified' ? 'dark' : 'white'
}

function fileName(path) {
  return path.split('/').at(-1)
}

function executionLabel(status) {
  return {
    queued: 'Vorgemerkt', running: 'Wird ausgeführt', waiting_permission: 'Freigabe erforderlich',
    completed: 'Abgeschlossen', failed: 'Fehlgeschlagen', cancelled: 'Abgebrochen',
    outcome_unknown: 'Ausgang unbekannt'
  }[status] || status
}

function executionColor(status) {
  return { completed: 'positive', failed: 'negative', cancelled: 'grey-7', outcome_unknown: 'warning' }[status] || 'primary'
}

function executionTextColor(status) {
  return status === 'outcome_unknown' ? 'dark' : 'white'
}

function pauseReasonLabel(reason) {
  return {
    manual: 'manuell', binding_changed: 'Zuordnung geändert', model_unavailable: 'Modell nicht verfügbar',
    external_activity: 'externe OpenCode-Aktivität', execution_failed: 'Ausführung fehlgeschlagen',
    outcome_unknown: 'Ausgang unbekannt'
  }[reason] || reason
}

function cancellable(status) {
  return ['queued', 'running', 'waiting_permission'].includes(status)
}

function requiredRule(value) {
  return Boolean(String(value || '').trim()) || 'Pflichtfeld'
}

async function loadBindings(integrationId, selectCurrent = false) {
  const loadVersion = ++bindingsLoadVersion
  bindingIndex.value = null
  bindings.value = []
  if (!integrationId) return
  bindingsLoading.value = true
  try {
    const items = await projects.listCodingBindings(integrationId)
    if (loadVersion !== bindingsLoadVersion || integrationId !== bindingIntegrationId.value) return
    bindings.value = items
    if (selectCurrent && bound.value) {
      const index = items.findIndex((item) => (
        item.projectKey === project.value.codingService.projectKey
        && item.workspaceKey === project.value.codingService.workspaceKey
      ))
      bindingIndex.value = index >= 0 ? String(index) : null
    }
  } catch {
    // The central API interceptor displays the error; the selector remains empty.
  } finally {
    if (loadVersion === bindingsLoadVersion) bindingsLoading.value = false
  }
}

async function openBindingDialog() {
  bindingDialogOpen.value = true
  bindingLoading.value = true
  bindingIntegrationId.value = null
  bindingIndex.value = null
  bindingIntegrations.value = []
  bindings.value = []
  try {
    const result = await integrations.fetchCoding({ personal: true })
    bindingIntegrations.value = result.instances.filter((item) => item.enabled)
    const currentId = bound.value ? project.value.codingService.integrationId : null
    let preferredId = bindingIntegrations.value.some((item) => item.id === currentId) ? currentId : null
    if (!preferredId && bindingIntegrations.value.length === 1) preferredId = bindingIntegrations.value[0].id
    bindingIntegrationId.value = preferredId
    if (preferredId) await loadBindings(preferredId, true)
  } catch {
    bindingIntegrations.value = []
  } finally {
    bindingLoading.value = false
  }
}

async function saveBinding() {
  if (bindingIndex.value === null) return
  const binding = bindings.value[Number(bindingIndex.value)]
  if (!project.value || !bindingIntegrationId.value || !binding) return
  const changed = bound.value && bindingKey.value !== (
    `${bindingIntegrationId.value}:${binding.projectKey}:${binding.workspaceKey}`
  )
  if (changed && !await confirm(
    'Die OpenCode-Zuordnung und die gespeicherte Modellauswahl werden geändert.',
    'OpenCode-Zuordnung ändern?',
    { okLabel: 'Ändern' }
  )) return
  bindingSaving.value = true
  try {
    await projects.setCodingBinding(project.value.id, {
      integrationId: bindingIntegrationId.value,
      projectKey: binding.projectKey,
      workspaceKey: binding.workspaceKey
    })
    bindingDialogOpen.value = false
    $q.notify({ type: 'positive', message: changed ? 'OpenCode-Zuordnung geändert.' : 'OpenCode verbunden.' })
  } catch {
    // The central API interceptor displays the error; the dialog remains open.
  } finally {
    bindingSaving.value = false
  }
}

async function clearBinding() {
  if (!project.value || !await confirm(
    'OpenCode wird von diesem Projekt getrennt. Die gespeicherte Modellauswahl wird entfernt.',
    'OpenCode-Verbindung trennen?',
    { okLabel: 'Trennen', okColor: 'negative' }
  )) return
  bindingSaving.value = true
  try {
    await projects.clearCodingBinding(project.value.id)
    $q.notify({ type: 'positive', message: 'OpenCode-Verbindung getrennt.' })
  } catch {
    // The central API interceptor displays the error.
  } finally {
    bindingSaving.value = false
  }
}

async function refresh(quiet = false) {
  if (!bound.value || coding.loading) return
  try {
    await coding.refresh({ quiet })
  } catch {
    // Direct requests use the central toast; background polling stays quiet.
  }
}

async function saveModels() {
  savingModels.value = true
  try {
    await coding.saveModels(
      analysisModel.value,
      analysisVariant.value,
      executionModel.value,
      executionVariant.value
    )
    modelSelectionDirty.value = false
    $q.notify({ type: 'positive', message: 'Modellauswahl gespeichert.' })
  } catch {
    // The central API interceptor displays the error.
  } finally {
    savingModels.value = false
  }
}

function modelVariants(model) {
  return Array.isArray(model?.variants) ? model.variants : []
}

function variantOptions(model, selected) {
  const variants = modelVariants(model)
  return [
    { label: 'OpenCode-Standard', value: null },
    ...variants.map((value) => ({ label: value, value })),
    ...(selected !== null && !variants.includes(selected)
      ? [{ label: `Nicht verfügbar · ${selected}`, value: selected, disable: true }]
      : [])
  ]
}

function variantAvailable(model, selected) {
  return selected === null || modelVariants(model).includes(selected)
}

function updateAnalysisModel(value) {
  analysisModel.value = value
  analysisVariant.value = null
  modelSelectionDirty.value = true
}

function updateAnalysisVariant(value) {
  analysisVariant.value = value
  modelSelectionDirty.value = true
}

function updateExecutionModel(value) {
  executionModel.value = value
  executionVariant.value = null
  modelSelectionDirty.value = true
}

function updateExecutionVariant(value) {
  executionVariant.value = value
  modelSelectionDirty.value = true
}

async function enqueue() {
  enqueueing.value = true
  try {
    await coding.enqueue(selectedPlans.value)
    selectedPlans.value = []
    tab.value = 'executions'
    $q.notify({ type: 'positive', message: 'Pläne wurden eingereiht.' })
  } catch {
    // The central API interceptor displays the error.
  } finally {
    enqueueing.value = false
  }
}

async function toggleQueue() {
  queueChanging.value = true
  try {
    if (coding.queuePauseReason) await coding.resumeQueue()
    else await coding.pauseQueue()
  } catch {
    // The central API interceptor displays the error.
  } finally {
    queueChanging.value = false
  }
}

async function cancel(execution) {
  if (!await confirm('Diese Ausführung abbrechen?', 'Ausführung abbrechen?', { okLabel: 'Abbrechen', okColor: 'negative' })) return
  try {
    await coding.cancelExecution(execution.id)
  } catch {
    // The central API interceptor displays the error.
  }
}

async function reply(executionId, response) {
  if (permissionReplyingId.value) return
  permissionReplyingId.value = executionId
  try {
    await coding.replyPermission(executionId, response)
  } catch {
    // The central API interceptor displays the error.
  } finally {
    permissionReplyingId.value = null
  }
}

function updatePolling() {
  clearInterval(pollTimer)
  pollTimer = null
  if (controlCenter.open && controlCenter.activeContentId === 'coding' && bound.value) {
    pollTimer = setInterval(() => void refresh(true), 5000)
  }
}

watch([projectId, bindingKey], async ([value, key]) => {
  modelSelectionDirty.value = false
  coding.resetProject(value)
  selectedPlans.value = []
  if (key) await refresh()
  updatePolling()
}, { immediate: true })
watch(() => [
  coding.analysisModelKey,
  coding.analysisModelVariant,
  coding.executionModelKey,
  coding.executionModelVariant
], ([analysis, analysisSelectedVariant, execution, executionSelectedVariant]) => {
  if (modelSelectionDirty.value) return
  analysisModel.value = analysis
  analysisVariant.value = analysisSelectedVariant
  executionModel.value = execution
  executionVariant.value = executionSelectedVariant
}, { immediate: true })
watch(() => [controlCenter.open, controlCenter.activeContentId, bound.value], updatePolling)
onBeforeUnmount(() => clearInterval(pollTimer))
</script>

<style scoped>
.coding-text {
  font-family: monospace;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.coding-patterns {
  overflow-wrap: anywhere;
}
</style>
