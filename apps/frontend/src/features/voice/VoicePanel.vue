<template>
  <q-card v-if="voice.active" flat bordered class="voice-console q-mb-sm" aria-label="Sprachmodus">
    <q-card-section class="q-pa-md">
      <div class="row items-center no-wrap q-gutter-md">
        <div class="voice-orb" :class="`voice-orb--${voice.muted ? 'muted' : voice.phase}`" aria-hidden="true">
          <q-icon v-if="voice.muted" name="mic_off" size="26px" />
          <q-spinner v-else-if="voice.phase === 'starting' || voice.phase === 'closing'" size="26px" />
          <div v-else class="voice-bars">
            <span v-for="bar in 5" :key="bar" :style="{ '--bar': bar }" />
          </div>
        </div>
        <div class="col" style="min-width: 0" role="status" aria-live="polite">
          <div class="text-caption text-weight-medium app-muted q-mb-xs">Sprachgespräch</div>
          <div class="voice-heading">{{ phaseLabel }}</div>
        </div>
        <q-badge v-if="voice.phase !== 'starting' && voice.phase !== 'closing'" rounded :color="voice.muted ? 'grey-7' : 'primary'" class="voice-badge">
          {{ voice.muted ? 'Stumm' : 'Live' }}
        </q-badge>
      </div>
      <div class="voice-actions q-mt-md">
        <q-btn unelevated rounded no-caps :color="voice.muted ? 'grey-7' : 'primary'" :icon="voice.muted ? 'mic' : 'mic_off'" :label="voice.muted ? 'Mikrofon an' : 'Stummschalten'" :aria-pressed="voice.muted" :disable="voice.phase === 'starting' || voice.phase === 'closing'" @click="voice.toggleMute()" />
        <q-btn flat rounded no-caps class="app-surface-tonal" icon="volume_off" label="Sprache stoppen" :disable="voice.phase === 'starting' || voice.phase === 'closing'" @click="voice.interrupt()" />
        <q-btn flat rounded no-caps class="app-surface-tonal" icon="keyboard" label="Zum Textchat" @click="voice.end()" />
        <q-btn unelevated rounded no-caps icon="call_end" color="negative" label="Gespräch beenden" @click="voice.end()" />
      </div>
    </q-card-section>
  </q-card>
  <q-list v-if="tasks.length" dense class="voice-tasks q-mb-sm">
    <q-item v-for="task in tasks" :key="task.taskId">
      <q-item-section avatar style="min-width: 32px">
        <q-spinner v-if="task.active && !task.requiresUi" color="primary" size="18px" />
        <q-icon v-else :name="task.requiresUi ? 'pending_actions' : task.status === 'completed' ? 'check_circle' : 'info_outline'" :color="task.status === 'completed' ? 'positive' : 'primary'" size="19px" />
      </q-item-section>
      <q-item-section>
        <q-item-label>Sprachauftrag · {{ taskStatus(task.status) }}</q-item-label>
        <q-item-label v-if="task.requiresUi" caption>Beantworte die Freigabe oder Rückfrage hier im Chat.</q-item-label>
      </q-item-section>
      <q-item-section side>
        <q-btn v-if="task.active && task.runId" flat dense no-caps color="negative" label="Stoppen" @click="stopTask(task)" />
      </q-item-section>
    </q-item>
  </q-list>
  <q-banner v-if="taskError" rounded class="app-surface-tonal q-mb-sm">
    Sprachaufträge konnten nicht geladen werden.
    <q-btn flat dense no-caps label="Erneut laden" @click="refreshTasks" />
  </q-banner>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from 'src/boot/axios'
import { useConfirm } from 'src/composables/confirm'
import { useChatStore } from 'src/features/chat/store'
import { useControlCenterStore } from 'stores/control-center'
import { useVoiceStore } from './store'
import { notifyError } from 'src/utils/notifications'

const chat = useChatStore(), voice = useVoiceStore()
const { confirm } = useConfirm()
const starting = ref(false), storedTasks = ref([])
const hiddenTasks = ref(new Set()), expiryTimers = new Map()
const taskError = ref(false)
let refreshTimer, refreshGeneration = 0, disposed = false
const availableTasks = computed(() => voice.active && voice.sessionId === chat.current?.id ? voice.tasks : storedTasks.value)
const tasks = computed(() => availableTasks.value.filter((task) => !hiddenTasks.value.has(task.taskId)))
const canStartWhileRunning = computed(() => tasks.value.some((task) => task.active && task.sessionId === chat.current?.id))
const phaseLabel = computed(() => voice.muted ? 'Mikrofon stumm' : ({
  starting: 'Verbindung wird aufgebaut', listening: 'Ich höre zu', thinking: 'Antwort wird vorbereitet', speaking: 'MatoLoop spricht'
})[voice.phase] || 'Verbindung wird beendet')
const taskStatus = (status) => ({
  starting: 'Wird gestartet', queued: 'Eingereiht', running: 'Läuft', waiting_approval: 'Freigabe erforderlich',
  waiting_input: 'Antwort erforderlich', completed: 'Abgeschlossen', cancelled: 'Gestoppt', failed: 'Fehlgeschlagen',
  outcome_unknown: 'Ausgang unklar – Auftrag prüfen'
})[status] || 'Status wird geprüft'

async function start() {
  if (starting.value || voice.active || disposed) return
  starting.value = true
  try {
    const session = chat.current || await chat.createEmptySession(useControlCenterStore())
    if (!session || disposed || chat.current?.id !== session.id) return
    await voice.start(session.id)
  } catch (error) {
    if (!error?.isAxiosError) notifyError(error)
  } finally { starting.value = false }
}
async function stopTask(task) {
  if (!await confirm('Soll der Sprachauftrag gestoppt werden? Bereits ausgeführte Aktionen werden nicht rückgängig gemacht.', 'Aufgabe stoppen', { okLabel: 'Stoppen', okColor: 'negative' })) return
  await api.post(`/sessions/${task.sessionId}/runs/${task.runId}/cancel`)
  await refreshTasks()
}
async function refreshTasks() {
  clearTimeout(refreshTimer)
  const generation = ++refreshGeneration
  if (disposed || starting.value || voice.active) return
  if (!chat.current?.id || chat.current.archived || chat.current.readOnly) { storedTasks.value = []; return }
  const id = chat.current.id
  try {
    const { data } = await api.get(`/sessions/${id}/voice`)
    if (disposed || generation !== refreshGeneration || chat.current?.id !== id) return
    storedTasks.value = data.tasks
    taskError.value = false
    if (data.tasks.some((task) => task.active)) refreshTimer = setTimeout(refreshTasks, 3000)
  } catch (error) {
    if (disposed || generation !== refreshGeneration || chat.current?.id !== id) return
    taskError.value = true
    if (!error?.isAxiosError) notifyError(error)
  }
}
function clearTaskExpiry() {
  for (const timer of expiryTimers.values()) clearTimeout(timer)
  expiryTimers.clear()
  hiddenTasks.value.clear()
}
watch(() => chat.current?.id, (id) => {
  clearTaskExpiry()
  if (voice.active && voice.sessionId !== id) void voice.end()
  storedTasks.value = []
  void refreshTasks()
}, { immediate: true })
watch(availableTasks, (items) => {
  const completed = new Set(items.filter((task) => !task.active && task.status === 'completed').map((task) => task.taskId))
  for (const [id, timer] of expiryTimers) {
    if (!completed.has(id)) { clearTimeout(timer); expiryTimers.delete(id) }
  }
  for (const id of hiddenTasks.value) {
    if (items.some((task) => task.taskId === id && (task.active || task.status !== 'completed'))) hiddenTasks.value.delete(id)
  }
  for (const id of completed) {
    if (expiryTimers.has(id) || hiddenTasks.value.has(id)) continue
    expiryTimers.set(id, setTimeout(() => {
      expiryTimers.delete(id)
      hiddenTasks.value.add(id)
    }, 5000))
  }
}, { immediate: true, deep: true })
watch(() => chat.current?.messages?.length, () => { void refreshTasks() })
watch(() => voice.active, (active) => { if (!active && voice.sessionId) void refreshTasks() })
onBeforeUnmount(() => {
  disposed = true
  clearTimeout(refreshTimer)
  clearTaskExpiry()
  if (voice.active) void voice.end()
})
defineExpose({ start, starting, canStartWhileRunning })
</script>

<style scoped lang="scss">
.voice-console {
  border-radius: 22px;
  border-color: color-mix(in srgb, var(--q-primary) 22%, var(--app-surface-border));
  background: radial-gradient(ellipse at top left, color-mix(in srgb, var(--q-primary) 8%, transparent), transparent 70%), var(--app-surface);
  overflow: hidden;
}
.voice-heading { font-size: 19px; font-weight: 650; line-height: 1.3; }
.voice-badge { padding: 6px 9px; font-size: 11px; }
.voice-orb {
  flex: 0 0 62px;
  height: 62px;
  display: grid;
  place-items: center;
  color: white;
  border-radius: 50%;
  background: linear-gradient(140deg, color-mix(in srgb, var(--q-primary) 75%, #a78bfa), var(--q-primary));
  box-shadow: 0 0 0 6px color-mix(in srgb, var(--q-primary) 8%, transparent);
}
.voice-bars { display: flex; align-items: center; gap: 3px; height: 28px; }
.voice-bars span {
  width: 3px;
  height: 12px;
  border-radius: 3px;
  background: currentColor;
  animation: voice-wave 1.5s ease-in-out infinite;
  animation-delay: calc(var(--bar) * -0.18s);
}
.voice-orb--speaking .voice-bars span { animation-duration: .65s; }
.voice-orb--thinking .voice-bars span { animation-duration: 2s; }
.voice-orb--muted { color: var(--app-muted); background: var(--app-surface-tonal); box-shadow: none; }
.voice-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.voice-actions > .q-btn { flex: 1 1 140px; min-height: 42px; font-size: 12px; }
.voice-tasks { border-radius: 14px; background: var(--app-surface-tonal); }
@keyframes voice-wave { 0%, 100% { transform: scaleY(.5); } 50% { transform: scaleY(1.7); } }
@media (max-width: 400px) {
  .voice-orb { flex-basis: 48px; height: 48px; }
  .voice-heading { font-size: 17px; }
  .voice-actions > .q-btn { flex-basis: 120px; }
}
@media (prefers-reduced-motion: reduce) {
  .voice-bars span { animation: none; }
}
</style>
