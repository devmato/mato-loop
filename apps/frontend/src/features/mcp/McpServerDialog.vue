<template>
  <q-dialog :model-value="modelValue" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="saving" @hide="close">
    <q-card class="integration-dialog app-surface column">
      <q-card-section class="row items-start no-wrap q-gutter-md">
        <q-avatar color="primary" text-color="white" icon="hub" />
        <div class="col">
          <div class="text-h6">{{ server ? `${server.name} bearbeiten` : 'MCP-Server hinzufügen' }}</div>
          <div class="text-body2 app-muted">Streamfähigen HTTP-MCP-Endpunkt verbinden.</div>
        </div>
        <q-btn flat round dense icon="close" aria-label="Dialog schließen" @click="close" />
      </q-card-section>
      <q-separator />

      <q-form ref="formRef" class="col column no-wrap" @submit.prevent="save">
        <q-card-section class="col scroll q-gutter-md">
          <q-input dense v-model.trim="form.name" outlined label="Name" maxlength="120" lazy-rules :rules="[requiredRule]" :disable="locked" />
          <q-input dense
            v-model.trim="form.endpoint"
            outlined
            type="url"
            label="HTTP-Endpunkt"
            placeholder="https://mcp.example.com/mcp"
            hint="Nur HTTP und HTTPS werden unterstützt."
            persistent-hint
            lazy-rules
            :rules="[requiredRule, urlRule]"
            :disable="locked"
          />
          <q-banner v-if="form.endpoint.toLowerCase().startsWith('http://')" rounded class="app-surface-tonal" role="alert">
            <template #avatar><q-icon name="warning_amber" color="warning" /></template>
            HTTP überträgt MCP-Daten und Zugangsdaten unverschlüsselt. Verwende es nur in einem vertrauenswürdigen lokalen Netz.
          </q-banner>
          <AppSelect dense v-model="authMode" outlined label="Authentifizierung" emit-value map-options :options="authModes" :disable="locked" />
          <q-input dense
            v-if="authMode === 'static'"
            v-model="form.bearerToken"
            outlined
            type="password"
            :label="server?.hasBearerToken ? 'Neuer Bearer-Token (optional)' : 'Bearer-Token (optional)'"
            :hint="server?.hasBearerToken ? 'Leer lassen, um den gespeicherten Token beizubehalten.' : 'Für Endpunkte ohne Anmeldung leer lassen.'"
            :disable="form.removeBearerToken || locked"
            lazy-rules
            persistent-hint
          />
          <q-toggle dense
            v-if="authMode === 'static' && server?.hasBearerToken"
            v-model="form.removeBearerToken"
            color="negative"
            label="Gespeicherten Bearer-Token entfernen"
            :disable="locked"
            @update:model-value="form.bearerToken = ''"
          />
          <template v-if="authMode === 'oauth'">
            <q-banner rounded class="app-surface-tonal">
              {{ server?.authStatus === 'authorized' ? 'OAuth-Anmeldung gespeichert.' : 'Melde dich im Browser beim OAuth-Anbieter des MCP-Servers an.' }}
            </q-banner>
            <q-input dense v-model.trim="oauth.scope" outlined label="Scopes (optional)" hint="Leer lassen für die vom MCP-Server angegebenen Scopes; mehrere Scopes mit Leerzeichen trennen." persistent-hint maxlength="1024" :disable="locked" />
            <q-toggle dense v-model="registeredClient" label="Vorregistrierten Client verwenden" :disable="locked" />
            <template v-if="registeredClient">
              <q-input dense v-model.trim="oauth.client_id" outlined label="Client-ID" maxlength="1024" lazy-rules :rules="[requiredRule]" :disable="locked" />
              <AppSelect dense v-model="oauth.token_endpoint_auth_method" outlined label="Client-Authentifizierung" emit-value map-options :options="clientMethods" :disable="locked" />
              <q-input dense v-if="oauth.token_endpoint_auth_method !== 'none'" v-model="clientSecret" outlined type="password" autocomplete="new-password" label="Client-Secret" :hint="server?.hasClientSecret ? 'Leer lassen, um das gespeicherte Secret bei unveränderten Client-Einstellungen zu verwenden.' : undefined" persistent-hint maxlength="10000" :disable="locked" />
            </template>
            <q-banner v-if="servers.oauthAttempt" rounded class="app-surface-tonal" role="status">
              Die Anmeldung ist vorbereitet. Öffne den Anbieter, um die Verbindung abzuschließen.
            </q-banner>
            <q-btn v-else-if="server?.oauth" outline no-caps color="primary" :label="server.authStatus === 'authorized' ? 'Erneut anmelden' : 'Verbinden'" :loading="saving" @click="connectOAuth" />
            <q-btn v-if="server?.oauth" flat no-caps color="negative" label="Anmeldung entfernen" :disable="locked" @click="disconnectOAuth" />
          </template>
          <q-toggle dense v-model="form.enabled" color="primary" label="MCP-Server aktivieren" :disable="locked" />
        </q-card-section>
        <q-separator />
        <q-card-actions class="q-pa-md">
          <q-btn flat no-caps label="Abbrechen" @click="close" />
          <q-space />
          <q-btn v-if="prepared" unelevated no-caps color="primary" label="Anmeldung öffnen" :href="servers.oauthAttempt.authorizationUrl" />
          <q-btn v-else unelevated no-caps color="primary" type="submit" :label="needsOAuth ? 'Verbinden' : server ? 'Änderungen speichern' : 'MCP-Server hinzufügen'" :loading="saving" />
        </q-card-actions>
      </q-form>
    </q-card>
  </q-dialog>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { useMcpServerStore } from 'stores/mcp-servers'
import { useConfirm } from 'src/composables/confirm'

defineOptions({ name: 'McpServerDialog' })

const props = defineProps({
  modelValue: { type: Boolean, required: true },
  server: { type: Object, default: null }
})
const emit = defineEmits(['update:modelValue'])
const $q = useQuasar()
const servers = useMcpServerStore()
const { confirm } = useConfirm()
const prepared = computed(() => Boolean(servers.oauthAttempt))
const formRef = ref(null)
const saving = ref(false)
const locked = computed(() => saving.value || prepared.value)
const form = reactive({ name: '', endpoint: '', bearerToken: '', removeBearerToken: false, enabled: true })
const authMode = ref('static')
const registeredClient = ref(false)
const clientSecret = ref('')
const oauth = reactive({ client_id: '', token_endpoint_auth_method: 'none', scope: '' })
const authModes = [
  { label: 'Ohne Anmeldung / Bearer-Token', value: 'static' },
  { label: 'OAuth', value: 'oauth' }
]
const clientMethods = [
  { label: 'Öffentlicher Client (ohne Secret)', value: 'none' },
  { label: 'Client-Secret im Authorization-Header', value: 'client_secret_basic' },
  { label: 'Client-Secret im Anfrageinhalt', value: 'client_secret_post' }
]
function oauthConfig() {
  return {
    client_id: registeredClient.value ? oauth.client_id : null,
    token_endpoint_auth_method: registeredClient.value ? oauth.token_endpoint_auth_method : 'none',
    scope: oauth.scope || null
  }
}
const needsOAuth = computed(() => authMode.value === 'oauth' && (
  !props.server?.oauth || props.server.authStatus !== 'authorized' || Boolean(clientSecret.value)
  || form.endpoint !== props.server.endpoint
  || JSON.stringify(oauthConfig()) !== JSON.stringify(props.server.oauth)
))
const requiredRule = (value) => Boolean(String(value || '').trim()) || 'Dieses Feld ist erforderlich.'
const urlRule = (value) => {
  try {
    return ['http:', 'https:'].includes(new URL(value).protocol) || 'Gib eine gültige HTTP- oder HTTPS-Adresse ein.'
  } catch {
    return 'Gib eine gültige HTTP- oder HTTPS-Adresse ein.'
  }
}
function assignForm() {
  Object.assign(form, {
    name: props.server?.name || '',
    endpoint: props.server?.endpoint || '',
    bearerToken: '',
    removeBearerToken: false,
    enabled: props.server?.enabled !== false
  })
  authMode.value = props.server?.oauth ? 'oauth' : 'static'
  registeredClient.value = Boolean(props.server?.oauth?.client_id)
  Object.assign(oauth, { client_id: props.server?.oauth?.client_id || '', token_endpoint_auth_method: props.server?.oauth?.token_endpoint_auth_method || 'none', scope: props.server?.oauth?.scope || '' })
  clientSecret.value = ''
}

function close() {
  if (!saving.value) {
    void cancelOAuth()
    clientSecret.value = ''
    emit('update:modelValue', false)
  }
}

async function cancelOAuth() {
  try { await servers.cancelOAuth() } catch { /* A cancelled or expired attempt needs no toast. */ }
}

async function disconnectOAuth() {
  if (!await confirm(`OAuth-Anmeldung für „${props.server.name}“ entfernen?`, 'Anmeldung entfernen', { okLabel: 'Entfernen' })) return
  saving.value = true
  try {
    await servers.disconnectOAuth(props.server)
    $q.notify({ type: 'positive', message: 'OAuth-Anmeldung entfernt' })
    emit('update:modelValue', false)
  } catch { /* The central API interceptor displays the error. */ } finally { saving.value = false }
}

async function connectOAuth() {
  if (!await formRef.value?.validate()) return
  saving.value = true
  try {
    await servers.startOAuth({
      name: form.name, endpoint: form.endpoint, enabled: form.enabled, oauth: oauthConfig(),
      ...(props.server ? { integrationId: props.server.id } : {}),
      ...(registeredClient.value && oauth.token_endpoint_auth_method !== 'none' && clientSecret.value ? { clientSecret: clientSecret.value } : {})
    })
    clientSecret.value = ''
  } catch { /* The central API interceptor displays the error. */ } finally { saving.value = false }
}

async function save() {
  if (needsOAuth.value) return connectOAuth()
  if (!await formRef.value?.validate()) return
  saving.value = true
  try {
    const payload = { ...form }
    payload.oauth = authMode.value === 'oauth' ? oauthConfig() : null
    if (authMode.value === 'oauth') {
      delete payload.bearerToken
      delete payload.removeBearerToken
    }
    if (!payload.bearerToken) delete payload.bearerToken
    if (!props.server) delete payload.removeBearerToken
    if (props.server) await servers.updateServer(props.server.id, payload)
    else await servers.createServer(payload)
    $q.notify({ type: 'positive', message: props.server ? 'MCP-Server gespeichert' : 'MCP-Server hinzugefügt' })
    emit('update:modelValue', false)
  } catch {
    // The central API interceptor displays the error without logging secret request data.
  } finally {
    saving.value = false
  }
}

watch(() => [props.modelValue, props.server], assignForm, { immediate: true })
</script>
