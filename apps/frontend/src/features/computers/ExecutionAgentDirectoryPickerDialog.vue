<template>
  <q-dialog
    :transition-duration="0"
    :model-value="modelValue"
    :maximized="$q.screen.lt.sm"
    @hide="close"
  >
    <q-card class="integration-dialog app-surface column no-wrap">
      <q-card-section class="row items-center no-wrap q-gutter-md">
        <q-avatar color="primary" text-color="white" icon="folder_open" />
        <div class="col">
          <div class="text-h6">Ordner auswählen</div>
          <div class="text-body2 app-muted">Wähle einen Ordner auf dem verbundenen Computer.</div>
        </div>
        <q-btn flat round dense icon="close" aria-label="Dialog schließen" @click="close" />
      </q-card-section>

      <q-separator />
      <q-toolbar class="q-px-sm">
        <q-btn
          flat
          round
          dense
          icon="arrow_upward"
          aria-label="Zum übergeordneten Ordner"
          :disable="currentPath === '/' || loading"
          @click="load(parentPath(currentPath))"
        />
        <q-breadcrumbs class="col q-px-sm" gutter="xs" active-color="primary">
          <template #separator><q-icon name="chevron_right" size="18px" /></template>
          <q-breadcrumbs-el
            v-for="crumb in breadcrumbs"
            :key="crumb.path"
            :label="crumb.label"
            :class="{ 'cursor-pointer': crumb.path !== currentPath && !loading }"
            @click="crumb.path !== currentPath && !loading && load(crumb.path)"
          />
        </q-breadcrumbs>
        <q-btn
          flat
          round
          dense
          icon="create_new_folder"
          aria-label="Neuen Ordner anlegen"
          :disable="loading || loadFailed || !executionAgentId"
          @click="createDirectory"
        />
        <q-btn
          flat
          round
          dense
          icon="refresh"
          aria-label="Ordner aktualisieren"
          :loading="loading"
          @click="load(currentPath)"
        />
      </q-toolbar>
      <q-separator />
      <q-linear-progress v-if="loading" indeterminate color="primary" aria-label="Ordner werden geladen" />

      <q-card-section class="col scroll q-pa-none">
        <div v-if="loadFailed" class="q-pa-lg text-center" role="alert">
          <q-icon name="cloud_off" size="40px" class="app-muted" />
          <div class="q-mt-sm">Der Ordner konnte nicht geladen werden.</div>
          <q-btn class="q-mt-sm" flat no-caps color="primary" label="Erneut versuchen" @click="load(currentPath)" />
        </div>
        <q-list v-else separator>
          <q-item
            v-for="entry in directories"
            :key="entry.path"
            clickable
            :disable="loading"
            v-ripple
            @click="load(entry.path)"
          >
            <q-item-section avatar><q-icon name="folder" color="primary" /></q-item-section>
            <q-item-section>
              <q-item-label>{{ entry.name }}</q-item-label>
              <q-item-label caption>{{ entry.path }}</q-item-label>
            </q-item-section>
            <q-item-section side><q-icon name="chevron_right" /></q-item-section>
          </q-item>
          <q-item v-if="!loading && !directories.length">
            <q-item-section class="app-muted text-center">Keine Unterordner vorhanden.</q-item-section>
          </q-item>
          <q-item v-if="nextOffset !== null" clickable :disable="loading" @click="load(currentPath, true)">
            <q-item-section class="text-primary text-center">Weitere Ordner laden</q-item-section>
          </q-item>
        </q-list>
      </q-card-section>

      <q-separator />
      <q-card-actions align="right" class="q-pa-md">
        <q-btn flat no-caps label="Abbrechen" @click="close" />
        <q-btn
          unelevated
          no-caps
          color="primary"
          label="Diesen Ordner auswählen"
          :disable="loading || loadFailed"
          @click="select"
        />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { api } from 'src/boot/axios'
import { absoluteComputerPath, parentPath } from 'src/features/files/state'

const props = defineProps({
  modelValue: Boolean,
  executionAgentId: { type: String, default: null },
  initialPath: { type: String, default: '/' }
})
const emit = defineEmits(['update:modelValue', 'select'])
const $q = useQuasar()

const currentPath = ref('/')
const directories = ref([])
const nextOffset = ref(null)
const loading = ref(false)
const loadFailed = ref(false)
let requestVersion = 0

const breadcrumbs = computed(() => {
  if (/^[A-Z]:\//u.test(currentPath.value)) {
    const drive = currentPath.value.slice(0, 3)
    const crumbs = [{ label: drive, path: drive }]
    let path = drive.replace(/\/$/u, '')
    for (const part of currentPath.value.slice(3).split('/').filter(Boolean)) {
      path += `/${part}`
      crumbs.push({ label: part, path })
    }
    return crumbs
  }
  const crumbs = [{ label: '/', path: '/' }]
  let path = ''
  for (const part of currentPath.value.split('/').filter(Boolean)) {
    path += `/${part}`
    crumbs.push({ label: part, path })
  }
  return crumbs
})

function startPath() {
  const path = String(props.initialPath || '').trim()
  if (/^[A-Z]:\/$/u.test(path)) return path
  return absoluteComputerPath(path) ? path.replace(/\/+$/u, '') || '/' : '/'
}

async function load(path, append = false) {
  if (!props.executionAgentId) return
  const version = ++requestVersion
  loading.value = true
  if (!append) loadFailed.value = false
  try {
    const { data } = await api.get('/workspace/entries', {
      params: {
        scope: 'filesystem',
        execution_agent_id: props.executionAgentId,
        path,
        ...(append ? { offset: nextOffset.value } : {})
      },
      suppressErrorToast: true
    })
    if (version !== requestVersion) return
    currentPath.value = data.path
    const items = data.entries.filter((entry) => entry.kind === 'directory')
    directories.value = append ? [...directories.value, ...items] : items
    nextOffset.value = data.hasMore ? data.nextOffset : null
    loadFailed.value = false
  } catch {
    if (version === requestVersion) loadFailed.value = true
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

function createDirectory() {
  $q.dialog({
    transitionDuration: 0,
    title: 'Neuen Ordner anlegen',
    prompt: {
      model: '',
      label: 'Ordnername',
      dense: true,
      isValid: (value) => Boolean(value.trim())
    },
    cancel: { label: 'Abbrechen', flat: true, noCaps: true },
    ok: { label: 'Anlegen', color: 'primary', unelevated: true, noCaps: true }
  }).onOk(async (name) => {
    const version = ++requestVersion
    loading.value = true
    try {
      const { data } = await api.post('/workspace/directories', {
        parent: currentPath.value,
        name: name.trim()
      }, {
        params: { scope: 'filesystem', execution_agent_id: props.executionAgentId }
      })
      $q.notify({ type: 'positive', message: 'Ordner wurde angelegt.' })
      if (version === requestVersion && props.modelValue) await load(data.path)
    } catch {
      // The central API interceptor displays the error.
    } finally {
      if (version === requestVersion) loading.value = false
    }
  })
}

function close() {
  emit('update:modelValue', false)
}

function select() {
  emit('select', currentPath.value)
  close()
}

watch(
  () => [props.modelValue, props.executionAgentId],
  ([open]) => {
    if (!open) return
    currentPath.value = startPath()
    directories.value = []
    nextOffset.value = null
    loadFailed.value = false
    void load(currentPath.value)
  },
  { immediate: true }
)
</script>
