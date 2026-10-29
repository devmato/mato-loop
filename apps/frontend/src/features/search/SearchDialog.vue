<template>
  <q-dialog
    :transition-duration="0"
    v-model="search.dialogOpen"
    :position="$q.screen.lt.md ? 'standard' : 'top'"
    :maximized="$q.screen.lt.md"
    aria-labelledby="global-search-title"
    @show="focusSearchInput"
  >
    <q-card class="global-search-dialog app-surface column no-wrap">
      <q-card-section class="row items-center no-wrap q-pb-sm">
        <div>
          <div id="global-search-title" class="text-h6">Suche</div>
          <div class="text-caption app-muted">Projekte, Sessions und Nachrichten durchsuchen</div>
        </div>
        <q-space />
        <q-btn flat round dense icon="close" aria-label="Suche schließen" v-close-popup />
      </q-card-section>

      <q-form class="q-px-md q-pb-md" @submit.prevent="runSearch">
        <div class="row items-start q-col-gutter-sm">
          <div class="col">
            <q-input dense
              ref="searchInput"
              v-model="search.query"
              outlined
              autofocus
              clearable
              label="Suchbegriff"
              :error="showQueryError"
              error-message="Bitte mindestens zwei Zeichen eingeben"
            >
              <template #prepend><q-icon name="search" /></template>
            </q-input>
          </div>
          <div class="col-auto">
            <q-btn
              unelevated
              no-caps
              color="primary"
              icon="search"
              label="Suchen"
              type="submit"
              class="global-search-dialog__submit"
              :loading="search.loading"
              :disable="normalizedQuery.length < 2"
            />
          </div>
        </div>
      </q-form>

      <q-separator />

      <q-scroll-area class="col">
        <div class="q-pa-md" aria-live="polite">
          <div v-if="search.loading" role="status" aria-label="Suchergebnisse werden geladen">
            <q-skeleton v-for="index in 4" :key="index" type="rect" height="64px" class="q-mb-sm" />
          </div>

          <AppStateMessage
            v-else-if="search.loadFailed"
            class="q-py-xl"
            role="alert"
            icon="error_outline"
            icon-color="negative"
            title="Suche konnte nicht geladen werden"
          >
            <template #actions>
              <q-btn flat no-caps color="primary" icon="refresh" label="Erneut versuchen" @click="runSearch" />
            </template>
          </AppStateMessage>

          <template v-else-if="search.searchedQuery">
            <q-banner
              v-if="search.strategy === 'lexical'"
              dense
              rounded
              class="app-surface-tonal q-mb-md"
            >
              <template #avatar><q-icon name="info_outline" color="primary" /></template>
              Die semantische Suche ist derzeit nicht verfügbar. Es werden exakte Texttreffer angezeigt.
            </q-banner>

            <section v-if="search.projects.length" aria-labelledby="project-search-results">
              <div id="project-search-results" class="text-subtitle2 app-muted q-mb-xs">
                Projekte
              </div>
              <q-list bordered separator class="rounded-borders">
                <q-item
                  v-for="project in search.projects"
                  :key="project.id"
                  clickable
                  v-ripple
                  @click="selectProject(project)"
                >
                  <q-item-section avatar><q-icon name="folder_open" color="primary" /></q-item-section>
                  <q-item-section>
                    <q-item-label>{{ project.name }}</q-item-label>
                    <q-item-label caption>
                      {{ project.sessionCount }} {{ project.sessionCount === 1 ? 'Session' : 'Sessions' }}
                    </q-item-label>
                  </q-item-section>
                  <q-item-section side><q-icon name="arrow_forward" /></q-item-section>
                </q-item>
              </q-list>
            </section>

            <section v-if="search.sessions.length" class="q-mt-md" aria-labelledby="session-search-results">
              <div id="session-search-results" class="text-subtitle2 app-muted q-mb-xs">
                Sessions
              </div>
              <q-list bordered separator class="rounded-borders">
                <q-item
                  v-for="session in search.sessions"
                  :key="session.id"
                  clickable
                  v-ripple
                  @click="selectSession(session)"
                >
                  <q-item-section avatar>
                    <q-icon :name="session.matchType === 'message' ? 'chat' : 'title'" color="primary" />
                  </q-item-section>
                  <q-item-section>
                    <q-item-label>{{ session.title }}</q-item-label>
                    <q-item-label v-if="session.projectName" caption>{{ session.projectName }}</q-item-label>
                    <q-item-label v-if="session.messageExcerpt" caption lines="2" class="q-mt-xs">
                      {{ session.messageExcerpt }}
                    </q-item-label>
                  </q-item-section>
                  <q-item-section side top>
                    <q-item-label v-if="session.matchedAt" caption>
                      {{ formatRelativeDateTime(session.matchedAt) }}
                    </q-item-label>
                    <q-icon name="arrow_forward" class="q-mt-xs" />
                  </q-item-section>
                </q-item>
              </q-list>
            </section>

            <AppStateMessage
              v-if="!hasResults"
              class="q-py-xl"
              icon="search_off"
              title="Keine Treffer gefunden"
              description="Versuche einen anderen Suchbegriff."
            />
          </template>

          <AppStateMessage
            v-else
            class="q-py-xl"
            icon="manage_search"
            title="Was möchtest du wiederfinden?"
            description="Gib mindestens zwei Zeichen ein und starte die Suche."
          />
        </div>
      </q-scroll-area>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import { useSearchStore } from 'stores/search'
import { formatRelativeDateTime } from 'src/utils/date-time'

defineOptions({ name: 'SearchDialog' })

const emit = defineEmits(['open-project', 'open-session'])
const search = useSearchStore()
const searchInput = ref(null)
const submitted = ref(false)
const normalizedQuery = computed(() => String(search.query || '').trim())
const showQueryError = computed(() => submitted.value && normalizedQuery.value.length < 2)
const hasResults = computed(() => search.projects.length > 0 || search.sessions.length > 0)

async function focusSearchInput() {
  await nextTick()
  searchInput.value?.focus()
  searchInput.value?.select()
}

async function runSearch() {
  submitted.value = true
  if (normalizedQuery.value.length < 2) return
  try {
    await search.run()
  } catch {
    // The central API interceptor displays the error and the dialog exposes a retry action.
  }
}

function selectProject(project) {
  search.dialogOpen = false
  emit('open-project', project)
}

function selectSession(session) {
  search.dialogOpen = false
  emit('open-session', session)
}

watch(
  () => search.query,
  (value) => {
    submitted.value = false
    if (String(value || '').trim() !== search.searchedQuery) search.clearResults()
  }
)
</script>

<style scoped>
.global-search-dialog {
  width: 720px;
  max-width: 96vw;
  height: min(640px, 80vh);
  max-height: 80vh;
}

.global-search-dialog__submit {
  min-height: 56px;
}

@media (max-width: 1023px) {
  .global-search-dialog {
    width: 100%;
    max-width: none;
    height: 100%;
    max-height: none;
    border: 0;
    border-radius: 0;
    padding-top: env(safe-area-inset-top);
    padding-bottom: env(safe-area-inset-bottom);
  }
}
</style>
