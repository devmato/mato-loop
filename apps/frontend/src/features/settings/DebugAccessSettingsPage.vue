<template>
  <section>
    <header class="settings-section-header">
      <div>
        <h2 class="text-h6 text-weight-bold q-my-none">Debug-Zugang</h2>
        <p class="text-body2 app-muted q-mb-none q-mt-xs">
          Ermöglicht einem MCP-Client die Diagnose deiner eigenen Chats, Agent-Läufe und der Backend-Logs.
        </p>
      </div>
    </header>

    <q-card flat class="app-surface">
      <q-card-section class="q-gutter-y-md">
        <q-toggle dense
          :model-value="access.configured"
          :disable="access.loading || access.saving"
          label="Debug-Zugang aktiv"
          color="primary"
          @update:model-value="setEnabled"
        />

        <q-banner rounded class="app-surface-tonal">
          Solange der Zugang aktiv ist, zeichnet MatoLoop zusätzliche Diagnosedaten für dein Konto
          auf. Der Zugang erlaubt auch das Lesen instanzweiter Backend-Logs, die Einträge zu anderen
          Benutzern enthalten können. Zugangsdaten bleiben unzugänglich.
        </q-banner>

        <q-input dense :model-value="endpoint" readonly outlined label="MCP-Adresse">
          <template #append>
            <q-btn flat round dense icon="content_copy" aria-label="MCP-Adresse kopieren" @click="copy(endpoint, 'MCP-Adresse')" />
          </template>
        </q-input>

        <div v-if="access.createdAt" class="text-body2 app-muted">
          Aktiviert {{ formatAbsoluteDateTime(access.createdAt) }}
        </div>
      </q-card-section>

      <q-card-actions v-if="access.configured" align="right" class="q-pa-md q-pt-none">
        <q-btn
          flat
          no-caps
          color="primary"
          label="Token erneuern"
          :loading="access.saving"
          @click="rotate"
        />
      </q-card-actions>
      <q-inner-loading :showing="access.loading" />
    </q-card>

    <q-dialog v-model="tokenDialogOpen" :transition-duration="0" persistent :maximized="$q.screen.lt.sm" @hide="token = ''">
      <q-card class="app-surface">
        <q-card-section>
          <div class="text-h6">Debug-Token</div>
          <q-banner rounded class="app-surface-tonal q-mt-md">
            Dieser Token wird nur einmal angezeigt. Jeder Besitzer kann damit deine eigenen
            Diagnose-, Chat- und Tool-Inhalte sowie instanzweite Backend-Logs lesen.
          </q-banner>
        </q-card-section>
        <q-card-section class="q-pt-none">
          <q-input dense :model-value="token" readonly outlined type="textarea" autogrow label="Bearer-Token">
            <template #append>
              <q-btn flat round dense icon="content_copy" aria-label="Debug-Token kopieren" @click="copy(token, 'Debug-Token')" />
            </template>
          </q-input>
        </q-card-section>
        <q-card-actions align="right" class="q-pa-md q-pt-none">
          <q-btn unelevated no-caps color="primary" label="Schließen" @click="closeTokenDialog" />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import { copyToClipboard, useQuasar } from 'quasar'
import { computed, onMounted, ref } from 'vue'
import { useConfirm } from 'src/composables/confirm'
import { formatAbsoluteDateTime } from 'src/utils/date-time'
import { useDebugAccessStore } from 'stores/debug-access'

defineOptions({ name: 'DebugAccessSettingsPage' })

const $q = useQuasar()
const { confirm } = useConfirm()
const access = useDebugAccessStore()
const token = ref('')
const tokenDialogOpen = ref(false)
const endpoint = computed(() => `${window.location.origin}/api/debug/mcp`)

async function showNewToken() {
  const result = await access.createToken()
  token.value = result.token
  tokenDialogOpen.value = true
}

async function setEnabled(enabled) {
  if (enabled) {
    await showNewToken()
    return
  }
  const accepted = await confirm(
    'Den Debug-Zugang deaktivieren? Der Token wird sofort ungültig und die Diagnoseaufzeichnung endet.',
    'Debug-Zugang deaktivieren',
    { okLabel: 'Deaktivieren', okColor: 'negative' }
  )
  if (!accepted) return
  await access.revokeToken()
  $q.notify({ type: 'positive', message: 'Debug-Zugang deaktiviert' })
}

async function rotate() {
  const accepted = await confirm(
    'Den vorhandenen Debug-Token ersetzen? Verbundene MCP-Clients verlieren sofort den Zugriff.',
    'Debug-Token erneuern',
    { okLabel: 'Erneuern' }
  )
  if (accepted) await showNewToken()
}

async function copy(value, label) {
  try {
    await copyToClipboard(value)
    $q.notify({ type: 'positive', message: `${label} kopiert` })
  } catch {
    $q.notify({ type: 'negative', message: `${label} konnte nicht kopiert werden` })
  }
}

function closeTokenDialog() {
  tokenDialogOpen.value = false
  token.value = ''
}

onMounted(() => access.fetchStatus())
</script>
