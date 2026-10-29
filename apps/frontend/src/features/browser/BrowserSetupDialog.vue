<template>
  <q-dialog :model-value="modelValue" :transition-duration="0" :maximized="$q.screen.lt.sm" @update:model-value="$emit('update:modelValue', $event)" @show="initialize" @hide="dispose">
    <q-card class="app-surface column no-wrap" :style="$q.screen.lt.sm ? undefined : { width: '900px', maxWidth: '95vw', height: '80dvh' }">
      <q-card-section class="row items-center no-wrap">
        <div class="text-h6 col">Browser einrichten</div>
        <q-btn flat round icon="close" aria-label="Einrichtung schließen" v-close-popup />
      </q-card-section>
      <q-card-section class="q-pt-none">
        <div role="status" aria-live="polite">{{ browserStatusLabel(setup.status) }}</div>
        <div class="text-caption app-muted">Die Einrichtung verwendet ein eigenes Terminal, unabhängig vom Chat. Ein angefordertes sudo-Passwort hier eingeben. Schließen beendet die Einrichtung nicht.</div>
        <div v-if="setup.status?.browserVersion" class="text-caption">Chromium: {{ setup.status.browserVersion }}</div>
        <div v-if="setup.status?.playwrightVersion" class="text-caption">Playwright: {{ setup.status.playwrightVersion }}</div>
        <div v-if="setup.status?.missing?.length" class="text-caption">Fehlende Komponenten: {{ setup.status.missing.join(', ') }}</div>
        <div v-if="setup.exitCode !== null" role="status">Einrichtung beendet (Exit-Code {{ setup.exitCode }}).</div>
        <div v-if="!setup.connected" role="status">{{ setup.socket ? 'Verbindung wird aufgebaut.' : 'Ansicht getrennt. Zum Fortsetzen erneut öffnen.' }}</div>
      </q-card-section>
      <q-banner v-if="setup.truncated" dense class="bg-warning text-dark">Frühere Terminalausgaben wurden gekürzt.</q-banner>
      <div ref="host" class="browser-setup-dialog__terminal col q-pa-sm" />
      <q-card-actions align="right">
        <q-btn flat no-caps label="Status aktualisieren" :disable="!setup.connected" @click="setup.send({ type: 'status' })" />
        <q-btn color="primary" no-caps label="Einrichtung starten" :loading="setup.starting" :disable="!setup.connected || ['installing', 'unsupported', 'ready'].includes(setup.status?.state)" @click="start" />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { useBrowserSetupStore } from 'stores/browser-setup'
import { browserStatusLabel } from 'src/utils/browser'
import { useConfirm } from 'src/composables/confirm'

const props = defineProps({ modelValue: Boolean, integrationId: { type: String, required: true } })
defineEmits(['update:modelValue'])
const setup = useBrowserSetupStore()
const { confirm } = useConfirm()
const host = ref(null)
let terminal, fit, observer, viewerGeneration, writing = 0

function initialize() {
  dispose()
  setup.connect(props.integrationId)
  viewerGeneration = setup.generation
  Terminal.strings.promptLabel = 'Eingabe für die Browser-Einrichtung'
  Terminal.strings.tooMuchOutput = 'Zu viel Ausgabe für die vollständige Sprachausgabe.'
  terminal = new Terminal({ screenReaderMode: true, convertEol: false, cursorBlink: true,
    disableStdin: true, scrollback: 5000, minimumContrastRatio: 4.5,
    theme: { background: '#111318', foreground: '#f1f3f5' } })
  fit = new FitAddon()
  terminal.loadAddon(fit)
  terminal.open(host.value)
  const instance = terminal
  const generation = viewerGeneration
  terminal.onData((data) => {
    if (terminal === instance && setup.generation === generation && !writing) setup.input(data)
  })
  observer = new ResizeObserver(resize)
  observer.observe(host.value)
  resize()
}

function resize() {
  if (!terminal || !host.value?.clientHeight || setup.generation !== viewerGeneration) return
  fit.fit()
  setup.resize(terminal.cols, terminal.rows)
}

async function start() {
  const generation = setup.generation
  if (await confirm('Die Browser-Komponenten werden auf diesem Computer installiert. Dafür können Administratorrechte erforderlich sein.', 'Browser einrichten?', { okLabel: 'Einrichten' })
    && generation === setup.generation && props.modelValue) setup.start()
}

function dispose() {
  if (setup.generation === viewerGeneration) setup.release()
  viewerGeneration = null
  observer?.disconnect()
  terminal?.dispose()
  terminal = null
  writing = 0
}

watch(() => setup.buffer, (value, previous) => {
  if (!terminal || setup.generation !== viewerGeneration) return
  const instance = terminal
  const append = value.startsWith(previous)
  if (!append) instance.reset()
  writing += 1
  instance.write(append ? value.slice(previous.length) : value, () => { if (terminal === instance) writing -= 1 })
})
watch(() => [setup.connected, setup.status?.state], () => {
  if (terminal) terminal.options.disableStdin = !setup.connected || setup.status?.state !== 'installing'
  resize()
})
onBeforeUnmount(dispose)
</script>

<style scoped>
.browser-setup-dialog__terminal { min-height: 0; min-width: 0; background: #111318; }
.browser-setup-dialog__terminal :deep(.xterm) { height: 100%; }
</style>
