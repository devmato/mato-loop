<template>
  <div ref="explorerRoot" class="column full-height no-wrap file-explorer">
    <div class="file-explorer__controls">
      <q-toolbar class="file-explorer__toolbar q-px-sm" role="toolbar" aria-label="Dateien durchsuchen und verwalten">
        <q-btn
          class="file-icon-action"
          flat
          round
          dense
          icon="arrow_upward"
          aria-label="Zum übergeordneten Ordner"
          :disable="!explorerAvailable || atBrowserRoot"
          @click="navigateUp"
        >
          <q-tooltip>Zum übergeordneten Ordner</q-tooltip>
        </q-btn>
        <q-separator vertical inset class="q-mx-xs" />
        <q-input
          v-if="searchOpen"
          ref="searchInput"
          v-model="workspace.filter"
          class="file-explorer__filter col"
          borderless
          dense
          clearable
          placeholder="Geladene Einträge filtern"
          aria-label="Geladene Einträge filtern"
          :disable="!explorerAvailable"
          @keydown.esc.stop.prevent="closeSearch"
        />
        <q-breadcrumbs v-else class="file-breadcrumbs col q-px-sm" gutter="xs" active-color="primary">
          <template #separator><q-icon name="chevron_right" size="18px" /></template>
          <q-breadcrumbs-el
            v-for="crumb in selectedBreadcrumbs"
            :key="crumb.path"
            :icon="crumb.icon"
            :label="crumb.label"
            :aria-label="crumb.ariaLabel"
            :class="{ 'cursor-pointer': explorerAvailable && crumb.path !== workspace.directoryPath }"
            @click="explorerAvailable && crumb.path !== workspace.directoryPath && navigateDirectory(crumb.path)"
          />
        </q-breadcrumbs>
        <q-btn
          flat
          round
          dense
          :icon="searchOpen ? 'close' : 'search'"
          :aria-label="searchOpen ? 'Suche schließen' : 'Dateien durchsuchen'"
          :disable="!explorerAvailable"
          @click="toggleSearch"
        >
          <q-tooltip>{{ searchOpen ? 'Suche schließen' : 'Dateien durchsuchen' }}</q-tooltip>
        </q-btn>
        <q-btn
          flat
          round
          dense
          icon="refresh"
          aria-label="Dateien aktualisieren"
          :loading="!projectOverview && workspace.loading[workspace.directoryPath]"
          :disable="!explorerAvailable"
          @click="refreshFiles"
        >
          <q-tooltip>Dateien aktualisieren</q-tooltip>
        </q-btn>
        <q-separator
          v-if="hasOverflowActions || canCreateFile || canCreateDirectory || canUpload"
          vertical
          inset
          class="q-mx-xs"
        />
        <q-btn
          v-if="canCreateFile || canCreateDirectory"
          class="file-explorer__primary-action"
          flat
          round
          dense
          color="primary"
          icon="add"
          aria-label="Datei oder Ordner erstellen"
        >
          <q-tooltip>Neu</q-tooltip>
          <AppMenu anchor="bottom right" self="top right">
            <q-list padding class="file-actions-menu">
              <q-item v-if="canCreateFile" clickable v-ripple v-close-popup @click="openCreateDialog('file')">
                <q-item-section avatar><q-icon name="note_add" /></q-item-section>
                <q-item-section>Neue Datei</q-item-section>
              </q-item>
              <q-item v-if="canCreateDirectory" clickable v-ripple v-close-popup @click="openCreateDialog('directory')">
                <q-item-section avatar><q-icon name="create_new_folder" /></q-item-section>
                <q-item-section>Neuer Ordner</q-item-section>
              </q-item>
            </q-list>
          </AppMenu>
        </q-btn>
        <q-btn
          v-if="canUpload"
          class="file-upload-action file-explorer__primary-action"
          flat
          round
          dense
          color="primary"
          icon="upload_file"
          aria-label="Dateien hochladen"
          :loading="workspace.uploading"
          @click="uploadInput.click()"
        >
          <q-tooltip>Dateien hochladen</q-tooltip>
        </q-btn>
        <q-btn
          v-if="hasOverflowActions || canCreateFile || canCreateDirectory || canUpload"
          flat
          round
          dense
          icon="more_vert"
          aria-label="Weitere Dateiaktionen"
        >
          <q-tooltip>Weitere Aktionen</q-tooltip>
          <AppMenu anchor="bottom right" self="top right">
            <q-list padding>
              <q-item
                v-if="directoryArchiveUrl"
                tag="a"
                :href="directoryArchiveUrl"
                download
                clickable
                v-ripple
                v-close-popup
              >
                <q-item-section avatar><q-icon name="folder_zip" /></q-item-section>
                <q-item-section>Ordner als ZIP herunterladen</q-item-section>
              </q-item>
              <q-item
                v-if="workspace.scope === 'filesystem' && selectedIsDirectory && workspace.operationAvailable('filesystem.list')"
                clickable
                v-ripple
                v-close-popup
                :disable="chat.running"
                @click="setWorkdir"
              >
                <q-item-section avatar><q-icon name="home_work" /></q-item-section>
                <q-item-section>Als aktuellen Ordner verwenden</q-item-section>
              </q-item>
              <q-item
                v-if="workspace.scope === 'filesystem' && explorerAvailable"
                clickable
                v-ripple
                v-close-popup
                @click="openAbsolutePath"
              >
                <q-item-section avatar><q-icon name="drive_file_move" /></q-item-section>
                <q-item-section>Absoluten Pfad öffnen</q-item-section>
              </q-item>
              <q-separator v-if="hasOverflowActions && (canCreateFile || canCreateDirectory || canUpload)" spaced />
              <q-item v-if="canCreateFile" clickable v-ripple v-close-popup @click="openCreateDialog('file')">
                <q-item-section avatar><q-icon name="note_add" /></q-item-section>
                <q-item-section>Neue Datei</q-item-section>
              </q-item>
              <q-item v-if="canCreateDirectory" clickable v-ripple v-close-popup @click="openCreateDialog('directory')">
                <q-item-section avatar><q-icon name="create_new_folder" /></q-item-section>
                <q-item-section>Neuer Ordner</q-item-section>
              </q-item>
              <q-item v-if="canUpload" clickable v-ripple v-close-popup @click="uploadInput.click()">
                <q-item-section avatar><q-icon name="upload_file" /></q-item-section>
                <q-item-section>Dateien hochladen</q-item-section>
              </q-item>
            </q-list>
          </AppMenu>
        </q-btn>
      </q-toolbar>
      <input ref="uploadInput" type="file" multiple class="hidden" @change="uploadSelected">
    </div>
    <q-separator />
    <q-linear-progress
      v-if="!projectOverview && workspace.loading[workspace.directoryPath]"
      indeterminate
      color="primary"
      aria-label="Dateien werden geladen"
    />

    <q-scroll-area class="col">
      <div
        v-if="explorerUnavailableMessage"
        class="file-load-state app-muted q-ma-md q-pa-lg text-center"
        role="status"
      >
        <q-icon :name="explorerUnavailableIcon" size="40px" />
        <div class="q-mt-sm">{{ explorerUnavailableMessage }}</div>
      </div>
      <div
        v-else
        class="q-pa-sm"
        role="tree"
        aria-label="Dateien und Ordner"
        :tabindex="treeFocusPath === displayedDirectoryPath ? 0 : -1"
        @focus.self="focusTreeItem(visibleTreeItems()[0])"
        @keydown="handleTreeKeydown"
      >
        <div v-if="!projectOverview && workspace.loadErrors[workspace.directoryPath]" class="file-load-state q-ma-sm q-pa-md text-center" role="alert">
          <q-icon name="cloud_off" size="32px" class="app-muted" />
          <div class="q-mt-sm">{{ rootLoadError }}</div>
          <q-btn class="q-mt-sm" flat no-caps color="primary" label="Erneut versuchen" @click="workspace.load(workspace.directoryPath)" />
        </div>
        <div v-if="!projectOverview && workspace.loading[workspace.directoryPath] && !workspace.entries[workspace.directoryPath]" class="q-pa-lg text-center">
          <q-spinner color="primary" size="32px" />
          <div class="app-muted q-mt-sm">Dateien werden geladen ...</div>
        </div>
        <div v-else-if="filterEmpty" class="file-load-state app-muted q-ma-sm q-pa-md text-center">
          <q-icon name="search_off" size="32px" />
          <div class="q-mt-sm">Keine Dateien oder Ordner entsprechen dem Filter.</div>
        </div>
        <div
          v-else-if="displayEntries && !displayEntries.length && !workspace.loadErrors[workspace.directoryPath]"
          class="file-load-state app-muted q-ma-sm q-pa-md text-center"
        >
          <q-icon name="folder_open" size="32px" />
          <div class="q-mt-sm">Hier sind noch keine Dateien oder Ordner.</div>
        </div>
        <FileTreeNode
          v-for="entry in displayEntries"
          :key="`${workspace.contextVersion}:${entry.path}`"
          :entry="entry"
          :filter="workspace.filter || ''"
          :focus-path="treeFocusPath"
          :mutable="!projectOverview"
          @open-file="openViewer"
          @open-directory="openDirectory"
          @focus-entry="treeFocusPath = $event"
          @download-entry="workspace.download($event.path)"
          @rename-entry="openRenameDialog"
          @delete-entry="removeEntry"
        />
        <q-btn
          v-if="!projectOverview && workspace.hasMore[workspace.directoryPath]"
          flat
          dense
          no-caps
          color="primary"
          label="Weitere laden"
          class="q-ml-xl"
          :disable="workspace.loading[workspace.directoryPath]"
          @click="workspace.load(workspace.directoryPath, true)"
        />
      </div>
    </q-scroll-area>
    <q-dialog v-model="renameDialogOpen" :transition-duration="0" :persistent="renaming">
      <q-card class="file-rename-dialog">
        <q-form @submit.prevent="renameEntry">
          <q-card-section>
            <div class="text-h6">{{ renameTarget?.kind === 'directory' ? 'Ordner umbenennen' : 'Datei umbenennen' }}</div>
            <div class="text-caption app-muted q-mt-xs ellipsis">{{ renameTarget?.path }}</div>
          </q-card-section>
          <q-card-section class="q-pt-none">
            <q-input dense
              v-model="renameName"
              autofocus
              outlined
              label="Name"
              maxlength="255"
              lazy-rules
              :rules="[requiredRule]"
            />
          </q-card-section>
          <q-card-actions align="right" class="q-pa-md q-pt-none">
            <q-btn v-close-popup flat no-caps label="Abbrechen" :disable="renaming" />
            <q-btn
              unelevated
              no-caps
              color="primary"
              label="Umbenennen"
              type="submit"
              :loading="renaming"
              :disable="!canRename"
            />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>

    <q-dialog v-model="createDialogOpen" :transition-duration="0" :persistent="creating">
      <q-card class="file-create-dialog">
        <q-form @submit.prevent="createFile">
          <q-card-section>
            <div class="text-h6">{{ createKind === 'directory' ? 'Neuer Ordner' : 'Neue Datei' }}</div>
            <div class="text-caption app-muted q-mt-xs ellipsis">{{ mutationDirectory }}</div>
          </q-card-section>
          <q-card-section class="q-pt-none q-gutter-y-md">
            <AppSelect dense
              v-if="createKind === 'file'"
              v-model="createSuffix"
              outlined
              emit-value
              map-options
              label="Dateityp"
              :options="fileTypeOptions"
            />
            <q-input dense
              v-model="createName"
              autofocus
              outlined
              :label="createKind === 'directory' ? 'Ordnername' : 'Dateiname'"
              maxlength="240"
              lazy-rules
              :rules="[requiredRule]"
              :hint="createKind === 'file' ? (createName.trim() ? `Wird als „${createFileName}“ angelegt.` : 'Die Endung wird automatisch ergänzt.') : undefined"
            />
          </q-card-section>
          <q-card-actions align="right" class="q-pa-md q-pt-none">
            <q-btn v-close-popup flat no-caps label="Abbrechen" :disable="creating" />
            <q-btn
              unelevated
              no-caps
              color="primary"
              label="Anlegen"
              type="submit"
              :loading="creating"
              :disable="!createName.trim()"
            />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>

  </div>
</template>

<script setup>
import AppMenu from 'src/shared/ui/AppMenu.vue'
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, nextTick, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import FileTreeNode from 'src/features/files/components/FileTreeNode.vue'
import { useConfirm } from 'src/composables/confirm'
import { apiErrorMessage } from 'src/utils/api-errors'
import { absoluteComputerPath } from 'src/features/files/state'
import { useChatStore } from 'src/features/chat/store'
import { useControlCenterStore } from 'stores/control-center'
import { useExecutionAgentStore } from 'stores/execution-agents'
import { useInstanceLimitsStore } from 'stores/instance-limits'
import {
  MAX_WORKSPACE_UPLOADS,
  parentPath,
  workspaceContextKey,
  withFileSuffix,
  useWorkspaceStore
} from 'stores/workspace'

defineOptions({ name: 'FileExplorerPanel' })
const props = defineProps({
  scope: { type: String, default: 'session' }
})

const $q = useQuasar()
const { confirm } = useConfirm()
const workspace = useWorkspaceStore()
const chat = useChatStore()
const controlCenter = useControlCenterStore()
const executionAgents = useExecutionAgentStore()
const instanceLimits = useInstanceLimitsStore()
const uploadInput = ref(null)
const explorerRoot = ref(null)
const searchInput = ref(null)
const searchOpen = ref(Boolean(workspace.filter))
const treeFocusPath = ref('.')
const renameDialogOpen = ref(false)
const renameTarget = ref(null)
const renameName = ref('')
const renaming = ref(false)
const createDialogOpen = ref(false)
const createKind = ref('file')
const createName = ref('')
const createSuffix = ref('.txt')
const creating = ref(false)
const selectedRootIdentity = ref(null)
const projectOverview = ref(false)
let typeahead = ''
let typeaheadTimer = null
const fileTypeOptions = [
  { label: 'Textdatei (.txt)', value: '.txt' },
  { label: 'Markdown (.md)', value: '.md' },
  { label: 'OpenSCAD (.scad)', value: '.scad' }
]
const createFileName = computed(() => withFileSuffix(createName.value, createSuffix.value))
const selectedIsDirectory = computed(() => workspace.selectedKind === 'directory')
const selectedExecutionAgent = computed(() => (
  executionAgents.byId(chat.activeContext?.preferredExecutionAgentId)
))
const directoryArchiveUrl = computed(() => projectOverview.value ? '' : workspace.archiveUrl(workspace.directoryPath))
const hasOverflowActions = computed(() => Boolean(
  directoryArchiveUrl.value || (workspace.scope === 'filesystem' && explorerAvailable.value)
))
const targetDirectory = computed(() => {
  if (selectedIsDirectory.value) return workspace.selectedPath
  return parentPath(workspace.selectedPath)
})
const mutationDirectory = computed(() => targetDirectory.value)
const writableLocation = computed(() => !projectOverview.value)
const canCreateFile = computed(() => writableLocation.value && workspace.operationAvailable('filesystem.import'))
const canCreateDirectory = computed(() => writableLocation.value && workspace.operationAvailable('filesystem.mkdir'))
const canUpload = computed(() => writableLocation.value && workspace.operationAvailable('filesystem.import'))
const canRename = computed(() => Boolean(
  !renaming.value
  && renameName.value.trim()
  && renameTarget.value
  && renameName.value.trim() !== renameTarget.value.name
))
const explorerAvailable = computed(() => projectOverview.value || workspace.operationAvailable('filesystem.list'))
const explorerUnavailableMessage = computed(() => {
  if (explorerAvailable.value) return ''
  if (workspace.scope === 'session') {
    return 'Dateien sind verfügbar, sobald die Session erstellt wurde.'
  }
  if (!chat.activeContext?.preferredExecutionAgentId) {
    return workspace.sessionId
      ? 'Wähle einen Computer aus, um Dateien anzuzeigen.'
      : 'Erstelle zuerst eine Session oder wähle einen Computer aus, um Dateien anzuzeigen.'
  }
  if (!selectedExecutionAgent.value) {
    return 'Der ausgewählte Computer ist nicht mehr verfügbar.'
  }
  if (!selectedExecutionAgent.value.connected) {
    return 'Der ausgewählte Computer ist offline.'
  }
  return 'Der ausgewählte Computer bietet keinen Dateizugriff an.'
})
const explorerUnavailableIcon = computed(() => (
  selectedExecutionAgent.value?.connected === false ? 'cloud_off' : 'folder_off'
))
const browserRootPath = computed(() => {
  if (workspace.scope !== 'filesystem') return '.'
  const selected = workspace.currentDirectory || workspace.directoryPath
  return /^[A-Z]:\//u.test(selected || '') ? selected.slice(0, 3) : '/'
})
const displayedDirectoryPath = computed(() => projectOverview.value ? '@project' : workspace.directoryPath)
const atBrowserRoot = computed(() => projectOverview.value || (
  workspace.directoryPath === browserRootPath.value && !chat.current?.projectId
))
const selectedBreadcrumbs = computed(() => {
  const filesystem = workspace.scope === 'filesystem'
  const parts = workspace.directoryPath.split('/').filter((part) => part && part !== '.')
  const windows = filesystem && /^[A-Z]:\//u.test(workspace.directoryPath)
  if (windows) parts.shift()
  if (!filesystem && chat.current?.projectId) {
    if (projectOverview.value) return [{ icon: 'folder_open', label: 'Projekt', ariaLabel: 'Projekt', path: '@project' }]
    const selected = managedSessionOptions.value.find((option) => option.value === workspace.rootIdentity)
    return [
      { icon: 'folder_open', label: 'Projekt', ariaLabel: 'Projekt', path: '@project' },
      { icon: 'folder', label: selected?.label || 'Session', ariaLabel: selected?.label || 'Session', path: '.' },
      ...parts.map((label, index) => ({
        label,
        ariaLabel: label,
        path: parts.slice(0, index + 1).join('/')
      }))
    ]
  }
  return [
    {
      icon: filesystem ? 'computer' : 'folder_open',
      ariaLabel: filesystem ? 'Computer' : 'Ablage',
      path: filesystem ? browserRootPath.value : '.'
    },
    ...parts.map((label, index) => ({
      label,
      ariaLabel: label,
      path: `${filesystem ? browserRootPath.value.replace(/\/$/u, '') + '/' : ''}${parts.slice(0, index + 1).join('/')}`
    }))
  ]
})
const projectSessionEntries = computed(() => managedSessionOptions.value.map((option) => ({
  kind: 'directory',
  name: option.label,
  path: `@session/${option.value}`,
  sessionId: option.value,
  currentSession: option.value === chat.current.id
})))
const displayEntries = computed(() => projectOverview.value
  ? projectSessionEntries.value
  : workspace.entries[workspace.directoryPath] || [])
const filterEmpty = computed(() => Boolean(
  workspace.filter
  && !displayEntries.value.some((entry) => (
    entry.name?.toLocaleLowerCase().includes(workspace.filter.toLocaleLowerCase())
  ))
))
const rootLoadError = computed(() => apiErrorMessage(
  workspace.loadErrors[workspace.directoryPath],
  'Die Dateien konnten nicht geladen werden.'
))
const requiredRule = (value) => Boolean(String(value || '').trim()) || 'Name erforderlich.'
const activeWorkspaceContext = computed(() => executionAgents.workspaceContextFor(chat.activeContext))
const managedSessionOptions = computed(() => {
  const current = chat.current
  if (props.scope !== 'session' || !current?.id || !current.projectId) return []
  const sessions = chat.sessionList(current.projectId)?.items || []
  const candidates = [current, ...sessions.filter((session) => session.id !== current.id)]
    .filter((session) => session.projectId === current.projectId)
  const titles = candidates.map((session) => session.title?.trim() || 'Unbenannte Session')
  const titleCounts = new Map(titles.map((title) => [title, titles.filter((item) => item === title).length]))
  return candidates.map((session, index) => ({
    label: `${titles[index]}${titleCounts.get(titles[index]) > 1 || !session.title?.trim() ? ` · ${session.id.length > 12 ? session.id.slice(-8) : session.id}` : ''}`,
    value: session.id
  }))
})
async function loadProjectSessions(projectId) {
  if (!projectId) return
  const list = chat.sessionList(projectId)
  if (!list?.loaded) await chat.fetchSessions({ suppressErrorToast: true }, projectId)
  while (chat.sessionList(projectId)?.nextCursor) {
    await chat.fetchMoreSessions({ suppressErrorToast: true }, projectId)
  }
}

function refreshFiles() {
  if (projectOverview.value) return loadProjectSessions(chat.current?.projectId)
  const requests = [workspace.refresh()]
  if (workspace.rootIdentity) requests.push(workspace.fetchStorageUsage(workspace.rootIdentity))
  return Promise.all(requests)
}

async function toggleSearch() {
  if (searchOpen.value) {
    closeSearch()
    return
  }
  searchOpen.value = true
  await nextTick()
  searchInput.value?.focus()
}

function closeSearch() {
  workspace.filter = ''
  searchOpen.value = false
}

function openCreateDialog(kind) {
  createKind.value = kind
  createName.value = ''
  createSuffix.value = '.txt'
  createDialogOpen.value = true
}

async function createFile() {
  if (!createName.value.trim() || creating.value) return
  creating.value = true
  try {
    if (createKind.value === 'directory') {
      await workspace.createDirectory(mutationDirectory.value, createName.value.trim())
    } else {
      const entry = await workspace.createFile(mutationDirectory.value, createFileName.value)
      await openViewer({ entry, edit: true })
    }
    createDialogOpen.value = false
  } catch {
    // The central API interceptor displays the error; keep the dialog open.
  } finally {
    creating.value = false
  }
}

async function uploadSelected(event) {
  const files = [...(event.target.files || [])]
  event.target.value = ''
  if (!files.length) return
  if (files.length > MAX_WORKSPACE_UPLOADS) {
    $q.notify({ type: 'negative', message: `Bitte höchstens ${MAX_WORKSPACE_UPLOADS} Dateien gleichzeitig auswählen.` })
    return
  }
  const oversized = files.find((file) => file.size > instanceLimits.maxUploadBytes)
  if (oversized) {
    $q.notify({ type: 'negative', message: `„${oversized.name}“ ist größer als ${instanceLimits.maxUploadMib} MiB.` })
    return
  }
  try {
    const count = await workspace.uploadFiles(mutationDirectory.value, files)
    if (count) {
      $q.notify({ type: 'positive', message: `${count} ${count === 1 ? 'Datei' : 'Dateien'} hochgeladen.` })
    }
  } catch (error) {
    if (error.uploadedCount) {
      $q.notify({
        type: 'warning',
        message: error.uploadCompleted
          ? 'Der Upload wurde noch in der vorherigen Ansicht abgeschlossen.'
          : error.uploadInterrupted
          ? `${error.uploadedCount} Dateien wurden hochgeladen; die übrigen wurden nach dem Ansichtswechsel übersprungen.`
          : `${error.uploadedCount} Dateien wurden hochgeladen, bevor ein Fehler auftrat.`
      })
    } else if (error.uploadInterrupted) {
      $q.notify({ type: 'info', message: 'Der Upload wurde nach dem Ansichtswechsel abgebrochen.' })
    }
  }
}

async function openViewer({ entry, edit = false }) {
  if (entry?.kind !== 'file') return
  const target = workspace.fileTarget(entry, edit)
  if (!target) return
  const tabId = await controlCenter.openFile(target)
  if (edit && tabId) workspace.startFileTabEdit(tabId)
}

function visibleTreeItems() {
  return [...(explorerRoot.value?.querySelectorAll('[role="treeitem"]') || [])]
}

function focusTreeItem(item) {
  if (!item) return
  treeFocusPath.value = item.dataset.filePath
  item.focus()
}

function handleTreeKeydown(event) {
  if (event.altKey || event.ctrlKey || event.metaKey || event.target.closest('button')) return
  const current = event.target.closest('[role="treeitem"]')
  if (!current) return
  const items = visibleTreeItems()
  const index = items.indexOf(current)
  let target = null
  if (event.key === 'ArrowDown') target = items[index + 1] || current
  else if (event.key === 'ArrowUp') target = items[index - 1] || current
  else if (event.key === 'Home') target = items[0]
  else if (event.key === 'End') target = items.at(-1)
  else if (event.key === 'ArrowRight' && current.dataset.fileKind === 'directory') {
    event.preventDefault()
    current.click()
    return
  } else if (event.key === 'ArrowLeft' && !atBrowserRoot.value) {
    event.preventDefault()
    navigateUp()
    return
  } else if (event.key === 'Enter' || event.key === ' ') {
    current.click()
  } else if (event.key.length === 1 && /\S/u.test(event.key)) {
    clearTimeout(typeaheadTimer)
    typeahead += event.key.toLocaleLowerCase()
    typeaheadTimer = setTimeout(() => { typeahead = '' }, 700)
    target = [...items.slice(index + 1), ...items.slice(0, index + 1)].find((item) => (
      item.textContent.trim().toLocaleLowerCase().startsWith(typeahead)
    ))
  } else return
  event.preventDefault()
  focusTreeItem(target)
}

function navigateDirectory(path) {
  if (path === '@project') {
    projectOverview.value = true
    treeFocusPath.value = '@project'
    return
  }
  projectOverview.value = false
  workspace.openDirectory(path)
  treeFocusPath.value = path
  focusPath(path)
}

function navigateUp() {
  if (workspace.scope === 'session' && chat.current?.projectId && workspace.directoryPath === '.') {
    navigateDirectory('@project')
    return
  }
  navigateDirectory(parentPath(workspace.directoryPath))
}

function openDirectory(entry) {
  if (projectOverview.value && entry.sessionId) {
    selectedRootIdentity.value = entry.sessionId
    projectOverview.value = false
    treeFocusPath.value = '.'
    return
  }
  navigateDirectory(entry.path)
}

async function focusPath(path, fallback = workspace.directoryPath) {
  await nextTick()
  const rows = [...(explorerRoot.value?.querySelectorAll('[data-file-path]') || [])]
  const row = rows.find((item) => item.dataset.filePath === path)
    || rows.find((item) => item.dataset.filePath === fallback)
    || rows.find((item) => item.dataset.filePath === workspace.directoryPath)
    || rows[0]
  row?.focus()
}

async function setWorkdir() {
  await chat.updateActive({ current_directory: workspace.selectedPath })
  $q.notify({ type: 'positive', message: 'Aktueller Ordner wurde geändert.' })
}

function openAbsolutePath() {
  $q.dialog({
    transitionDuration: 0,
    color: 'primary',
    title: 'Absoluten Pfad öffnen',
    prompt: {
      dense: true,
      model: workspace.directoryPath,
      type: 'text',
      label: 'Absoluter Pfad',
      isValid: (value) => absoluteComputerPath(String(value || '').trim())
    },
    cancel: { label: 'Abbrechen', color: undefined, class: 'app-muted', flat: true, noCaps: true },
    ok: { label: 'Öffnen', unelevated: true, noCaps: true, color: 'primary' }
  }).onOk((path) => workspace.openDirectory(path.trim()))
}

function openRenameDialog(entry) {
  workspace.selectPath(entry.path, entry.kind)
  renameTarget.value = entry
  renameName.value = entry.name
  renameDialogOpen.value = true
}

async function renameEntry() {
  if (!canRename.value) return
  renaming.value = true
  try {
    const source = fileTargetForPath(renameTarget.value.path)
    const renamed = await workspace.renameEntry(renameTarget.value.path, renameName.value.trim())
    if (source) {
      const reboundTabs = await controlCenter.rebindFileTargets(source, renamed.path).catch(() => [])
      workspace.rebindFileTabPaths(reboundTabs, source.path, renamed.path)
    }
    renameDialogOpen.value = false
    await focusPath(renamed.path, targetDirectory.value)
  } catch {
    // The central API interceptor displays the error; keep the dialog open.
  } finally {
    renaming.value = false
  }
}

async function removeEntry(entry) {
  workspace.selectPath(entry.path, entry.kind)
  const target = fileTargetForPath(entry.path)
  const affectedTabs = target ? controlCenter.fileTabsForTarget(target) : []
  const dirtyHint = affectedTabs.some((tab) => workspace.fileTabDirty(tab.tabId))
    ? ' Ungespeicherte Änderungen in geöffneten Datei-Tabs gehen verloren.'
    : ''
  const accepted = await confirm(
    entry.kind === 'directory'
      ? `Soll der Ordner „${entry.name}“ einschließlich aller enthaltenen Dateien und Ordner gelöscht werden?${dirtyHint}`
      : `Soll die Datei „${entry.name}“ gelöscht werden?${dirtyHint}`,
    entry.kind === 'directory' ? 'Ordner löschen' : 'Datei löschen',
    { okLabel: 'Löschen', okColor: 'negative' }
  )
  if (!accepted) return
  const parent = parentPath(entry.path)
  await workspace.deleteEntry(entry.path)
  if (target) {
    const removedTabs = await controlCenter.rebindFileTargets(target).catch(() => [])
    removedTabs.forEach((tabId) => workspace.discardFileTab(tabId))
  }
  workspace.selectPath(parent)
  await focusPath(parent)
}

function fileTargetForPath(path) {
  return workspace.fileTarget({
    kind: 'file',
    path,
    name: path.split('/').filter(Boolean).at(-1) || path
  })
}

watch(
  [() => chat.current?.id || null, () => chat.current?.projectId || null],
  ([sessionId, projectId]) => {
    selectedRootIdentity.value = sessionId
    projectOverview.value = false
    void loadProjectSessions(projectId).catch(() => {})
  },
  { immediate: true }
)

watch(managedSessionOptions, (options) => {
  if (!options.some((option) => option.value === selectedRootIdentity.value)) {
    selectedRootIdentity.value = chat.current?.id || null
    projectOverview.value = false
  }
})

watch(
  [
    () => chat.selectedSessionId || null,
    () => chat.current?.id || null,
    () => chat.current?.projectId || null,
    () => selectedRootIdentity.value,
    () => chat.current?.currentDirectory || null,
    () => chat.activeContext?.currentDirectory || null,
    () => workspaceContextKey(activeWorkspaceContext.value),
    () => controlCenter.requestGeneration,
    () => controlCenter.ready
  ],
  ([selectedSessionId, currentSessionId, , selectedManagedSessionId, currentDirectory, draftCurrentDirectory]) => {
    const sessionId = selectedSessionId || currentSessionId
    const selectedDirectory = currentSessionId === sessionId
      ? currentDirectory
      : sessionId ? null : draftCurrentDirectory
    workspace.configure({
      sessionId,
      rootIdentity: sessionId ? selectedManagedSessionId || sessionId : null,
      currentDirectory: selectedDirectory || selectedExecutionAgent.value?.homeDirectory || '/',
      workspaceContext: activeWorkspaceContext.value,
      scope: props.scope
    })
    if (
      sessionId
      && currentSessionId === sessionId
      && controlCenter.sessionId === sessionId
      && controlCenter.ready
    ) workspace.restoreView(controlCenter.fileViews[props.scope])
    workspace.load(workspace.directoryPath)
  },
  { immediate: true }
)

watch(() => workspace.contextVersion, () => { treeFocusPath.value = workspace.directoryPath })
watch(() => workspace.filter, async () => {
  await nextTick()
  if (!visibleTreeItems().some((item) => item.dataset.filePath === treeFocusPath.value)) {
    treeFocusPath.value = workspace.directoryPath
  }
})
</script>
