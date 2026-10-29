<template>
  <ImageViewerPanel
    v-if="directImage"
    :image="directImage"
    :fullscreen="fullscreen"
    :fullscreen-toggle="fullscreenToggle"
    @close="emit('close')"
    @toggle-fullscreen="emit('toggle-fullscreen')"
  />
  <section
    v-else
    ref="viewerRoot"
    class="file-viewer column no-wrap full-height"
    tabindex="-1"
    :aria-label="viewerName"
    @keydown.esc="handleEscape"
  >
    <slot name="toolbar">
      <q-toolbar v-if="!usingTextEditor" class="file-viewer__toolbar q-px-md">
        <q-toolbar-title class="text-subtitle1 text-weight-bold ellipsis">{{ viewerName }}</q-toolbar-title>
        <q-btn-toggle
          v-if="preview.kind === 'openscad' && !historical"
          class="file-viewer__view-toggle"
          :model-value="workspace.editorActive ? 'source' : openScadView"
          unelevated
          no-caps
          toggle-color="primary"
          :options="openScadViewOptions"
          aria-label="OpenSCAD-Ansicht wählen"
          @update:model-value="setOpenScadView"
        />
        <q-btn
          v-if="preview.kind === 'openscad' && !historical && !workspace.editorActive"
          class="file-viewer__export-stl"
          unelevated
          no-caps
          color="primary"
          icon="download"
          :label="$q.screen.gt.xs ? 'STL erzeugen' : undefined"
          :loading="exportingOpenScad"
          :disable="renderingOpenScad"
          aria-label="STL erzeugen und herunterladen"
          @click="exportOpenScad"
        >
          <q-tooltip>STL erzeugen und herunterladen</q-tooltip>
        </q-btn>
        <q-btn
          v-if="preview.kind === 'image' && contentUrl"
          class="file-icon-action"
          flat
          round
          dense
          icon="open_in_new"
          :href="contentUrl"
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Bild in neuem Tab öffnen"
        >
          <q-tooltip>In neuem Tab öffnen</q-tooltip>
        </q-btn>
        <q-btn
          v-if="preview.kind === 'markdown' && !historical"
          class="file-icon-action"
          flat
          round
          dense
          icon="picture_as_pdf"
          :href="printUrl"
          :disable="workspace.editorDirty"
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Markdown als PDF speichern"
        >
          <q-tooltip>{{ workspace.editorDirty ? 'Speichere zuerst die Änderungen.' : 'Als PDF speichern' }}</q-tooltip>
        </q-btn>
        <q-btn
          v-if="textualPreview(preview)"
          class="file-icon-action"
          flat
          round
          dense
          icon="content_copy"
          aria-label="Dateiinhalt kopieren"
          @click="copyPreview(preview)"
        >
          <q-tooltip>Inhalt kopieren</q-tooltip>
        </q-btn>
        <q-btn
          v-if="entry && !historical && workspace.scope === 'session'"
          class="file-icon-action"
          flat
          round
          dense
          icon="history"
          aria-label="Versionsverlauf öffnen"
          :disable="workspace.editorDirty || saving"
          @click="openRevisions"
        >
          <q-tooltip>Versionsverlauf</q-tooltip>
        </q-btn>
        <q-btn v-if="canDownload" class="file-icon-action" flat round dense icon="download" aria-label="Datei herunterladen" @click="download">
          <q-tooltip>Gespeicherte Datei herunterladen</q-tooltip>
        </q-btn>
        <q-btn
          v-if="fullscreen || fullscreenToggle"
          class="file-icon-action"
          flat
          round
          dense
          :icon="fullscreen ? 'fullscreen_exit' : 'fullscreen'"
          :aria-label="fullscreen ? 'Vollbild verlassen' : 'Im Vollbild öffnen'"
          @click="emit('toggle-fullscreen')"
        >
          <q-tooltip>{{ fullscreen ? 'Vollbild verlassen' : 'Im Vollbild öffnen' }}</q-tooltip>
        </q-btn>
      </q-toolbar>
    </slot>
    <q-separator v-if="$slots.toolbar || !usingTextEditor" />
    <q-banner v-if="historical" dense class="app-surface-tonal">
      Version {{ entry.revision }} · Schreibgeschützt.
      <template v-if="preview.kind === 'openscad'">Historische OpenSCAD-Versionen zeigen nur den Quelltext. Abhängige Dateien werden nicht historisch gerendert.</template>
    </q-banner>

    <TextEditorApp
      v-if="usingTextEditor"
      v-model="draft"
      v-model:session="workspace.editorSession"
      class="col file-viewer__editor"
      :filename="entry.name"
      :markdown="preview.kind === 'markdown'"
      :saving="saving"
      :dirty="workspace.editorDirty"
      :show-toolbar="!$slots.toolbar"
      v-model:diff-active="diffActive"
      :comparison="comparison"
      :readonly="comparison?.deleted || false"
      @retry-diff="loadDiff"
      @save="save"
      @cancel="cancel"
    >
      <template #before-content>
        <div v-if="saveError" class="file-save-error q-ma-sm q-pa-sm" role="alert">
          <div class="text-weight-medium">{{ saveErrorMessage }}</div>
          <div v-if="saveConflict" class="app-muted text-caption q-mt-xs">
            Dein Entwurf bleibt erhalten. Vergleiche ihn mit der aktuellen Datei vor dem erneuten Speichern.
            <template v-if="workspace.scope === 'session'">Von einem laufenden KI-Run belegte Ablage-Dateien können erst nach dessen Abschluss gespeichert werden.</template>
          </div>
          <div v-if="conflictLoadError" class="text-negative text-caption q-mt-xs">{{ conflictLoadErrorMessage }}</div>
          <div class="row q-gutter-sm q-mt-sm">
            <q-btn
              v-if="saveConflict"
              outline
              no-caps
              color="primary"
              label="Änderungen vergleichen"
              :loading="loadingLatest"
              @click="loadLatestForConflict"
            />
            <q-btn v-else flat no-caps color="primary" label="Erneut versuchen" @click="save" />
          </div>
        </div>
        <q-banner v-if="workspace.editorExternalChange" dense rounded class="app-surface-tonal q-ma-sm" role="alert">
          <template #avatar><q-icon name="warning_amber" color="warning" /></template>
          Die Datei wurde außerhalb des Editors geändert. Dein ungespeicherter Entwurf bleibt erhalten.
          <template #action>
            <q-btn flat no-caps color="primary" label="Aktuelle Datei laden" :disable="saving" @click="cancel" />
          </template>
        </q-banner>
      </template>
      <template #file-menu>
        <q-item clickable v-close-popup aria-label="Dateiinhalt kopieren" @click="copyPreview({ ...preview, content: draft })">
          <q-item-section avatar><q-icon name="content_copy" /></q-item-section>
          <q-item-section>Inhalt kopieren</q-item-section>
        </q-item>
        <q-item
          v-if="preview.kind === 'markdown'"
          clickable v-close-popup
          :href="printUrl"
          :disable="workspace.editorDirty"
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Markdown als PDF speichern"
        >
          <q-item-section avatar><q-icon name="picture_as_pdf" /></q-item-section>
          <q-item-section>Als PDF speichern</q-item-section>
        </q-item>
        <q-item
          v-if="entry && !historical && workspace.scope === 'session'"
          clickable v-close-popup
          :disable="workspace.editorDirty || saving"
          aria-label="Versionsverlauf öffnen"
          @click="openRevisions"
        >
          <q-item-section avatar><q-icon name="history" /></q-item-section>
          <q-item-section>Versionsverlauf</q-item-section>
        </q-item>
        <q-item v-if="canDownload" clickable v-close-popup aria-label="Datei herunterladen" @click="download">
          <q-item-section avatar><q-icon name="download" /></q-item-section>
          <q-item-section>Gespeicherte Datei herunterladen</q-item-section>
        </q-item>
        <q-item
          v-if="fullscreen || fullscreenToggle"
          clickable v-close-popup
          :aria-label="fullscreen ? 'Vollbild verlassen' : 'Im Vollbild öffnen'"
          @click="emit('toggle-fullscreen')"
        >
          <q-item-section avatar><q-icon :name="fullscreen ? 'fullscreen_exit' : 'fullscreen'" /></q-item-section>
          <q-item-section>{{ fullscreen ? 'Vollbild verlassen' : 'Im Vollbild öffnen' }}</q-item-section>
        </q-item>
        <q-item
          v-if="preview.kind === 'openscad'"
          clickable v-close-popup
          :disable="workspace.editorDirty || saving"
          aria-label="OpenSCAD-3D-Modell anzeigen"
          @click="setOpenScadView('model')"
        >
          <q-item-section avatar><q-icon name="view_in_ar" /></q-item-section>
          <q-item-section>3D-Modell anzeigen</q-item-section>
        </q-item>
      </template>
    </TextEditorApp>
    <div v-else class="col file-viewer__content q-pa-md">
      <div v-if="loading" class="fit flex flex-center"><q-spinner color="primary" size="36px" /></div>
      <AppStateMessage
        v-else-if="loadError"
        class="fit q-pa-xl"
        role="alert"
        icon="cloud_off"
        :title="loadErrorTitle"
        :description="loadErrorMessage"
      >
        <template #actions>
          <q-btn color="primary" no-caps label="Erneut versuchen" @click="loadPreview" />
        </template>
      </AppStateMessage>
      <div v-else-if="preview.kind === 'image'" class="fit relative-position">
        <div v-if="imageLoading" class="fit flex flex-center"><q-spinner color="primary" size="36px" /></div>
        <img
          v-show="!imageLoading"
          :src="contentUrl"
          :alt="viewerName || 'Bildvorschau'"
          class="file-viewer__image"
          @load="imageLoaded"
          @error="imageFailed"
        />
      </div>
      <div v-else-if="preview.kind === 'audio'" class="fit flex flex-center">
        <audio
          :src="contentUrl"
          controls
          preload="metadata"
          class="full-width"
          :style="{ colorScheme: $q.dark.isActive ? 'dark' : 'light' }"
          :aria-label="`Audio abspielen: ${viewerName}`"
          @error="preview = { kind: 'none', reason: 'invalidAudio' }"
        />
      </div>
      <MessageContent
        v-else-if="preview.kind === 'markdown'"
        class="file-viewer__markdown"
        :content="preview.content"
        :allow-images="false"
      />
      <pre v-else-if="preview.kind === 'text' || (preview.kind === 'openscad' && openScadView === 'source')" class="file-viewer__text">{{ preview.content }}</pre>
      <template v-else-if="preview.kind === 'openscad' && openScadView === 'model'">
        <div v-if="renderingOpenScad" class="fit flex flex-center" aria-live="polite">
          <div class="column items-center q-gutter-sm">
            <q-spinner color="primary" size="36px" />
            <span>OpenSCAD-Modell wird gerendert …</span>
          </div>
        </div>
        <AppStateMessage
          v-else-if="openScadError"
          class="fit q-pa-xl"
          role="alert"
          icon="view_in_ar"
          title="OpenSCAD-Modell konnte nicht gerendert werden"
          :description="openScadErrorMessage"
        >
          <template #actions>
            <q-btn color="primary" no-caps label="Erneut versuchen" @click="renderOpenScad" />
          </template>
        </AppStateMessage>
        <StlViewerPanel v-else-if="openScadUrl" :src="openScadUrl" format="off" />
      </template>
      <StlViewerPanel v-else-if="preview.kind === 'stl'" :src="stlUrl" :format="directModel?.format || 'stl'" />
      <PdfViewerPanel v-else-if="preview.kind === 'pdf'" :src="directPdf?.url || contentUrl" :name="viewerName" />
      <AppStateMessage
        v-else
        class="fit q-pa-xl"
        icon="draft"
        title="Keine Vorschau verfügbar"
        :description="unavailableMessage"
      >
        <template v-if="canDownload" #actions>
          <q-btn color="primary" no-caps icon="download" label="Datei herunterladen" @click="download" />
        </template>
      </AppStateMessage>
    </div>

    <q-dialog v-model="conflictOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" aria-labelledby="file-conflict-title">
      <q-card class="file-revisions-dialog app-surface column no-wrap">
        <q-toolbar>
          <q-toolbar-title id="file-conflict-title" class="text-subtitle1 text-weight-bold">Änderungen vergleichen</q-toolbar-title>
          <q-btn flat round dense icon="close" aria-label="Vergleich schließen" v-close-popup />
        </q-toolbar>
        <q-card-section class="col scroll">
          <p>Es wird nichts automatisch zusammengeführt. Kopiere benötigte Stellen aus der aktuellen Datei in deinen Entwurf.</p>
          <h3 class="text-subtitle2">Aktuelle Datei</h3>
          <pre class="file-viewer__text" tabindex="0" aria-label="Aktuelle Datei">{{ conflictPreview?.content }}</pre>
          <q-separator class="q-my-md" />
          <h3 class="text-subtitle2">Dein Entwurf</h3>
          <pre class="file-viewer__text" tabindex="0" aria-label="Dein Entwurf">{{ draft }}</pre>
        </q-card-section>
        <q-card-actions class="row q-gutter-sm" align="right">
          <q-btn flat no-caps label="Aktuelle Datei übernehmen" @click="resolveConflict(false)" />
          <q-btn unelevated no-caps color="primary" label="Entwurf weiterbearbeiten" @click="resolveConflict(true)" />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <q-dialog
      :transition-duration="0"
      v-model="revisionsOpen"
      :maximized="$q.screen.lt.sm"
      aria-labelledby="file-revisions-title"
    >
      <q-card class="file-revisions-dialog app-surface column no-wrap">
        <q-toolbar>
          <q-icon name="history" color="primary" size="24px" class="q-ml-sm" />
          <q-toolbar-title id="file-revisions-title" class="text-subtitle1 text-weight-bold">Versionsverlauf</q-toolbar-title>
          <q-btn flat round dense icon="close" aria-label="Versionsverlauf schließen" v-close-popup />
        </q-toolbar>
        <q-separator />
        <q-card-section v-if="revisionsLoading" class="col flex flex-center">
          <q-spinner color="primary" size="36px" />
        </q-card-section>
        <q-card-section v-else-if="!revisions.length" class="col flex flex-center text-center app-muted">
          Für diese Datei sind keine früheren Versionen vorhanden.
        </q-card-section>
        <q-card-section v-else class="col scroll q-pa-none">
          <q-list separator>
            <q-item
              v-for="revision in revisions"
              :key="revisionId(revision)"
              clickable
              v-ripple
              @click="previewRevision(revision)"
            >
              <q-item-section avatar><q-icon name="description" /></q-item-section>
              <q-item-section>
                <q-item-label>Version {{ revisionLabel(revision) }}</q-item-label>
                <q-item-label caption>{{ revisionDate(revision) }}</q-item-label>
              </q-item-section>
              <q-item-section side>
                <div class="row items-center no-wrap q-gutter-xs">
                  <q-chip v-if="isCurrentRevision(revision)" dense color="primary" text-color="white">Aktuell</q-chip>
                  <q-btn
                    v-if="!isCurrentRevision(revision) && canActivateRevision"
                    flat round dense icon="restore" color="primary"
                    :loading="activatingRevisionId === revisionId(revision)"
                    :disable="Boolean(revisionActionPending)"
                    :aria-label="`Version ${revisionLabel(revision)} aktivieren`"
                    @click.stop="activateRevision(revision)"
                  ><q-tooltip>Aktivieren</q-tooltip></q-btn>
                </div>
              </q-item-section>
            </q-item>
          </q-list>
        </q-card-section>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { copyToClipboard, useQuasar } from 'quasar'
import { useRouter } from 'vue-router'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import ImageViewerPanel from 'src/features/files/components/ImageViewerPanel.vue'
import MessageContent from 'src/features/chat/components/MessageContent.vue'
import StlViewerPanel from 'src/features/files/components/StlViewerPanel.vue'
import PdfViewerPanel from 'src/features/files/components/PdfViewerPanel.vue'
import { useConfirm } from 'src/composables/confirm'
import { apiErrorMessage, errorDescriptor } from 'src/utils/api-errors'
import { notifyError } from 'src/utils/notifications'
import { useWorkspaceStore } from 'stores/workspace'
import { useControlCenterStore } from 'stores/control-center'
import { useGitChangesStore } from 'src/features/files/git-store'

defineOptions({ name: 'FileViewerPanel' })
const TextEditorApp = defineAsyncComponent(() => import('src/features/files/components/WorkspaceFileEditor.vue'))

const props = defineProps({
  entry: { type: Object, default: null },
  image: { type: Object, default: null },
  model: { type: Object, default: null },
  pdf: { type: Object, default: null },
  diffActive: Boolean,
  fullscreen: Boolean,
  fullscreenToggle: Boolean
})
const emit = defineEmits(['close', 'toggle-fullscreen', 'update:diffActive'])
const $q = useQuasar()
const router = useRouter()
const { confirm } = useConfirm()
const workspace = useWorkspaceStore()
const controlCenter = useControlCenterStore()
const git = useGitChangesStore()
const historical = computed(() => props.entry?.revision != null)
const diffActive = computed({ get: () => props.diffActive && workspace.scope === 'filesystem' && !historical.value, set: (value) => emit('update:diffActive', value) })
const gitFile = computed(() => workspace.scope === 'filesystem' && workspace.workspaceContext?.executionAgentId === git.current?.executionAgentId
  ? git.current?.files.find((file) => `${git.current.repository?.replace(/\/$/u, '')}/${file.path}` === props.entry?.path)
  : null)
const comparison = computed(() => {
  if (historical.value || (!gitFile.value && !props.diffActive)) return null
  const state = git.current
  const data = state?.preview?.file?.path === gitFile.value?.path ? state?.preview : null
  return {
    ...data, loading: state?.loading || (gitFile.value && (!data || state?.previewLoading) && !state?.previewError),
    error: state?.error || state?.previewError, deleted: gitFile.value?.status === 'deleted',
    reason: data?.reason || (!gitFile.value && !state?.loading ? 'unchanged' : null)
  }
})
const viewerRoot = ref(null)
const loading = ref(false)
const imageLoading = ref(false)
const saving = ref(false)
const loadingLatest = ref(false)
const loadError = ref(null)
const saveError = ref(null)
const conflictLoadError = ref(null)
const conflictOpen = ref(false)
const conflictPreview = ref(null)
let conflictContextVersion = 0
let conflictViewVersion = 0
const preview = ref({ kind: 'none', reason: 'unsupported' })
const loadedImageUrl = ref('')
const openScadView = ref('model')
const renderingOpenScad = ref(false)
const exportingOpenScad = ref(false)
const openScadError = ref(null)
const openScadUrl = ref('')
const revisionsOpen = ref(false)
const revisionsLoading = ref(false)
const activatingRevisionId = ref(null)
let renderController
let renderVersion = 0
let exportController
let exportVersion = 0
let previewVersion = 0
const directImage = computed(() => props.image?.url ? props.image : null)
const directModel = computed(() => props.model?.url ? props.model : null)
const directPdf = computed(() => props.pdf?.url ? props.pdf : null)
const viewerName = computed(() => (props.entry?.name || directModel.value?.name || directPdf.value?.name || '')
  + (historical.value ? ` · Version ${props.entry.revision}` : ''))
const draft = computed({
  get: () => workspace.editorDraft ?? '',
  set: (value) => workspace.updateEditorDraft(value, preview.value.content)
})
const contentUrl = computed(() => props.entry?.path
  ? historical.value
    ? workspace.revisionDownloadUrl(props.entry.path, props.entry.revision, props.entry.fileId, 'content')
    : workspace.fileUrl('content', props.entry.path)
  : '')
const editable = computed(() => (
  props.entry?.path
  && !historical.value
  && ['text', 'markdown', 'openscad'].includes(preview.value.kind)
  && workspace.operationAvailable('filesystem.write')
))
const usingTextEditor = computed(() => diffActive.value || (workspace.editorActive && editable.value))
const canActivateRevision = computed(() => Boolean(
  props.entry?.path
  && workspace.scope === 'session'
  && workspace.operationAvailable('filesystem.write')
))
const canDownload = computed(() => Boolean(directModel.value || directPdf.value) || workspace.operationAvailable('filesystem.export'))
const stlUrl = computed(() => directModel.value?.url
  || contentUrl.value)
const openScadViewOptions = computed(() => [
  {
    label: $q.screen.gt.xs ? 'Quelltext' : undefined,
    icon: 'code',
    value: 'source',
    attrs: { 'aria-label': 'OpenSCAD-Quelltext anzeigen' }
  },
  {
    label: $q.screen.gt.xs ? '3D' : undefined,
    icon: 'view_in_ar',
    value: 'model',
    disable: workspace.editorDirty,
    attrs: {
      'aria-label': 'OpenSCAD-3D-Modell anzeigen',
      title: workspace.editorDirty ? 'Speichere zuerst die Änderungen.' : undefined
    }
  }
])
const printUrl = computed(() => {
  if (!props.entry?.path || preview.value.kind !== 'markdown') return undefined
  return router.resolve({
    name: 'markdown-print',
    query: workspace.readRequestParams({
      path: props.entry.path,
      revision: preview.value.revision
    })
  }).href
})
const loadErrorCode = computed(() => errorDescriptor(loadError.value)?.code)
const loadErrorTitle = computed(() => loadErrorCode.value === 'file.pathNotFound'
  ? 'Datei nicht gefunden'
  : 'Vorschau konnte nicht geladen werden')
const loadErrorMessage = computed(() => loadErrorCode.value === 'file.pathNotFound'
  ? `Unter „${props.entry?.path}“ ist keine Datei mehr verfügbar. Sie wurde möglicherweise verschoben oder gelöscht.`
  : apiErrorMessage(loadError.value, 'Die Vorschau konnte nicht geladen werden.'))
const saveErrorMessage = computed(() => apiErrorMessage(saveError.value, 'Die Datei konnte nicht gespeichert werden.'))
const conflictLoadErrorMessage = computed(() => apiErrorMessage(conflictLoadError.value, 'Die aktuelle Revision konnte nicht geladen werden.'))
const openScadErrorMessage = computed(() => apiErrorMessage(openScadError.value, 'Das OpenSCAD-Modell konnte nicht gerendert werden.'))
const saveConflict = computed(() => errorDescriptor(saveError.value)?.code === 'file.changed')
const revisionState = computed(() => workspace.fileRevisions[props.entry?.path] || {})
const revisions = computed(() => revisionState.value.items || revisionState.value.revisions || [])
const currentRevisionId = computed(() => (
  revisionState.value.currentRevisionId
  ?? revisionState.value.currentRevision?.id
  ?? revisionState.value.currentRevision
  ?? preview.value.revision
))
const revisionActionPending = computed(() => activatingRevisionId.value)
const unavailableMessage = computed(() => ({
  invalidAudio: 'Die Audiodatei konnte nicht geladen werden oder wird von diesem Browser nicht unterstützt.',
  invalidImage: 'Die Bilddatei ist beschädigt oder ungültig.',
  invalidPdf: 'Die PDF-Datei ist beschädigt oder ungültig.',
  unsafeSvg: 'Die SVG-Datei enthält aktive oder externe Inhalte und kann nicht sicher angezeigt werden.',
  tooLarge: 'Die Datei ist für eine Vorschau zu groß.',
  unsupported: 'Dieser Dateityp kann nur heruntergeladen werden.'
})[preview.value.reason] || 'Dieser Dateityp kann nur heruntergeladen werden.')

async function loadPreview({ revalidate = false } = {}) {
  if (diffActive.value) {
    preview.value = workspace.filePreviews[props.entry?.path] || { kind: 'none' }
    if (!workspace.editorDirty && workspace.editorDraft === null && typeof preview.value.content === 'string') workspace.editorDraft = preview.value.content
    return
  }
  const version = ++previewVersion
  saveError.value = null
  conflictLoadError.value = null
  conflictOpen.value = false
  conflictPreview.value = null
  loadingLatest.value = false
  saving.value = false
  if (!revalidate) {
    clearOpenScadRender()
    clearOpenScadExport()
    openScadView.value = historical.value ? 'source' : 'model'
    preview.value = { kind: 'none', reason: 'unsupported' }
  }
  loadError.value = null
  if (directModel.value || directPdf.value) {
    preview.value = { kind: directPdf.value ? 'pdf' : 'stl' }
    await nextTick()
    viewerRoot.value?.focus()
    return
  }
  if (!props.entry?.path) return
  const preservedPreview = workspace.editorDirty
    ? workspace.filePreviews[props.entry.path]
    : null
  if (preservedPreview) {
    preview.value = preservedPreview
    if (workspace.editorActive) openScadView.value = 'source'
    return
  }
  loading.value = true
  await nextTick()
  viewerRoot.value?.focus()
  try {
    const result = historical.value
      ? await workspace.revisionPreview(props.entry.path, props.entry.revision, props.entry.fileId)
      : revalidate ? await workspace.preview(props.entry.path, { cache: false }) : await workspace.preview(props.entry.path)
    if (version === previewVersion && result) {
      if (revalidate) {
        if (diffActive.value) return
        if (workspace.editorDirty) {
          if (result.revision !== preview.value.revision) workspace.editorExternalChange = true
          return
        }
        if (workspace.editorDraft !== null && result.kind === preview.value.kind && result.content === preview.value.content && result.revision === preview.value.revision) return
        workspace.filePreviews[props.entry.path] = result
        clearOpenScadRender()
        clearOpenScadExport()
      }
      preview.value = result
      imageLoading.value = result.kind === 'image' && loadedImageUrl.value !== contentUrl.value
      if (editable.value && (result.kind !== 'openscad' || workspace.editorActive)) {
        workspace.editorDraft = result.content || ''
        workspace.editorActive = true
        openScadView.value = 'source'
      } else if (result.kind === 'openscad' && !historical.value) void renderOpenScad()
    }
  } catch (error) {
    if (version === previewVersion) {
      loadError.value = error
      if (revalidate) notifyError(error)
    }
  } finally {
    if (version === previewVersion) loading.value = false
  }
}

async function loadDiff() {
  if (!diffActive.value || !gitFile.value || git.current?.loading) return
  await git.loadDiff(gitFile.value.path)
}

watch([
  () => diffActive.value, () => gitFile.value?.path, () => git.current?.listVersion,
  () => git.current?.loading, () => workspace.fileChangeVersions[props.entry?.path]
], ([active], [wasActive] = []) => {
  if (active) void loadDiff()
  else if (wasActive && props.entry?.path && !workspace.editorDirty) void loadPreview({ revalidate: true })
}, { immediate: true })

function revisionId(revision) {
  return revision.id ?? revision.revisionId ?? revision.revision
}

function revisionLabel(revision) {
  return revision.label || revision.number || revision.revision || revisionId(revision)
}

function revisionDate(revision) {
  const value = revision.createdAt || revision.created_at
  return value ? new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : ''
}

function isCurrentRevision(revision) {
  return revision.current === true || revisionId(revision) === currentRevisionId.value
}

function textualPreview(value) {
  return ['text', 'markdown', 'openscad'].includes(value?.kind)
    && typeof value.content === 'string'
}

async function copyPreview(value) {
  if (!textualPreview(value)) return
  try {
    await copyToClipboard(value.content)
    $q.notify({ type: 'positive', message: 'Dateiinhalt kopiert.' })
  } catch {
    $q.notify({ type: 'negative', message: 'Dateiinhalt konnte nicht kopiert werden.' })
  }
}

async function openRevisions() {
  revisionsOpen.value = true
  revisionsLoading.value = true
  try {
    await workspace.loadRevisions(props.entry.path)
  } catch {
    revisionsOpen.value = false
  } finally {
    revisionsLoading.value = false
  }
}

async function previewRevision(revision) {
  const target = workspace.fileTarget(props.entry)
  if (!target) return
  const tabId = await controlCenter.openFile({
    ...target,
    ...(!isCurrentRevision(revision) ? {
      fileId: revisionState.value.fileId,
      revision: revisionId(revision),
      readOnly: true
    } : {})
  })
  if (tabId) revisionsOpen.value = false
}

async function activateRevision(revision) {
  const id = revisionId(revision)
  const path = props.entry?.path
  const contextVersion = workspace.contextVersion
  const viewVersion = workspace.viewVersion
  const expectedState = revisionState.value
  const accepted = await confirm(
    `Soll Version ${revisionLabel(revision)} aktiviert werden? Die Datei wird auf diesen Stand gesetzt. Alle Versionen bleiben erhalten.`,
    'Version aktivieren?',
    { okLabel: 'Aktivieren', okColor: 'primary' }
  )
  if (
    !accepted
    || props.entry?.path !== path
    || workspace.contextVersion !== contextVersion
    || workspace.viewVersion !== viewVersion
  ) return
  activatingRevisionId.value = id
  try {
    const result = await workspace.activateRevision(path, id, expectedState)
    if (!result) return
    $q.notify({ type: 'positive', message: 'Version aktiviert.' })
  } finally {
    activatingRevisionId.value = null
  }
}

async function setOpenScadView(view) {
  if (view === 'model' && workspace.editorDirty) {
    $q.notify({ type: 'warning', message: 'Speichere zuerst die Änderungen am Quelltext.' })
    return
  }
  if (view === 'model') {
    diffActive.value = false
    workspace.editorActive = false
  }
  else if (editable.value) {
    workspace.editorDraft = preview.value.content
    workspace.editorActive = true
  }
  if (view === 'source' && renderingOpenScad.value) clearOpenScadRender()
  openScadView.value = view
  if (view === 'model') await renderOpenScad()
}

async function renderOpenScad() {
  if (historical.value) return
  if (!props.entry?.path || openScadUrl.value || renderingOpenScad.value || workspace.editorDirty) return
  clearOpenScadRender()
  const version = ++renderVersion
  renderingOpenScad.value = true
  openScadError.value = null
  renderController = new AbortController()
  try {
    const blob = await workspace.renderOpenScad(
      props.entry.path,
      preview.value.revision,
      renderController.signal
    )
    if (!blob || version !== renderVersion || openScadView.value !== 'model') return
    openScadUrl.value = URL.createObjectURL(blob)
  } catch (error) {
    if (error?.name !== 'CanceledError' && error?.name !== 'AbortError' && version === renderVersion) {
      openScadError.value = error
    }
  } finally {
    if (version === renderVersion) renderingOpenScad.value = false
  }
}

async function exportOpenScad() {
  if (historical.value) return
  if (
    !props.entry?.path || renderingOpenScad.value || exportingOpenScad.value
    || workspace.editorDirty
  ) return
  const path = props.entry.path
  const revision = preview.value.revision
  const filename = `${props.entry.name.replace(/\.scad$/i, '')}.stl`
  const version = ++exportVersion
  exportingOpenScad.value = true
  exportController = new AbortController()
  try {
    const blob = await workspace.renderOpenScad(path, revision, exportController.signal, 'stl')
    if (!blob || version !== exportVersion || props.entry?.path !== path) return
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  } catch {
    // The central Axios interceptor displays the API error.
  } finally {
    if (version === exportVersion) {
      exportController = null
      exportingOpenScad.value = false
    }
  }
}

function clearOpenScadExport() {
  exportVersion += 1
  exportController?.abort()
  exportController = null
  exportingOpenScad.value = false
}

function clearOpenScadRender() {
  renderVersion += 1
  renderController?.abort()
  renderController = null
  renderingOpenScad.value = false
  openScadError.value = null
  if (openScadUrl.value) URL.revokeObjectURL(openScadUrl.value)
  openScadUrl.value = ''
}

async function cancel() {
  const context = workspace.contextVersion
  const version = workspace.viewVersion
  if (workspace.editorDirty) {
    const accepted = await confirm(
      'Sollen die ungespeicherten Änderungen verworfen werden?',
      'Änderungen verwerfen?',
      { okLabel: 'Verwerfen' }
    )
    if (!accepted) return
  }
  if (context !== workspace.contextVersion || version !== workspace.viewVersion) return
  workspace.updateEditorDraft(preview.value.content, preview.value.content)
  workspace.editorSession = null
  saveError.value = null
  conflictPreview.value = null
  if (workspace.editorExternalChange) {
    workspace.editorExternalChange = false
    await loadPreview()
  }
}

async function save() {
  if (saving.value || !workspace.editorDirty) return
  const version = previewVersion
  saveError.value = null
  conflictLoadError.value = null
  saving.value = true
  try {
    const result = await workspace.saveFile(props.entry.path, draft.value, preview.value.revision)
    if (!result || version !== previewVersion) return
    clearOpenScadRender()
    preview.value = workspace.filePreviews[props.entry.path]
    $q.notify({ type: 'positive', message: 'Datei gespeichert.' })
  } catch (error) {
    if (version === previewVersion) saveError.value = error
  } finally {
    if (version === previewVersion) saving.value = false
  }
}

async function loadLatestForConflict() {
  if (loadingLatest.value) return
  const version = previewVersion
  loadingLatest.value = true
  conflictLoadError.value = null
  conflictContextVersion = workspace.contextVersion
  conflictViewVersion = workspace.viewVersion
  try {
    const result = await workspace.preview(props.entry.path, { cache: false })
    if (result && version === previewVersion && conflictContextVersion === workspace.contextVersion && conflictViewVersion === workspace.viewVersion) {
      if (!textualPreview(result)) {
        conflictLoadError.value = { code: 'file.changed' }
        return
      }
      conflictPreview.value = result
      conflictOpen.value = true
    }
  } catch (error) {
    if (version === previewVersion) conflictLoadError.value = error
  } finally {
    if (version === previewVersion) loadingLatest.value = false
  }
}

async function resolveConflict(keepDraft) {
  const latest = conflictPreview.value
  if (!latest) return
  const accepted = await confirm(
    keepDraft
      ? 'Beim nächsten Speichern ersetzt dein Entwurf die angezeigte aktuelle Datei. Nicht übernommene Änderungen gehen dabei verloren. Entwurf auf dieser Grundlage weiterbearbeiten?'
      : 'Soll dein ungespeicherter Entwurf durch die aktuelle Datei ersetzt werden?',
    keepDraft ? 'Neue Speichergrundlage übernehmen?' : 'Entwurf verwerfen?',
    { okLabel: keepDraft ? 'Weiterbearbeiten' : 'Verwerfen' }
  )
  if (!accepted || conflictContextVersion !== workspace.contextVersion || conflictViewVersion !== workspace.viewVersion) return
  preview.value = latest
  workspace.filePreviews[props.entry.path] = latest
  workspace.updateEditorDraft(keepDraft ? draft.value : latest.content, latest.content)
  workspace.editorExternalChange = false
  saveError.value = null
  conflictOpen.value = false
}

function download() {
  if (historical.value) {
    const anchor = document.createElement('a')
    anchor.href = workspace.revisionDownloadUrl(props.entry.path, props.entry.revision, props.entry.fileId)
    anchor.download = props.entry.name
    anchor.click()
    return
  }
  const directFile = directPdf.value || directModel.value
  if (!directFile) {
    workspace.download(props.entry.path)
    return
  }
  const anchor = document.createElement('a')
  anchor.href = directFile.downloadUrl || directFile.url
  anchor.download = directFile.name || 'datei'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

function handleEscape() {
  if (props.fullscreen) emit('toggle-fullscreen')
  else emit('close')
}

function imageFailed() {
  imageLoading.value = false
  preview.value = { kind: 'none', reason: 'invalidImage' }
}

function imageLoaded() {
  loadedImageUrl.value = contentUrl.value
  imageLoading.value = false
}

watch(
  [
    () => props.entry?.path,
    () => props.entry?.revision,
    () => props.entry?.fileId,
    () => directModel.value?.url,
    () => directPdf.value?.url,
    () => workspace.scope,
    () => workspace.sessionId,
    () => workspace.currentDirectory,
    () => workspace.rootIdentity,
    () => workspace.workspaceContext,
    () => workspace.fileChangeVersions[props.entry?.path]
  ],
  loadPreview,
  { immediate: true }
)

onBeforeUnmount(() => {
  previewVersion += 1
  clearOpenScadRender()
  clearOpenScadExport()
})
</script>

<style scoped>
.file-revisions-dialog {
  width: min(720px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
}
</style>
