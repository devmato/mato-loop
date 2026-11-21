<template>
  <AppPage :title="isTrash ? 'Papierkorb' : 'Archiv'" :subtitle="isTrash ? 'Gelöschte Projekte und Sessions bis zur angezeigten Frist wiederherstellen oder endgültig löschen.' : 'Archivierte Gespräche bleiben erhalten. Zum Weiterarbeiten wiederherstellen.'">
    <q-banner v-if="archive.failed" rounded class="app-surface-tonal q-mb-md">
      {{ isTrash ? 'Der Papierkorb' : 'Das Archiv' }} konnte nicht geladen werden.
      <template #action><q-btn flat no-caps label="Erneut laden" @click="load" /></template>
    </q-banner>
    <q-spinner v-if="archive.loading" color="primary" size="24px" :aria-label="isTrash ? 'Papierkorb wird geladen' : 'Archiv wird geladen'" />
    <template v-else>
      <h2 class="text-subtitle2 app-muted q-mb-sm">Projekte</h2>
      <q-list v-if="projectItems.length" class="archive-list app-surface q-pa-xs q-mb-lg">
        <q-expansion-item v-for="project in projectItems" :key="project.id" :duration="0" switch-toggle-side dense header-class="sidebar-row rounded-borders q-px-sm q-py-xs" @show="loadSessions(project.id)">
          <template #header>
            <q-item-section avatar class="archive-icon"><q-icon name="folder_open" size="20px" /></q-item-section>
            <q-item-section class="text-body2 ellipsis">{{ project.name }}</q-item-section>
            <q-item-section side>
              <ArchiveEntryActions :title="project.name" label="Projekt" :trash="isTrash" :expired="isTrash && expired(project)" :disabled="Boolean(busy)" :restoring="restoring === project.id" @restore="isTrash ? restoreTrash('projects', project) : restoreProject(project)" @delete="deleteEntry('projects', project)" />
            </q-item-section>
          </template>
          <div class="archive-children q-ml-md q-pl-sm">
            <p v-if="isTrash" class="text-caption app-muted q-ma-sm">{{ expiryLabel(project) }}</p>
            <q-spinner v-if="loadingProject === project.id" color="primary" size="20px" class="q-ma-sm" aria-label="Sessions werden geladen" />
            <q-list v-else dense>
              <q-item v-for="session in projectSessions(project.id)" :key="session.id" :clickable="!isTrash" @click="!isTrash && openSession(session)" class="sidebar-row rounded-borders q-px-sm q-py-xs">
                <q-item-section avatar class="archive-icon"><q-icon name="chat_bubble_outline" size="18px" /></q-item-section>
                <q-item-section class="archive-title">
                  <q-item-label class="text-body2 ellipsis">{{ session.title }}</q-item-label>
                  <q-item-label v-if="isTrash && session.deleteAt" caption>Auch einzeln gelöscht · {{ expiryLabel(session) }}</q-item-label>
                  <q-item-label v-else-if="!isTrash && session.archivedAt" caption>Auch einzeln archiviert</q-item-label>
                </q-item-section>
                <q-item-section v-if="!isTrash || session.deleteAt" side>
                  <ArchiveEntryActions :title="session.title" label="Session" :trash="isTrash" :restorable="false" :disabled="Boolean(busy)" @delete="deleteEntry('sessions', { ...session, projectId: project.id })" />
                </q-item-section>
              </q-item>
              <q-item v-if="!projectSessions(project.id).length"><q-item-section class="text-caption app-muted">Keine Sessions.</q-item-section></q-item>
              <q-btn v-if="projectNextCursor(project.id)" flat no-caps label="Weitere Sessions" @click="loadSessions(project.id, true)" />
            </q-list>
          </div>
        </q-expansion-item>
      </q-list>
      <p v-else class="app-muted">{{ isTrash ? 'Keine gelöschten Projekte.' : 'Keine archivierten Projekte.' }}</p>
      <h2 class="text-subtitle2 app-muted q-mb-sm">{{ isTrash ? 'Einzeln gelöschte Sessions' : 'Einzeln archivierte Sessions' }}</h2>
      <q-list v-if="sessionItems.length" class="archive-list app-surface q-pa-xs">
        <q-item v-for="session in sessionItems" :key="session.id" :clickable="!isTrash" @click="!isTrash && openSession(session)" class="sidebar-row rounded-borders q-px-sm q-py-xs">
          <q-item-section avatar class="archive-icon"><q-icon name="chat_bubble_outline" size="18px" /></q-item-section>
          <q-item-section class="archive-title">
            <q-item-label class="text-body2 ellipsis">{{ session.title }}</q-item-label>
            <q-item-label v-if="session.projectId" caption>{{ session.projectName || 'Aus einem Projekt' }}</q-item-label>
            <q-item-label v-if="isTrash" caption>{{ expiryLabel(session) }}</q-item-label>
          </q-item-section>
          <q-item-section side>
            <ArchiveEntryActions :title="session.title" label="Session" :trash="isTrash" :expired="isTrash && expired(session)" :disabled="Boolean(busy)" :restoring="restoring === session.id" @restore="isTrash ? restoreTrash('sessions', session) : restoreSession(session)" @delete="deleteEntry('sessions', session)" />
          </q-item-section>
        </q-item>
      </q-list>
      <p v-else class="app-muted">{{ isTrash ? 'Keine einzeln gelöschten Sessions.' : 'Keine einzeln archivierten Sessions.' }}</p>
      <q-btn v-if="!isTrash && archive.sessionLists.sessions?.nextCursor" flat no-caps label="Weitere Sessions" @click="loadSessions(null, true)" />
    </template>
  </AppPage>
</template>

<script setup>
import { computed, inject, onBeforeUnmount, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useRouter } from 'vue-router'
import AppPage from 'src/shared/ui/AppPage.vue'
import ArchiveEntryActions from './ArchiveEntryActions.vue'
import { useArchiveStore } from './store'
import { useChatStore } from 'src/features/chat/store'
import { useProjectStore } from 'stores/projects'
import { useAutomationStore } from 'stores/automations'

const props = defineProps({ mode: { type: String, default: 'archive' } })
const isTrash = computed(() => props.mode === 'trash')
const deleteActions = inject('deleteActions', null)
const archive = useArchiveStore()
const chat = useChatStore()
const projects = useProjectStore()
const automations = useAutomationStore()
const $q = useQuasar()
const router = useRouter()
const restoring = ref(null)
const deleting = ref(null)
const loadingProject = ref(null)
const busy = computed(() => restoring.value || deleting.value)
const projectItems = computed(() => isTrash.value ? archive.trash.projects : archive.projects)
const sessionItems = computed(() => isTrash.value ? archive.trash.sessions : archive.sessionLists.sessions?.items || [])
const projectList = (id) => isTrash.value ? archive.trashSessionLists[id] : archive.sessionLists[id]
const projectSessions = (id) => projectList(id)?.items || []
const projectNextCursor = (id) => projectList(id)?.nextCursor
const now = ref(Date.now())
const clock = window.setInterval(() => { now.value = Date.now() }, 1000)
onBeforeUnmount(() => window.clearInterval(clock))
const expired = (item) => Date.parse(item.deleteAt) <= now.value
const expiryLabel = (item) => `${expired(item) ? 'Frist abgelaufen' : 'Wiederherstellbar bis'}: ${new Date(item.deleteAt).toLocaleString('de-DE')}`
const sessionRoute = (session) => ({ name: 'chat', query: { session: session.id, ...(session.projectId ? { project: session.projectId } : {}) } })
const openSession = (session) => router.push(sessionRoute(session))
const load = () => archive.load(props.mode)

async function restoreTrash(kind, item) {
  if (busy.value || expired(item)) return
  restoring.value = item.id
  try {
    const result = await archive.restoreTrash(kind, item.id)
    await Promise.all([load(), projects.fetchAll(), chat.refreshSessions(null, kind === 'sessions' ? item.projectId || null : item.id), automations.fetchAutomations()])
    if (result?.worktreeRestored === false) {
      $q.notify({ type: 'warning', message: 'Session wiederhergestellt. Der Worktree konnte nicht neu angelegt werden. Öffne die Git-App, um es erneut zu versuchen.' })
    } else {
      $q.notify({ type: 'positive', message: kind === 'projects' ? 'Projekt wiederhergestellt.' : 'Session wiederhergestellt.' })
    }
  } catch { /* Axios displays errors. */ } finally { restoring.value = null }
}

async function deleteEntry(kind, item) {
  if (busy.value) return
  deleting.value = item.id
  try {
    if (await deleteActions[kind](item, { permanent: isTrash.value })) {
      const expanded = Object.keys(archive.sessionLists).filter(id => id !== 'sessions' && id !== item.id)
      await load()
      if (!isTrash.value) await Promise.all(expanded.map(id => archive.loadSessions(id)))
    }
  } catch { /* Axios displays errors. */ } finally { deleting.value = null }
}

async function loadSessions(id, more = false) {
  loadingProject.value = id
  try {
    if (isTrash.value) await archive.loadTrashSessions(id, more)
    else await archive.loadSessions(id, more)
  } catch { /* Axios displays errors. */ } finally { loadingProject.value = null }
}

async function restoreSession(session) {
  if (busy.value) return
  restoring.value = session.id
  try {
    await chat.setArchived(session.id, false)
    const expanded = Object.keys(archive.sessionLists).filter(id => id !== 'sessions')
    await Promise.all([load(), automations.fetchAutomations(), ...expanded.map(id => archive.loadSessions(id))])
    $q.notify({ type: 'positive', message: 'Session wiederhergestellt' })
  } catch { /* Axios displays errors. */ } finally { restoring.value = null }
}

async function restoreProject(project) {
  if (busy.value) return
  restoring.value = project.id
  try {
    await projects.setArchived(project.id, false)
    await Promise.all([load(), automations.fetchAutomations(), chat.refreshSessions(null, project.id)])
    if (chat.current?.projectId === project.id) await chat.refreshSession(chat.current.id)
    $q.notify({ type: 'positive', message: 'Projekt wiederhergestellt' })
  } catch { /* Axios displays errors. */ } finally { restoring.value = null }
}
watch(() => props.mode, load, { immediate: true })
</script>

<style scoped>
.archive-list { border-radius: 8px; }
.archive-icon { min-width: 28px; padding-right: 8px; color: var(--app-muted); }
.archive-title { min-width: 0; }
.archive-children { border-left: 1px solid var(--app-surface-border); }
</style>
