<template>
  <section class="memory-page">
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Memory</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">
          Dauerhafte Erinnerungen durchsuchen, pflegen und optional mit Dreaming konsolidieren.
        </p>
      </div>
      <q-btn
        v-if="section === 'memories'"
        unelevated
        no-caps
        color="primary"
        icon="add"
        label="Erinnerung hinzufügen"
        @click="openCreateDialog"
      />
    </header>

    <q-tabs
      v-model="section"
      dense
      no-caps
      align="left"
      active-color="primary"
      indicator-color="primary"
      class="memory-tabs q-mb-lg"
    >
      <q-tab name="memories" icon="psychology" label="Erinnerungen" />
      <q-tab name="dreaming" icon="auto_awesome" label="Dreaming" />
    </q-tabs>

    <q-tab-panels
      v-model="section"
      class="memory-panels bg-transparent"
    >
      <q-tab-panel name="memories" class="q-pa-none">
        <q-card flat class="app-surface memory-access-card q-mb-md">
          <q-card-section class="memory-access-header row items-center no-wrap q-gutter-md">
            <q-avatar rounded color="primary" text-color="white" icon="psychology" />
            <div class="col">
              <div class="text-subtitle1 text-weight-medium">Memory-Zugriff</div>
              <div class="text-body2 app-muted">
                Steuert, ob frühere Inhalte für Modellanfragen verwendet und neue Erinnerungen gespeichert werden.
              </div>
            </div>
            <q-toggle dense
              :model-value="settingsForm.enabled"
              color="primary"
              label="Memory aktiv"
              :disable="!settingsLoaded || memories.settingsLoading"
              @update:model-value="toggleMemory"
            />
          </q-card-section>
          <q-card-section class="q-pt-none">
            <q-banner rounded class="app-surface-tonal">
              <template #avatar><q-icon name="info_outline" color="primary" /></template>
              Erinnerungen mit „Immer im Kontext“ werden bei erlaubten Anfragen vollständig eingefügt. Erinnerungen mit „Bei passender Anfrage“ bleiben dauerhaft gespeichert und erscheinen automatisch nur als kurze relevante Treffer; vollständige Details werden bei Bedarf geladen. Relevante frühere Sessions mit vollem Memory-Zugriff können als begrenzte Auszüge erscheinen. Alle Erinnerungen bleiben auch bei ausgeschaltetem Memory oder Dreaming hier verwaltbar.
            </q-banner>
          </q-card-section>
          <q-inner-loading :showing="memories.settingsLoading && !settingsLoaded" />
        </q-card>

        <q-card flat class="app-surface memory-filter-card q-mb-md">
          <q-card-section>
            <div class="memory-category-filter row items-center q-gutter-xs q-mb-md">
              <q-chip
                v-for="option in categoryFilterOptions"
                :key="option.value || 'all'"
                clickable
                :outline="filters.category !== option.value"
                :color="filters.category === option.value ? 'primary' : undefined"
                :text-color="filters.category === option.value ? 'white' : undefined"
                :icon="option.icon"
                @click="setCategoryFilter(option.value)"
              >
                {{ option.label }}
              </q-chip>
            </div>
            <div class="memory-filters">
              <q-input
                v-model="filters.query"
                outlined
                dense
                clearable
                debounce="350"
                label="Erinnerungen durchsuchen"
                class="memory-search"
                @update:model-value="applyFilters"
              >
                <template #prepend><q-icon name="search" /></template>
              </q-input>
              <AppSelect
                v-model="filters.sourceType"
                outlined
                dense
                clearable
                emit-value
                map-options
                label="Quelle"
                :options="sourceTypeOptions"
                @update:model-value="applyFilters"
              />
              <AppSelect
                v-if="auth.modelTrustEnabled"
                v-model="filters.audience"
                outlined
                dense
                clearable
                emit-value
                map-options
                label="Verfügbar für"
                :options="audienceOptions"
                @update:model-value="applyFilters"
              />
              <AppSelect
                v-model="filters.pinned"
                outlined
                dense
                clearable
                emit-value
                map-options
                label="Berücksichtigung"
                :options="pinnedOptions"
                @update:model-value="applyFilters"
              />
              <q-btn
                flat
                round
                icon="filter_alt_off"
                aria-label="Filter zurücksetzen"
                :disable="!filtersActive"
                @click="resetFilters"
              >
                <q-tooltip>Filter zurücksetzen</q-tooltip>
              </q-btn>
            </div>
          </q-card-section>
        </q-card>

        <q-card flat class="app-surface memory-list-card">
          <q-card-section v-if="!memories.loading && !memories.items.length" class="memory-empty">
            <AppStateMessage
              icon="psychology_alt"
              :title="filtersActive ? 'Keine passenden Erinnerungen' : 'Noch keine Erinnerungen'"
              :description="filtersActive ? 'Passe die Suche oder die Filter an.' : 'Füge eine Erinnerung manuell hinzu oder nutze optional Dreaming.'"
            />
          </q-card-section>

          <q-list v-else separator>
            <q-item v-for="item in memories.items" :key="item.id" class="memory-item">
              <q-item-section>
                <div class="memory-item__chips row items-center q-gutter-xs q-mb-sm">
                  <q-chip
                    v-if="!item.seenAt"
                    dense
                    square
                    color="positive"
                    text-color="white"
                    icon="fiber_new"
                  >
                    Neu
                  </q-chip>
                  <q-chip
                    dense
                    square
                    :color="categoryMeta(item.category).color"
                    text-color="white"
                    :icon="categoryMeta(item.category).icon"
                  >
                    {{ categoryMeta(item.category).label }}
                  </q-chip>
                  <q-chip dense square :icon="sourceMeta(item.sourceType).icon">
                    {{ sourceMeta(item.sourceType).label }}
                  </q-chip>
                  <q-chip
                    dense
                    square
                    :color="audienceMeta(item.audience).color"
                    text-color="white"
                    :icon="audienceMeta(item.audience).icon"
                  >
                    {{ audienceMeta(item.audience).label }}
                  </q-chip>
                  <q-badge
                    outline
                    color="primary"
                    class="memory-status-badge"
                    :aria-label="pinnedLabel(item.pinned)"
                  >
                    <q-icon :name="item.pinned ? 'push_pin' : 'bookmark_border'" size="14px" />
                    {{ pinnedLabel(item.pinned) }}
                    <q-tooltip>{{ item.pinned ? 'Wird bei erlaubten Anfragen vollständig in den Kontext eingefügt.' : 'Erscheint automatisch nur als kurzer relevanter Treffer; vollständige Details werden bei Bedarf geladen.' }}</q-tooltip>
                  </q-badge>
                </div>
                <div class="memory-item__content text-body1">{{ item.content }}</div>
                <div class="memory-item__meta row items-center q-gutter-x-md q-gutter-y-xs q-mt-sm text-caption app-muted">
                  <router-link
                    v-if="item.sourceSession"
                    :to="{ name: 'chat', query: { session: item.sourceSession.id } }"
                    class="memory-source-link"
                  >
                    <q-icon name="forum" /> {{ item.sourceSession.title }}
                  </router-link>
                  <span>Aktualisiert {{ formatDate(item.updatedAt) }}</span>
                </div>
              </q-item-section>
              <q-item-section side top class="memory-item__actions">
                <q-btn
                  flat
                  round
                  dense
                  icon="push_pin"
                  :color="item.pinned ? 'primary' : undefined"
                  :aria-label="item.pinned ? 'Auf „Bei passender Anfrage“ setzen' : 'Auf „Immer im Kontext“ setzen'"
                  @click="togglePinned(item)"
                >
                  <q-tooltip>{{ item.pinned ? 'Auf „Bei passender Anfrage“ setzen' : 'Auf „Immer im Kontext“ setzen' }}</q-tooltip>
                </q-btn>
                <q-btn flat round dense icon="edit" aria-label="Erinnerung bearbeiten" @click="openEditDialog(item)">
                  <q-tooltip>Bearbeiten</q-tooltip>
                </q-btn>
                <q-btn flat round dense color="negative" icon="delete_outline" aria-label="Erinnerung löschen" @click="removeMemory(item)">
                  <q-tooltip>Löschen</q-tooltip>
                </q-btn>
              </q-item-section>
            </q-item>
          </q-list>

          <q-separator v-if="memories.total" />
          <q-card-actions v-if="memories.total" class="memory-pagination q-pa-md">
            <span class="text-caption app-muted">{{ resultRange }} von {{ memories.total }}</span>
            <q-space />
            <AppSelect
              v-model="pageSize"
              dense
              outlined
              emit-value
              map-options
              options-dense
              label="Pro Seite"
              :options="pageSizeOptions"
              @update:model-value="changePageSize"
            />
            <q-pagination
              v-model="page"
              color="primary"
              direction-links
              boundary-links
              :max="pageCount"
              :max-pages="$q.screen.lt.sm ? 3 : 7"
              @update:model-value="changePage"
            />
          </q-card-actions>
          <q-inner-loading :showing="memories.loading" />
        </q-card>
      </q-tab-panel>

      <q-tab-panel name="dreaming" class="q-pa-none">
        <section aria-labelledby="dreaming-proposals" class="q-mb-lg">
          <h3 id="dreaming-proposals" class="text-subtitle1 text-weight-medium q-mt-none q-mb-sm">Offene Vorschläge</h3>
          <q-skeleton v-if="approvals.loading && !approvals.loaded" type="text" />
          <q-banner v-if="approvals.loadFailed" rounded class="app-surface-tonal">
            Vorschläge konnten nicht geladen werden.
            <template #action><q-btn flat no-caps label="Erneut laden" @click="refreshProposals" /></template>
          </q-banner>
          <p v-else-if="approvals.loaded && !memoryProposals.length" class="app-muted">Keine offenen Vorschläge.</p>
          <q-banner v-if="route.query.approval && approvals.loaded && !approvals.loadFailed && !memoryProposals.some((item) => item.id === route.query.approval)" rounded class="app-surface-tonal q-mb-sm">
            Dieser Vorschlag ist nicht mehr offen.
          </q-banner>
          <div v-for="proposal in memoryProposals" :id="`memory-proposal-${proposal.id}`" :key="proposal.id" tabindex="-1" class="q-mb-sm" :class="{ 'app-surface-tonal rounded-borders q-pa-sm': proposal.id === route.query.approval }">
            <ApprovalCard :approval="proposal" :show-draft-action="false" />
          </div>
        </section>
        <div class="dreaming-grid">
          <q-card flat class="app-surface dreaming-settings-card">
            <q-card-section class="dreaming-settings-header row items-start no-wrap q-gutter-md">
              <q-avatar rounded color="deep-purple" text-color="white" icon="auto_awesome" />
              <div class="col">
                <div class="text-h6">Dreaming <span class="text-caption app-muted">(optional)</span></div>
                <div class="text-body2 app-muted">Leitet aus abgeschlossenen Gesprächen dauerhafte Erinnerungen ab und konsolidiert bestehende. Memory funktioniert auch ohne Dreaming.</div>
              </div>
              <div v-if="settingsLoaded" class="column items-end">
                <q-toggle dense
                  :model-value="settingsForm.automaticDreamingEnabled"
                  color="deep-purple"
                  label="Automatisches Dreaming eingeschaltet"
                  :disable="!settingsForm.enabled || memories.settingsLoading"
                  @update:model-value="toggleAutomaticDreaming"
                />
                <div class="text-caption app-muted">
                  {{ settingsForm.effectiveAutomaticDreamingEnabled ? 'Derzeit aktiv' : settingsForm.automaticDreamingEnabled ? 'Derzeit nicht verfügbar' : 'Ausgeschaltet' }}
                </div>
              </div>
            </q-card-section>
            <q-separator />
            <q-form class="dreaming-settings-form" @submit.prevent="saveSettings" @validation-error="advancedSettingsOpen = true">
              <q-card-section>
                <q-banner v-if="settingsForm.automaticDreamingUnavailableReason" rounded class="app-surface-tonal q-mb-md">
                  <template #avatar><q-icon name="info_outline" color="warning" /></template>
                  {{ automaticUnavailableReasonMessage }}
                </q-banner>
                <q-input dense
                  v-model="settingsForm.dreamingInstructions"
                  outlined
                  autogrow
                  type="textarea"
                  maxlength="8000"
                  counter
                  label="Zusätzliche Dreaming-Hinweise"
                  hint="Kann Schwerpunkte und Kategorien festlegen. Vertrauen, Sicherheit und Evidenz bleiben hostkontrolliert."
                  class="q-mb-lg"
                  :disable="memories.settingsLoading"
                />
                <div class="text-subtitle2 text-weight-medium">Automatischer Kontext</div>
                <div class="text-body2 app-muted q-mt-xs">
                  Begrenzt die kurzen relevanten Treffer aus Erinnerungen und früheren Sessions. In Projekten haben relevante Session-Auszüge Vorrang. Diese Einstellungen gelten unabhängig von Dreaming.
                </div>
                <div class="settings-number-grid q-mt-md q-mb-lg">
                  <q-input dense v-model.number="settingsForm.automaticMemorySnippetLimit" outlined type="number" min="0" max="5" label="Memory-Treffer bei passender Anfrage" :disable="memories.settingsLoading" :rules="[integerRangeRule(0, 5)]" />
                  <q-input dense v-model.number="settingsForm.automaticSessionSnippetLimit" outlined type="number" min="0" max="5" label="Relevante Session-Auszüge" :disable="memories.settingsLoading" :rules="[integerRangeRule(0, 5)]" />
                  <q-input dense v-model.number="settingsForm.recentSessionSnippetLimit" outlined type="number" min="0" max="5" label="Auszüge aus letzten Sessions" :disable="memories.settingsLoading" :rules="[integerRangeRule(0, 5)]" />
                  <q-input dense v-model.number="settingsForm.automaticContextMaxTokens" outlined type="number" min="128" max="2048" label="Max. automatischer Kontext (Tokens)" :disable="memories.settingsLoading" :rules="[integerRangeRule(128, 2048)]" />
                </div>
                <div class="text-subtitle2 text-weight-medium">Änderungsrichtlinien</div>
                <div class="text-body2 app-muted q-mt-xs q-mb-md">
                  Legt fest, ob Dreaming Änderungen verwirft, zur Freigabe vorlegt oder automatisch anwendet.
                </div>
                <AppSelect dense
                  v-model="settingsForm.dreamingUpdatePolicy"
                  outlined
                  emit-value
                  map-options
                  label="Bestehende Erinnerungen ändern"
                  :options="dreamingPolicyOptions"
                  :disable="memories.settingsLoading"
                  class="q-mb-md"
                />
                <AppSelect dense
                  v-model="settingsForm.dreamingRemovePolicy"
                  outlined
                  emit-value
                  map-options
                  label="Erinnerungen entfernen"
                  :options="dreamingPolicyOptions"
                  :disable="memories.settingsLoading"
                  class="q-mb-lg"
                />
                <div class="text-body2 app-muted q-mb-lg">
                  Das Zusammenführen von Erinnerungen benötigt sowohl das Ändern der bestehenden als auch das Entfernen der zusammengeführten Erinnerungen.
                </div>
                <div class="text-subtitle2 text-weight-medium">Extraktion</div>
                <div class="text-body2 app-muted q-mt-xs">
                  Begrenzt, wie viele Nachrichten und Erinnerungskandidaten ein Lauf verarbeitet.
                </div>
                <div class="settings-number-grid q-mt-md">
                    <q-input dense v-model.number="settingsForm.dreamingMaxBatchMessages" outlined type="number" min="2" max="500" label="Max. Nachrichten pro Lauf" :disable="memories.settingsLoading" :rules="[integerRangeRule(2, 500)]" />
                    <q-input dense v-model.number="settingsForm.dreamingMaxCandidates" outlined type="number" min="1" max="100" label="Max. Kandidaten" :disable="memories.settingsLoading" :rules="[integerRangeRule(1, 100)]" />
                </div>
                <q-expansion-item v-model="advancedSettingsOpen" :duration="0" dense-toggle icon="settings" label="Erweiterte Einstellungen" class="q-mt-lg">
                  <div class="settings-number-grid q-pt-md">
                    <q-input dense v-model.number="settingsForm.dreamingIntervalMinutes" outlined type="number" min="1" max="10080" label="Intervall (Minuten)" :disable="memories.settingsLoading" :rules="[integerRangeRule(1, 10080)]" />
                    <q-input dense v-model.number="settingsForm.dreamingIdleMinutes" outlined type="number" min="1" max="10080" label="Leerlauf (Minuten)" :disable="memories.settingsLoading" :rules="[integerRangeRule(1, 10080)]" />
                  </div>
                </q-expansion-item>
              </q-card-section>
              <q-card-actions align="right" class="q-pa-md q-pt-none">
                <q-btn
                  unelevated
                  no-caps
                  color="primary"
                  icon="save"
                  label="Einstellungen speichern"
                  type="submit"
                  :loading="memories.settingsLoading"
                  :disable="!settingsLoaded || !settingsChanged"
                />
              </q-card-actions>
            </q-form>
            <q-inner-loading :showing="memories.settingsLoading && !settingsLoaded" />
          </q-card>

          <q-card flat class="app-surface dream-runs-card">
            <q-card-section>
              <div class="row items-start q-gutter-md">
                <q-avatar rounded color="secondary" text-color="white" icon="play_circle" />
                <div class="col">
                  <div class="text-h6">Verarbeitung</div>
                  <div class="text-body2 app-muted">Dreaming sofort für alle oder eine bestimmte Session starten.</div>
                </div>
              </div>
              <q-banner
                v-if="settingsForm.manualDreamingUnavailableReason"
                rounded
                class="app-surface-tonal q-mt-lg"
              >
                <template #avatar><q-icon name="info_outline" color="warning" /></template>
                {{ manualUnavailableReasonMessage }}
              </q-banner>
              <AppSelect dense
                v-model="dreamSessionId"
                outlined
                clearable
                emit-value
                map-options
                options-dense
                label="Session (optional)"
                :options="sessionOptions"
                class="q-mt-lg"
              />
              <q-btn
                unelevated
                no-caps
                color="deep-purple"
                icon="auto_awesome"
                label="Jetzt verarbeiten"
                class="full-width q-mt-md"
                :loading="memories.dreamRunStarting"
                :disable="!canStartDreaming"
                @click="startDreamRun"
              />
            </q-card-section>
            <q-separator />
            <q-card-section>
              <div class="row items-center q-mb-sm">
                <div class="text-subtitle1 text-weight-medium">Letzte Läufe</div>
                <q-space />
                <q-btn flat round dense icon="refresh" aria-label="Dreaming-Läufe aktualisieren" :loading="memories.dreamRunsLoading" @click="refreshDreamRuns" />
              </div>
              <div v-if="!memories.dreamRunsLoading && !memories.dreamRuns.length" class="text-body2 app-muted q-py-md">
                Noch keine Dreaming-Läufe vorhanden.
              </div>
              <q-list v-else separator>
                <q-item v-for="run in memories.dreamRuns" :key="run.id" class="q-px-none">
                  <q-item-section avatar>
                    <q-icon :name="dreamRunMeta(run.status).icon" :color="dreamRunMeta(run.status).color" />
                  </q-item-section>
                  <q-item-section>
                    <q-item-label>{{ dreamRunMeta(run.status).label }}</q-item-label>
                    <q-item-label caption>
                      {{ dreamRunSession(run) }} · {{ formatDate(run.createdAt) }}
                    </q-item-label>
                    <q-item-label v-if="run.error" caption class="text-negative">
                      {{ dreamRunError(run) }}
                    </q-item-label>
                  </q-item-section>
                  <q-item-section
                    v-if="run.status === 'completed' && (run.createdMemories != null || run.removedMemories != null)"
                    side
                    class="items-end"
                  >
                    <q-chip v-if="run.createdMemories != null" dense square>{{ run.createdMemories }} neu</q-chip>
                    <q-chip v-if="run.updatedMemories" dense square color="deep-purple" text-color="white">
                      {{ run.updatedMemories }} aktualisiert
                    </q-chip>
                    <q-chip v-if="run.removedMemories" dense square color="negative" text-color="white">
                      {{ run.removedMemories }} entfernt
                    </q-chip>
                  </q-item-section>
                </q-item>
              </q-list>
            </q-card-section>
            <q-inner-loading :showing="memories.dreamRunsLoading && !memories.dreamRuns.length" />
          </q-card>
        </div>
      </q-tab-panel>
    </q-tab-panels>

    <q-dialog v-model="memoryDialogOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="savingMemory">
      <q-card class="memory-dialog app-surface">
        <q-form ref="memoryFormRef" class="column full-height" @submit.prevent="saveMemory">
          <q-card-section class="row items-start no-wrap q-gutter-md">
            <q-avatar color="primary" text-color="white" :icon="editingMemory ? 'edit' : 'add'" />
            <div class="col">
              <div class="text-h6">{{ editingMemory ? 'Erinnerung bearbeiten' : 'Erinnerung hinzufügen' }}</div>
              <div class="text-body2 app-muted">
                Inhalt, Kategorie und Modellzugriff festlegen.
              </div>
            </div>
            <q-btn flat round dense icon="close" aria-label="Dialog schließen" :disable="savingMemory" v-close-popup />
          </q-card-section>
          <q-separator />
          <q-card-section class="col scroll">
            <q-input dense
              v-model="memoryForm.content"
              outlined
              autofocus
              autogrow
              type="textarea"
              label="Inhalt"
              maxlength="20000"
              lazy-rules
              :rules="[requiredRule]"
            />
            <div class="memory-dialog__fields q-mt-sm">
              <AppSelect dense
                v-model="memoryForm.category"
                outlined
                emit-value
                map-options
                label="Kategorie"
                :options="categoryOptions"
                lazy-rules
                :rules="[requiredRule]"
              />
              <AppSelect dense
                v-if="auth.modelTrustEnabled"
                v-model="memoryForm.audience"
                outlined
                emit-value
                map-options
                label="Verfügbar für"
                hint="Legt fest, welche Modellarten diese Erinnerung verwenden dürfen."
                :options="audienceOptions"
              />
            </div>
            <q-toggle dense
              v-if="editingMemory"
              :model-value="memoryForm.pinned"
              color="primary"
              label="Immer im Kontext"
              class="q-mt-md"
              @update:model-value="setPinned"
            />
            <div v-if="editingMemory" class="text-caption app-muted">
              Ausgeschaltet bleibt die Erinnerung gespeichert und erscheint automatisch nur bei einer passenden Anfrage; vollständige Details bleiben gezielt abrufbar.
            </div>
            <div v-if="editingMemory" class="memory-source app-surface-tonal q-pa-md q-mt-md rounded-borders">
              <div class="text-subtitle2">Quelle</div>
              <div class="q-mt-sm">{{ sourceDescription(editingMemory) }}</div>
              <div v-if="editingMemory.sourceMessageIds?.length" class="text-caption app-muted q-mt-xs">
                Nachrichten: {{ editingMemory.sourceMessageIds.join(', ') }}
              </div>
              <div v-if="editingMemory.sourceRunId" class="text-caption app-muted q-mt-xs">
                Run: {{ editingMemory.sourceRunId }}
              </div>
            </div>
          </q-card-section>
          <q-separator />
          <q-card-actions align="right" class="q-pa-md">
            <q-btn flat no-caps label="Abbrechen" :disable="savingMemory" v-close-popup />
            <q-btn unelevated no-caps color="primary" label="Speichern" type="submit" :loading="savingMemory" />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useRoute, useRouter } from 'vue-router'
import ApprovalCard from 'src/features/approvals/ApprovalCard.vue'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import { useConfirm } from 'src/composables/confirm'
import { useChatStore } from 'src/features/chat/store'
import { useApprovalStore } from 'stores/approvals'
import { useMemoryStore } from 'stores/memories'
import { useAuthStore } from 'stores/auth'
import { apiErrorMessage } from 'src/utils/api-errors'

defineOptions({ name: 'MemorySettingsPage' })

const $q = useQuasar()
const { confirm } = useConfirm()
const chat = useChatStore()
const memories = useMemoryStore()
const auth = useAuthStore()
const approvals = useApprovalStore()
const route = useRoute()
const router = useRouter()
const section = computed({
  get: () => route.query.tab === 'dreaming' ? 'dreaming' : 'memories',
  set: (tab) => router.replace({ query: { ...route.query, tab, approval: undefined } })
})
const memoryProposals = computed(() => approvals.pending.filter((item) => item.requestType === 'memory_change'))
const page = ref(1)
const pageSize = ref(20)
const memoryDialogOpen = ref(false)
const memoryFormRef = ref(null)
const editingMemory = ref(null)
const savingMemory = ref(false)
const settingsLoaded = ref(false)
const savedSettings = ref(null)
const dreamSessionId = ref(null)
const advancedSettingsOpen = ref(false)
const filters = reactive({ query: '', category: null, sourceType: null, audience: null, pinned: null })
watch(() => auth.modelTrustEnabled, (enabled) => {
  if (!enabled && filters.audience) {
    filters.audience = null
    void applyFilters()
  }
})
const memoryForm = reactive({ content: '', category: null, audience: 'both', pinned: false })
const settingsForm = reactive({
  enabled: false,
  automaticDreamingEnabled: false,
  effectiveAutomaticDreamingEnabled: false,
  dreamingInstructions: '',
  dreamingIntervalMinutes: 1,
  dreamingIdleMinutes: 1,
  dreamingMaxBatchMessages: 2,
  dreamingMaxCandidates: 1,
  dreamingUpdatePolicy: 'auto',
  dreamingRemovePolicy: 'ask',
  automaticMemorySnippetLimit: 2,
  automaticSessionSnippetLimit: 3,
  recentSessionSnippetLimit: 2,
  automaticContextMaxTokens: 1024,
  automaticDreamingUnavailableReason: null,
  manualDreamingAvailable: false,
  manualDreamingUnavailableReason: null
})
const sourceTypeOptions = [
  { label: 'Tool-Aufruf', value: 'tool_call' },
  { label: 'Dreaming', value: 'dreaming' },
  { label: 'Manuell', value: 'manual' }
]
const audienceOptions = [
  { label: 'Nur private Modelle', value: 'private_only', icon: 'lock', color: 'deep-purple' },
  { label: 'Nur öffentliche Modelle', value: 'public_only', icon: 'public', color: 'blue-grey-7' },
  { label: 'Öffentliche und private Modelle', value: 'both', icon: 'groups', color: 'primary' }
]
const pinnedOptions = [
  { label: 'Immer im Kontext', value: true },
  { label: 'Bei passender Anfrage', value: false }
]
const dreamingPolicyOptions = [
  { label: 'Deaktiviert', value: 'disabled' },
  { label: 'Vorher fragen', value: 'ask' },
  { label: 'Automatisch anwenden', value: 'auto' }
]
const categoryOptions = [
  { label: 'Kontext', value: 'context', icon: 'notes', color: 'blue-grey-7' },
  { label: 'Fakt', value: 'fact', icon: 'lightbulb', color: 'blue-7' },
  { label: 'Ziel', value: 'goal', icon: 'flag', color: 'teal-7' },
  { label: 'Identität', value: 'identity', icon: 'person', color: 'indigo-7' },
  { label: 'Präferenz', value: 'preference', icon: 'favorite', color: 'deep-purple-7' }
]
const categoryFilterOptions = [{ label: 'Alle', value: null, icon: 'apps' }, ...categoryOptions]
const pageSizeOptions = [10, 20, 50].map((value) => ({ label: String(value), value }))
const sourceTypeMeta = {
  tool_call: { label: 'Tool-Aufruf', icon: 'build' },
  dreaming: { label: 'Dreaming', icon: 'auto_awesome' },
  manual: { label: 'Manuell', icon: 'edit_note' }
}

const filtersActive = computed(() => (
  Boolean(filters.query || filters.category || filters.sourceType || filters.audience)
  || filters.pinned != null
))
const pageCount = computed(() => Math.max(1, Math.ceil(memories.total / pageSize.value)))
const resultRange = computed(() => {
  if (!memories.total) return '0'
  return `${memories.offset + 1}-${Math.min(memories.offset + memories.items.length, memories.total)}`
})
const sessionOptions = computed(() => chat.sessionCatalog.map((session) => ({
  label: session.title,
  value: session.id,
  disable: session.memoryMode !== 'full'
})))
const editableTechnicalSettings = computed(() => ({
  dreamingInstructions: settingsForm.dreamingInstructions,
  dreamingIntervalMinutes: Number(settingsForm.dreamingIntervalMinutes),
  dreamingIdleMinutes: Number(settingsForm.dreamingIdleMinutes),
  dreamingMaxBatchMessages: Number(settingsForm.dreamingMaxBatchMessages),
  dreamingMaxCandidates: Number(settingsForm.dreamingMaxCandidates),
  dreamingUpdatePolicy: settingsForm.dreamingUpdatePolicy,
  dreamingRemovePolicy: settingsForm.dreamingRemovePolicy,
  automaticMemorySnippetLimit: Number(settingsForm.automaticMemorySnippetLimit),
  automaticSessionSnippetLimit: Number(settingsForm.automaticSessionSnippetLimit),
  recentSessionSnippetLimit: Number(settingsForm.recentSessionSnippetLimit),
  automaticContextMaxTokens: Number(settingsForm.automaticContextMaxTokens)
}))
const settingsChanged = computed(() => (
  JSON.stringify(editableTechnicalSettings.value) !== JSON.stringify(savedSettings.value)
))
const canStartDreaming = computed(() => Boolean(
  settingsLoaded.value
  && memories.settings?.manualDreamingAvailable
  && !settingsChanged.value
))
const automaticUnavailableReasonMessage = computed(() => settingsForm.automaticDreamingUnavailableReason
  ? apiErrorMessage({ error: { code: settingsForm.automaticDreamingUnavailableReason } })
  : '')
const manualUnavailableReasonMessage = computed(() => settingsForm.manualDreamingUnavailableReason
  ? apiErrorMessage({ error: { code: settingsForm.manualDreamingUnavailableReason } })
  : '')

function listParams(offset = (page.value - 1) * pageSize.value) {
  return { ...filters, offset, limit: pageSize.value }
}

async function loadMemories(offset) {
  try {
    const result = await memories.fetchMemories(listParams(offset))
    if (!result) return
    page.value = Math.floor(memories.offset / memories.limit) + 1
    const unseenIds = result.items.filter((item) => !item.seenAt).map((item) => item.id)
    if (unseenIds.length) {
      await nextTick()
      void memories.acknowledgeMemories(unseenIds).catch(() => {})
    }
  } catch {
    // Existing results remain visible; the central interceptor displays the error.
  }
}

function applyFilters() {
  page.value = 1
  void loadMemories(0)
}

function resetFilters() {
  Object.assign(filters, { query: '', category: null, sourceType: null, audience: null, pinned: null })
  applyFilters()
}

function setCategoryFilter(value) {
  filters.category = value
  applyFilters()
}

function changePage(value) {
  void loadMemories((value - 1) * pageSize.value)
}

function changePageSize() {
  page.value = 1
  void loadMemories(0)
}

function openCreateDialog() {
  editingMemory.value = null
  Object.assign(memoryForm, {
    content: '', category: null, audience: 'both', pinned: false
  })
  memoryDialogOpen.value = true
}

function openEditDialog(item) {
  editingMemory.value = item
  Object.assign(memoryForm, {
    content: item.content,
    category: item.category,
    audience: item.audience,
    pinned: item.pinned
  })
  memoryDialogOpen.value = true
}

async function saveMemory() {
  if (!await memoryFormRef.value?.validate()) return
  if (
    editingMemory.value?.audience === 'private_only'
    && memoryForm.audience !== 'private_only'
  ) {
    const accepted = await confirm(
      'Die Erinnerung wird danach auch für öffentliche Modelle verfügbar. Bereits übermittelte Inhalte lassen sich nicht zurückholen. Wirklich freigeben?',
      'Für öffentliche Modelle freigeben',
      { okLabel: 'Freigeben', okColor: 'warning' }
    )
    if (!accepted) return
  }
  if (
    editingMemory.value
    && memoryForm.pinned
    && !editingMemory.value.pinned
  ) {
    const accepted = await confirm(
      'Diese Erinnerung wird bei erlaubten Anfragen vollständig in den Modellkontext eingefügt und vergrößert ihn.',
      'Immer im Kontext',
      { okLabel: 'Bestätigen', okColor: 'primary' }
    )
    if (!accepted) return
  }

  savingMemory.value = true
  try {
    const payload = {
      content: memoryForm.content.trim(),
      category: memoryForm.category,
      audience: memoryForm.audience
    }
    if (editingMemory.value) {
      await memories.updateMemory(editingMemory.value.id, {
        ...payload,
        pinned: memoryForm.pinned,
        expectedRevision: editingMemory.value.revision
      })
      page.value = 1
      await loadMemories(0)
      $q.notify({ type: 'positive', message: 'Erinnerung gespeichert' })
    } else {
      await memories.createMemory(payload)
      await loadMemories(0)
      $q.notify({ type: 'positive', message: 'Erinnerung hinzugefügt' })
    }
    memoryDialogOpen.value = false
  } catch {
    // The central API interceptor displays the error; the dialog remains open.
  } finally {
    savingMemory.value = false
  }
}

async function removeMemory(item) {
  const accepted = await confirm(
    'Diese Erinnerung dauerhaft löschen?',
    'Erinnerung löschen',
    { okLabel: 'Löschen' }
  )
  if (!accepted) return
  try {
    await memories.deleteMemory(item.id, item.revision)
    const targetPage = Math.min(page.value, Math.max(1, Math.ceil(memories.total / pageSize.value)))
    page.value = targetPage
    await loadMemories((targetPage - 1) * pageSize.value)
    $q.notify({ type: 'positive', message: 'Erinnerung gelöscht' })
  } catch {
    // The central API interceptor displays the error.
  }
}

async function togglePinned(item) {
  const pinned = !item.pinned
  if (pinned) {
    const accepted = await confirm(
      'Diese Erinnerung wird bei erlaubten Anfragen vollständig in den Modellkontext eingefügt und vergrößert ihn.',
      'Immer im Kontext',
      { okLabel: 'Bestätigen', okColor: 'primary' }
    )
    if (!accepted) return
  }
  try {
    await memories.updateMemory(item.id, {
      content: item.content,
      category: item.category,
      audience: item.audience,
      pinned,
      expectedRevision: item.revision
    })
    await loadMemories()
    $q.notify({ type: 'positive', message: pinned ? 'Erinnerung ist immer im Kontext' : 'Erinnerung erscheint bei passender Anfrage' })
  } catch {
    // The central API interceptor displays stale revisions and other errors.
  }
}

function assignSettings(settings) {
  Object.assign(settingsForm, settings)
  savedSettings.value = { ...editableTechnicalSettings.value }
  settingsLoaded.value = true
}

async function saveSettings() {
  try {
    assignSettings(await memories.saveSettings(editableTechnicalSettings.value))
    $q.notify({ type: 'positive', message: 'Memory-Einstellungen gespeichert' })
  } catch {
    // The central API interceptor displays the error.
  }
}

async function toggleMemory(value) {
  await saveToggle('enabled', value, value ? 'Memory aktiviert' : 'Memory deaktiviert')
}

async function toggleAutomaticDreaming(value) {
  await saveToggle(
    'automaticDreamingEnabled',
    value,
    value ? 'Automatisches Dreaming aktiviert' : 'Automatisches Dreaming deaktiviert'
  )
}

async function saveToggle(field, value, message) {
  const previous = settingsForm[field]
  const unsavedTechnicalSettings = { ...editableTechnicalSettings.value }
  settingsForm[field] = value
  try {
    const settings = await memories.saveSettings({ [field]: value })
    Object.assign(settingsForm, settings, unsavedTechnicalSettings)
    $q.notify({ type: 'positive', message })
  } catch {
    settingsForm[field] = previous
  }
}

async function refreshDreamRuns() {
  const [, settingsResult] = await Promise.allSettled([
    memories.fetchDreamRuns(),
    memories.fetchSettings()
  ])
  if (settingsResult.status === 'fulfilled') {
    const settings = settingsResult.value
    if (!settingsLoaded.value) {
      assignSettings(settings)
    }
  }
}

async function startDreamRun() {
  try {
    const result = await memories.startDreamRun(dreamSessionId.value)
    $q.notify({
      type: result.queued > 0 ? 'positive' : 'info',
      message: result.queued > 0
        ? `${result.queued} ${result.queued === 1 ? 'Dreaming-Lauf' : 'Dreaming-Läufe'} eingeplant`
        : 'Kein neues Dreaming erforderlich'
    })
  } catch {
    // The central API interceptor displays the error.
  }
}

function integerRangeRule(minimum, maximum) {
  return (value) => (
    Number.isInteger(Number(value))
    && Number(value) >= minimum
    && Number(value) <= maximum
  ) || `Bitte gib eine ganze Zahl zwischen ${minimum} und ${maximum} ein.`
}

function requiredRule(value) {
  return Boolean(String(value || '').trim()) || 'Pflichtfeld'
}

function setPinned(value) {
  memoryForm.pinned = value
}

function audienceMeta(value) {
  return audienceOptions.find((item) => item.value === value)
    || { label: value || 'Unbekannt', icon: 'help_outline', color: 'grey-7' }
}

function pinnedLabel(pinned) {
  return pinned ? 'Immer im Kontext' : 'Bei passender Anfrage'
}

function sourceDescription(item) {
  const source = sourceMeta(item.sourceType).label
  return item.sourceSession ? `${source} · ${item.sourceSession.title}` : source
}

function sourceMeta(value) {
  return sourceTypeMeta[value] || { label: value || 'Unbekannt', icon: 'help_outline' }
}

function categoryMeta(value) {
  return categoryOptions.find((item) => item.value === value)
    || { label: value || 'Unbekannt', icon: 'help_outline', color: 'grey-7' }
}

function formatDate(value) {
  if (!value) return 'unbekannt'
  return new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function dreamRunMeta(status) {
  return ({
    queued: { label: 'Eingeplant', icon: 'schedule', color: 'grey-7' },
    running: { label: 'Wird verarbeitet', icon: 'sync', color: 'primary' },
    blocked: { label: 'Blockiert', icon: 'block', color: 'warning' },
    completed: { label: 'Abgeschlossen', icon: 'check_circle', color: 'positive' },
    failed: { label: 'Fehlgeschlagen', icon: 'error', color: 'negative' },
    cancelled: { label: 'Abgebrochen', icon: 'cancel', color: 'grey-7' }
  })[status] || { label: status || 'Unbekannt', icon: 'help', color: 'grey-7' }
}

function dreamRunSession(run) {
  return run.sourceSession?.title || run.session?.title || run.sessionId || 'Alle Sessions'
}

function dreamRunError(run) {
  return apiErrorMessage(run.error)
}

watch(section, (value, previous) => {
  if (value === 'memories' && previous !== 'memories') {
    page.value = 1
    void loadMemories(0)
  }
})

function refreshProposals() { return approvals.fetchPending().catch(() => {}) }

watch([section, () => route.query.approval, () => memoryProposals.value.some((item) => item.id === route.query.approval)], async ([tab, id, present]) => {
  if (tab !== 'dreaming' || !present) return
  await nextTick()
  const proposal = document.getElementById(`memory-proposal-${id}`)
  proposal?.scrollIntoView({ block: 'start' })
  proposal?.focus({ preventScroll: true })
}, { immediate: true, flush: 'post' })

onMounted(() => {
  void refreshProposals()
  void loadMemories(0)
  void refreshDreamRuns()
  void chat.fetchSessionCatalog().catch(() => {})
})
</script>

<style scoped>
.memory-page {
  width: min(1240px, 100%);
  margin: 0 auto;
}

.memory-tabs {
  border-bottom: 1px solid var(--app-surface-border);
}

.memory-filter-card,
.memory-list-card,
.memory-access-card,
.dreaming-settings-card,
.dream-runs-card {
  position: relative;
}

.memory-filters {
  display: grid;
  grid-template-columns: minmax(260px, 2fr) repeat(3, minmax(150px, 1fr)) auto;
  align-items: start;
  gap: 12px;
}

.memory-category-filter {
  min-width: 0;
}

.memory-item {
  align-items: flex-start;
  padding: 20px 24px;
}

.memory-item__content {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.55;
}

.memory-status-badge {
  gap: 4px;
  padding: 4px 7px;
  font-weight: 500;
}

.memory-item__actions {
  flex-direction: row;
  gap: 4px;
}

.memory-source-link {
  color: var(--q-primary);
  text-decoration: none;
}

.memory-empty {
  padding: 64px 24px;
}

.memory-pagination {
  gap: 12px;
}

.memory-pagination .q-select {
  width: 96px;
}

.dreaming-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(320px, 0.8fr);
  align-items: start;
  gap: 20px;
}

.settings-number-grid,
.memory-dialog__fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 16px;
}

.memory-dialog {
  width: min(720px, calc(100vw - 32px));
  max-width: 720px;
  max-height: calc(100dvh - 32px);
}

@media (max-width: 1000px) {
  .memory-filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .memory-search {
    grid-column: 1 / -1;
  }

  .dreaming-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 700px) {
  .memory-access-header,
  .dreaming-settings-header {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .memory-access-header .q-toggle,
  .dreaming-settings-header > .column {
    align-items: flex-start;
    width: 100%;
  }

  .memory-filters,
  .settings-number-grid,
  .memory-dialog__fields {
    grid-template-columns: minmax(0, 1fr);
  }

  .memory-item {
    flex-wrap: wrap;
    padding: 16px;
  }

  .memory-item__actions {
    width: 100%;
    justify-content: flex-end;
    padding-top: 8px;
  }

  .memory-pagination {
    align-items: stretch;
    flex-direction: column;
  }

  .memory-pagination .q-space {
    display: none;
  }

  .memory-pagination .q-select {
    width: 100%;
  }

  .memory-pagination .q-pagination {
    justify-content: center;
  }

  .memory-dialog .q-card__actions {
    align-items: stretch;
    flex-direction: column;
  }

  .memory-dialog {
    width: 100%;
    max-width: none;
    max-height: none;
    border-radius: 0;
  }
}
</style>
