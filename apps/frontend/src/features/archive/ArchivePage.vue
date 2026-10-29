<template>
  <AppPage title="Archiv" subtitle="Archivierte Gespräche bleiben erhalten. Zum Weiterarbeiten wiederherstellen.">
    <q-banner v-if="archive.failed" rounded class="app-surface-tonal q-mb-md">
      Das Archiv konnte nicht geladen werden.
      <template #action><q-btn flat no-caps label="Erneut laden" @click="archive.load" /></template>
    </q-banner>
    <q-spinner v-if="archive.loading" color="primary" size="24px" aria-label="Archiv wird geladen" />
    <template v-else>
      <h2 class="text-subtitle1"><q-icon name="folder_open" class="q-mr-sm" />Projekte</h2>
      <q-list bordered separator class="rounded-borders q-mb-lg" v-if="archive.projects.length">
        <q-expansion-item v-for="project in archive.projects" :key="project.id" :duration="0" @show="loadSessions(project.id)">
          <template #header>
            <q-item-section>{{ project.name }}</q-item-section>
            <q-item-section side><q-btn flat round icon="unarchive" :loading="restoring === project.id" aria-label="Projekt wiederherstellen" @click.stop="restoreProject(project)" /></q-item-section>
          </template>
          <q-list dense>
            <q-item v-for="session in archive.sessionLists[project.id]?.items || []" :key="session.id" clickable :to="sessionRoute(session)">
              <q-item-section><q-item-label>{{ session.title }}</q-item-label><q-item-label v-if="session.archivedAt" caption>Auch einzeln archiviert</q-item-label></q-item-section>
            </q-item>
            <q-item v-if="archive.sessionLists[project.id]?.nextCursor"><q-btn flat no-caps label="Weitere Sessions" @click="loadSessions(project.id, true)" /></q-item>
          </q-list>
        </q-expansion-item>
      </q-list>
      <p v-else class="app-muted">Keine archivierten Projekte.</p>
      <h2 class="text-subtitle1"><q-icon name="chat_bubble_outline" class="q-mr-sm" />Einzeln archivierte Sessions</h2>
      <q-list v-if="archive.sessionLists.sessions?.items.length" bordered separator class="rounded-borders">
        <q-item v-for="session in archive.sessionLists.sessions.items" :key="session.id" clickable :to="sessionRoute(session)">
          <q-item-section><q-item-label>{{ session.title }}</q-item-label><q-item-label v-if="session.projectId" caption>Aus einem Projekt</q-item-label></q-item-section>
          <q-item-section side><q-btn flat round icon="unarchive" :loading="restoring === session.id" aria-label="Session wiederherstellen" @click.stop.prevent="restoreSession(session)" /></q-item-section>
        </q-item>
      </q-list>
      <p v-else class="app-muted">Keine einzeln archivierten Sessions.</p>
      <q-btn v-if="archive.sessionLists.sessions?.nextCursor" flat no-caps label="Weitere Sessions" @click="loadSessions(null, true)" />
    </template>
  </AppPage>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useQuasar } from 'quasar'
import AppPage from 'src/shared/ui/AppPage.vue'
import { useArchiveStore } from './store'
import { useChatStore } from 'src/features/chat/store'
import { useProjectStore } from 'stores/projects'
import { useAutomationStore } from 'stores/automations'

const archive = useArchiveStore()
const chat = useChatStore()
const projects = useProjectStore()
const automations = useAutomationStore()
const $q = useQuasar()
const restoring = ref(null)
const sessionRoute = (session) => ({ name: 'chat', query: { session: session.id, ...(session.projectId ? { project: session.projectId } : {}) } })

async function loadSessions(id, more = false) {
  try { await archive.loadSessions(id, more) } catch { /* Axios displays errors. */ }
}

async function restoreSession(session) {
  if (restoring.value) return
  restoring.value = session.id
  try {
    await chat.setArchived(session.id, false)
    await Promise.all([archive.load(), automations.fetchAutomations()])
    $q.notify({ type: 'positive', message: 'Session wiederhergestellt' })
  } catch { /* Axios displays errors. */ } finally { restoring.value = null }
}

async function restoreProject(project) {
  if (restoring.value) return
  restoring.value = project.id
  try {
    await projects.setArchived(project.id, false)
    await Promise.all([archive.load(), automations.fetchAutomations(), chat.refreshSessions(null, project.id)])
    if (chat.current?.projectId === project.id) await chat.refreshSession(chat.current.id)
    $q.notify({ type: 'positive', message: 'Projekt wiederhergestellt' })
  } catch { /* Axios displays errors. */ } finally { restoring.value = null }
}
onMounted(() => archive.load())
</script>
