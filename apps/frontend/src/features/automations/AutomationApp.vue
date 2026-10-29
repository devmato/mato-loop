<template>
  <section class="column full-height no-wrap" aria-labelledby="automation-app-title">
    <header class="row items-center no-wrap q-gutter-sm q-pa-md">
      <q-btn v-if="selectedId" flat round dense icon="arrow_back" aria-label="Zurück zu den Automationen" @click="select(null)" />
      <div class="col overflow-hidden">
        <h2 id="automation-app-title" class="text-subtitle1 text-weight-bold q-my-none ellipsis" :title="definition?.name">
          {{ selectedId ? definition?.name || 'Neue Automation' : 'Automationen' }}
        </h2>
        <div v-if="definition" class="text-caption app-muted" role="status">
          <q-icon :name="status.icon" :color="status.color" class="q-mr-xs" />{{ status.label }}
        </div>
        <div v-else-if="!selectedId" class="text-caption app-muted">Aufträge in dieser Session</div>
      </div>
      <q-btn v-if="!selectedId && entries.length" unelevated no-caps color="primary" icon="add" label="Neu" @click="select('new')" />
    </header>
    <q-separator />
    <q-scroll-area class="col">
      <div class="q-pa-md">
        <q-banner v-if="loadFailed" rounded class="bg-negative text-white q-mb-md">
          Die Automation konnte nicht geladen werden.
          <template #action><q-btn flat no-caps color="white" label="Erneut laden" @click="load" /></template>
        </q-banner>
        <div v-if="loading" class="row justify-center q-pa-md" role="status"><q-spinner color="primary" size="24px" /><span class="app-sr-only">Automationen werden geladen</span></div>
        <template v-else-if="!selectedId">
          <div v-if="!entries.length && !loadFailed" class="text-center q-py-lg">
            <q-icon name="schedule" size="36px" color="primary" />
            <p class="text-body2 q-mt-sm">Was soll der Agent für dich übernehmen?</p>
            <q-btn unelevated no-caps color="primary" icon="add" label="Automation erstellen" @click="select('new')" />
          </div>
          <q-list v-else separator>
            <q-item v-for="item in entries" :key="item.id" clickable v-ripple class="q-px-none q-py-sm" @click="select(item.id)">
              <q-item-section avatar><q-icon :name="automationStatus(item).icon" :color="automationStatus(item).color" /></q-item-section>
              <q-item-section>
                <q-item-label lines="1" class="text-weight-medium">{{ item.name }}</q-item-label>
                <q-item-label caption>{{ timingLabel(item) }}</q-item-label>
                <q-item-label caption>{{ archived ? 'Ruhend' : automationStatus(item).label }}</q-item-label>
                <q-btn v-if="item.lastRun?.messageId" flat dense no-caps :color="automationRunStatus(item.lastRun).color" :icon="automationRunStatus(item.lastRun).icon" :label="automationLastRunLabel(item)" class="q-mt-xs" @click.stop="openLastRun(item)" @keydown.stop />
                <q-item-label v-else caption>{{ automationLastRunLabel(item) }}</q-item-label>
              </q-item-section>
              <q-item-section side @click.stop @keydown.stop>
                <q-btn flat round dense icon="more_vert" :aria-label="`Aktionen für ${item.name}`" :disable="saving || starting">
                  <AppMenu><q-list dense>
                    <q-item clickable v-close-popup :disable="archived || Boolean(item.effectiveState?.activeRunStatus)" @click="runNow(item)"><q-item-section>Jetzt ausführen</q-item-section></q-item>
                    <q-item clickable v-close-popup :disable="archived" @click="toggleEnabled(item)"><q-item-section>{{ item.enabled ? 'Deaktivieren' : 'Aktivieren' }}</q-item-section></q-item>
                    <q-item clickable v-close-popup class="text-negative" @click="remove(item)"><q-item-section>Entfernen</q-item-section></q-item>
                  </q-list></AppMenu>
                </q-btn>
              </q-item-section>
            </q-item>
          </q-list>
        </template>
        <template v-else-if="!loadFailed">
          <q-banner v-if="conflict" rounded class="app-surface-tonal q-mb-md">
            Die Automation wurde geändert. Deine Eingaben bleiben erhalten.
            <template #action><q-btn flat no-caps color="primary" label="Aktuellen Stand laden" @click="reload" /></template>
          </q-banner>
          <q-banner v-if="archived" rounded class="app-surface-tonal q-mb-md">Diese Session ist archiviert. Automationen ruhen bis zur Wiederherstellung.</q-banner>
          <q-form ref="formRef" :inert="saving ? '' : null" @submit.prevent="save">
            <div class="row items-center q-mb-sm">
              <h3 class="text-subtitle2 text-weight-bold q-my-none col">Auftrag</h3>
              <q-btn v-if="!archived && !editingTask" flat dense no-caps icon="edit" label="Bearbeiten" @click="editingTask = true" />
              <q-btn flat round dense icon="info_outline" aria-label="Hinweis zum Auftrag">
                <q-popup-proxy :transition-duration="0"><q-banner class="app-surface">Der gespeicherte Auftrag wird in diesem Gespräch ausgeführt. Chatnachrichten ändern ihn nicht automatisch, können aber spätere Antworten beeinflussen.</q-banner></q-popup-proxy>
              </q-btn>
            </div>
            <div v-if="editingTask" class="q-gutter-sm">
              <q-input v-model="form.name" :readonly="archived" outlined dense label="Name" maxlength="240" hide-bottom-space lazy-rules :rules="[requiredRule]" />
              <q-input v-model="form.task" :readonly="archived" outlined dense type="textarea" rows="6" label="Was soll der Agent tun?" maxlength="40000" hide-bottom-space lazy-rules :rules="[requiredRule]" />
            </div>
            <template v-else>
              <q-item-label :lines="taskExpanded ? undefined : 3" class="text-body2 text-pre-wrap">{{ form.task }}</q-item-label>
              <q-btn flat dense no-caps color="primary" :label="taskExpanded ? 'Weniger anzeigen' : 'Vollständigen Auftrag anzeigen'" class="q-mt-xs" @click="taskExpanded = !taskExpanded" />
            </template>

            <q-separator class="q-my-md" />
            <div class="row items-center q-mb-sm">
              <h3 class="text-subtitle2 text-weight-bold q-my-none col">Zeitpläne</h3>
              <q-btn v-if="!archived" flat dense no-caps color="primary" icon="add" label="Hinzufügen" :disable="form.schedules.length >= 50" @click="addSchedule" />
            </div>
            <p v-if="!form.schedules.length" class="text-body2 app-muted">Kein zeitgesteuerter Start.</p>
            <q-list v-else bordered separator class="rounded-borders">
              <AutomationScheduleEditor v-for="(schedule, index) in form.schedules" :key="`${selectedId}-${index}`" v-model="form.schedules[index]" :expanded="expandedSchedule === index" :readonly="archived" :error="validationAttempted ? scheduleError(schedule) : ''" @update:expanded="expandedSchedule = $event ? index : expandedSchedule === index ? null : expandedSchedule" @remove="removeSchedule(index)" />
            </q-list>
            <div v-if="definition && savedState.effectiveState?.nextRunAt" class="text-caption app-muted q-mt-sm">{{ automationTiming(savedState) }}</div>

            <q-separator class="q-my-md" />
            <h3 class="text-subtitle2 text-weight-bold q-mt-none q-mb-xs">Auslöser</h3>
            <div class="text-caption app-muted q-mb-sm">Ereignisse in deinen Sessions. Optionale Bedingungen stehen im Auftrag.</div>
            <div v-for="trigger in triggerOptions" :key="trigger.value">
              <q-checkbox v-model="form.triggers" :val="trigger.value" :label="trigger.label" :disable="archived" dense class="q-my-xs" />
            </div>
            <q-expansion-item v-model="optionsOpen" :duration="0" dense label="Weitere Optionen" class="q-mt-md">
              <div class="q-pa-sm q-gutter-sm">
                <q-toggle v-model="form.enabled" :disable="archived" dense label="Automatische Starts aktiv" />
                <q-btn v-if="definition" flat dense no-caps color="negative" icon="delete_outline" label="Automation entfernen" :disable="saving" @click="remove(definition)" />
              </div>
            </q-expansion-item>
          </q-form>
          <template v-if="definition">
            <q-btn v-if="savedState.lastRun?.messageId" flat dense no-caps :color="automationRunStatus(savedState.lastRun).color" :icon="automationRunStatus(savedState.lastRun).icon" :label="automationLastRunLabel(savedState)" class="q-mt-md" @click="openLastRun(savedState)" />
            <div v-else class="text-caption app-muted q-mt-md">{{ automationLastRunLabel(savedState) }}</div>
          </template>
        </template>
      </div>
    </q-scroll-area>
    <template v-if="selectedId && !loading && !loadFailed">
      <q-separator />
      <footer class="q-pa-sm" aria-label="Automationsaktionen">
        <div v-if="dirty" class="text-caption app-muted q-px-sm q-pb-xs">Ungespeicherte Änderungen</div>
        <div class="row items-center q-gutter-xs">
          <q-btn v-if="dirty || !definition" unelevated no-caps no-wrap color="primary" label="Speichern" :loading="saving" :disable="archived || conflict" @click="save" />
          <q-btn v-if="definition" :unelevated="!dirty" :flat="dirty" no-caps no-wrap color="primary" icon="play_arrow" label="Jetzt ausführen" :loading="starting" :disable="dirty || archived || conflict || busy || saving" @click="runNow(definition)" />
          <q-btn v-if="definition && !archived" flat no-caps no-wrap :icon="savedState.enabled ? 'pause' : 'play_arrow'" :label="savedState.enabled ? 'Deaktivieren' : 'Aktivieren'" :disable="dirty || conflict || saving" @click="toggleEnabled(definition)" />
        </div>
      </footer>
    </template>
  </section>
</template>

<script setup>
import AppMenu from 'src/shared/ui/AppMenu.vue'
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { isNavigationFailure, NavigationFailureType, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import AutomationScheduleEditor from 'src/features/automations/AutomationScheduleEditor.vue'
import { useConfirm } from 'src/composables/confirm'
import { scheduleDraft, scheduleError, schedulePayload, scheduleSummary } from 'src/utils/automation-schedules'
import { automationLastRunLabel, automationRunStatus, automationStatus, automationTiming } from 'src/utils/automation-status'
import { isPwaUpdateReload } from 'src/utils/pwa-update'
import { useAutomationStore } from 'stores/automations'
import { useChatStore } from 'src/features/chat/store'
import { useControlCenterStore } from 'stores/control-center'

const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const { confirm } = useConfirm()
const automations = useAutomationStore()
const chat = useChatStore()
const controlCenter = useControlCenterStore()
const sessionId = computed(() => controlCenter.sessionId)
const selectedId = computed(() => typeof route.query.automation === 'string' ? route.query.automation : null)
const definition = ref(null)
const formRef = ref(null)
const loading = ref(false)
const loadFailed = ref(false)
const saving = ref(false)
const starting = ref(false)
const savedDraft = ref('')
const validationAttempted = ref(false)
const optionsOpen = ref(false)
const editingTask = ref(false)
const taskExpanded = ref(false)
const expandedSchedule = ref(null)
const form = reactive({ name: '', task: '', enabled: true, schedules: [], triggers: [] })
const triggerOptions = [{ value: 'agent_response', label: 'Agent hat geantwortet' }, { value: 'agent_error', label: 'Agent-Auftrag fehlgeschlagen' }]
const summaries = computed(() => new Map(automations.items.map((item) => [item.id, item])))
const entries = computed(() => automations.sessionItems
  .filter((item) => item.executionSessionId === sessionId.value)
  .map((item) => ({ ...item, ...summaries.value.get(item.id) })))
const summary = computed(() => summaries.value.get(definition.value?.id))
const savedState = computed(() => summary.value || definition.value)
const status = computed(() => archived.value ? { label: 'Ruhend', icon: 'archive', color: 'grey-6' } : automationStatus(savedState.value))
const archived = computed(() => Boolean(chat.current?.archived))
const conflict = computed(() => Boolean(definition.value && summary.value && summary.value.revision !== definition.value.revision))
const busy = computed(() => Boolean(savedState.value?.effectiveState?.activeRunStatus))
const draftSignature = () => JSON.stringify(form, (key, value) => key === 'advanced' ? undefined : value)
const dirty = computed(() => Boolean(selectedId.value && savedDraft.value) && draftSignature() !== savedDraft.value)
const requiredRule = (value) => Boolean(String(value || '').trim()) || 'Pflichtfeld'
let version = 0

function timingLabel(item) {
  if (!item.schedules.length) return item.triggers?.length ? item.triggers.map((id) => triggerOptions.find((option) => option.value === id)?.label).join(' · ') : 'Nur auf Knopfdruck'
  return scheduleSummary(scheduleDraft(item.schedules[0])) + (item.schedules.length > 1 ? ` · +${item.schedules.length - 1} weitere` : '')
}

function assign(value) {
  definition.value = value
  Object.assign(form, {
    name: value?.name || '', task: value?.task || '', enabled: value?.enabled ?? true,
    schedules: value?.schedules?.map(scheduleDraft) || [],
    triggers: [...(value?.triggers || [])]
  })
  savedDraft.value = draftSignature()
  validationAttempted.value = false
  editingTask.value = !value
}

async function load() {
  const current = ++version
  loading.value = true
  loadFailed.value = false
  savedDraft.value = ''
  definition.value = null
  saving.value = false
  starting.value = false
  expandedSchedule.value = null
  taskExpanded.value = false
  optionsOpen.value = false
  try {
    const values = await automations.fetchForSession(sessionId.value)
    if (current !== version) return
    const value = values.find((item) => item.id === selectedId.value)
    if (selectedId.value && selectedId.value !== 'new' && !value) { loadFailed.value = true; return }
    assign(value || null)
    if (value && typeof route.query.run === 'string') {
      const run = await automations.fetchRun(value.id, route.query.run)
      if (current !== version) return
      if (!await router.replace({ name: 'chat', query: { session: sessionId.value, ...(run.messageId ? { message: run.messageId } : {}) } })) await controlCenter.setOpen(false)
    }
  } catch {
    if (current === version) loadFailed.value = true
  } finally {
    if (current === version) loading.value = false
  }
}

async function select(id) {
  await router.replace({ query: { ...route.query, automation: id || undefined, run: undefined } })
}

async function openLastRun(item) {
  if (!item.lastRun?.messageId) return
  try {
    const failure = await router.push({ name: 'chat', query: { session: sessionId.value, message: item.lastRun.messageId } })
    if (failure && !isNavigationFailure(failure, NavigationFailureType.duplicated)) return
    await controlCenter.setOpen(false)
  } catch {
    // The Axios interceptor displays panel persistence errors.
  }
}

function payload() {
  return {
    name: form.name.trim(), task: form.task.trim(), sessionId: sessionId.value,
    enabled: form.enabled, schedules: form.schedules.map(schedulePayload),
    triggers: form.triggers
  }
}

async function save() {
  if (saving.value || loading.value || archived.value || conflict.value || !selectedId.value) return
  const current = version
  saving.value = true
  validationAttempted.value = true
  try {
    if (!form.name.trim() || !form.task.trim()) { editingTask.value = true; await nextTick() }
    const valid = await formRef.value?.validate()
    const invalidIndex = form.schedules.findIndex((schedule) => scheduleError(schedule))
    if (invalidIndex !== -1) { expandedSchedule.value = invalidIndex; form.schedules[invalidIndex].advanced = true }
    if (current !== version || !valid || invalidIndex !== -1) return
    const value = definition.value
      ? await automations.updateAutomation(definition.value.id, { ...payload(), expectedRevision: definition.value.revision })
      : await automations.createAutomation(payload())
    if (current !== version) return
    assign(value)
    saving.value = false
    await controlCenter.ensureApp('automation')
    if (selectedId.value === 'new') await select(value.id)
    $q.notify({ type: 'positive', message: 'Automation gespeichert' })
  } catch {
    // The Axios interceptor displays the error; keep the draft.
  } finally {
    if (current === version) saving.value = false
  }
}

async function runNow(item = definition.value) {
  if (!item || item.effectiveState?.activeRunStatus || dirty.value || archived.value || starting.value || saving.value || conflict.value) return
  const current = version
  starting.value = true
  try { await automations.runNow(item.id, item.revision) } catch {
    // The Axios interceptor displays the error.
  } finally { if (current === version) starting.value = false }
}

async function toggleEnabled(item = definition.value) {
  if (!item || saving.value || dirty.value || conflict.value) return
  const current = version
  saving.value = true
  try {
    const saved = await automations.fetchAutomation(item.id)
    if (current !== version) return
    const value = await automations.updateAutomation(item.id, {
      name: saved.name, task: saved.task, enabled: !item.enabled,
      schedules: saved.schedules.map((schedule) => schedulePayload(scheduleDraft(schedule))),
      triggers: saved.triggers || [], expectedRevision: saved.revision
    })
    if (current === version && definition.value?.id === item.id) assign(value)
  } catch {
    // The Axios interceptor displays the error; keep the saved state.
  } finally { if (current === version) saving.value = false }
}

async function remove(item = definition.value) {
  const current = version
  if (!item || !await confirm(`„${item.name}“ mit allen Zeitplänen entfernen? Das Gespräch bleibt erhalten.`, 'Automation entfernen?', { okLabel: 'Entfernen' }) || current !== version) return
  saving.value = true
  try {
    await automations.deleteAutomation(item.id, item.revision)
    if (current !== version) return
    if (definition.value?.id === item.id) { assign(null); saving.value = false; await select(null) }
    $q.notify({ type: 'positive', message: 'Automation entfernt' })
  } catch {
    // The Axios interceptor displays the error.
  } finally { if (current === version) saving.value = false }
}

function addSchedule() {
  form.schedules.push(scheduleDraft())
  expandedSchedule.value = form.schedules.length - 1
}

async function removeSchedule(index) {
  const current = version
  if (await confirm('Diesen Zeitplan entfernen?', 'Zeitplan entfernen?', { okLabel: 'Entfernen' }) && current === version) {
    form.schedules.splice(index, 1)
    expandedSchedule.value = null
  }
}

async function confirmDiscard() {
  if (saving.value) return false
  return !dirty.value || await confirm('Ungespeicherte Änderungen verwerfen?', 'Änderungen verwerfen?', { okLabel: 'Verwerfen' })
}

async function reload() {
  const current = version
  if (await confirmDiscard() && current === version) await load()
}

function beforeUnload(event) {
  if (isPwaUpdateReload() || (!dirty.value && !saving.value)) return
  event.preventDefault()
  event.returnValue = ''
}

onBeforeRouteUpdate((to, from) => to.query.session !== from.query.session || (to.query.automation === from.query.automation && to.query.run === from.query.run) || confirmDiscard())
watch(dirty, (value) => { automations.dirty = value }, { flush: 'sync' })
watch([sessionId, selectedId, () => route.query.run], () => { if (sessionId.value) void load(); else version += 1 }, { immediate: true })
watch(() => automations.items, (items, previous) => {
  if (!saving.value && previous.some((item) => item.id === selectedId.value) && !items.some((item) => item.id === selectedId.value)) {
    version += 1
    loading.value = false
    starting.value = false
    assign(null)
    void select(null)
  }
})
watch(() => automations.items.filter((item) => item.executionSessionId === sessionId.value).map((item) => `${item.id}:${item.revision}`).join(','), () => {
  if (sessionId.value && !selectedId.value && !loading.value && !saving.value) void load()
})
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => { version += 1; automations.dirty = false; window.removeEventListener('beforeunload', beforeUnload) })
</script>
