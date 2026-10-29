<template>
  <section class="column full-height no-wrap" aria-label="E-Mail">
    <div v-if="mail.accountsLoading && !mail.accountsLoaded" class="col flex flex-center">
      <q-spinner color="primary" size="40px" aria-label="Mail-Konten werden geladen" />
    </div>
    <AppStateMessage
      v-else-if="mail.accountsLoadFailed && !mail.accountsLoaded"
      class="col q-pa-xl"
      icon="cloud_off"
      title="Mail-Konten konnten nicht geladen werden"
    >
      <template #actions>
        <q-btn outline no-caps color="primary" label="Erneut laden" @click="loadAccounts" />
      </template>
    </AppStateMessage>
    <AppStateMessage
      v-else-if="mail.accountsLoaded && !mail.accounts.length"
      class="col q-pa-xl"
      icon="mail_outline"
      title="Kein Mail-Konto verfügbar"
      description="Richte in den Einstellungen ein aktives Mail-Konto ein."
    />

    <template v-else>
      <div class="row q-col-gutter-sm q-pa-sm">
        <div class="col-12 col-sm">
          <AppSelect
            v-model="selectedAccountId"
            outlined
            dense
            emit-value
            map-options
            label="Konto"
            :options="accountOptions"
            @update:model-value="selectAccount"
          />
        </div>
        <div class="col-12 col-sm">
          <AppSelect
            v-model="selectedMailboxKey"
            outlined
            dense
            emit-value
            map-options
            label="Ordner"
            :options="mailboxOptions"
            :loading="mail.mailboxesLoading"
            :disable="mail.mailboxesLoading || !mail.mailboxes.length"
            @update:model-value="selectMailbox"
          />
        </div>
        <div class="col-12 col-sm-auto row items-center justify-end no-wrap q-gutter-xs">
          <q-btn
            flat
            round
            dense
            icon="edit"
            aria-label="E-Mail verfassen"
            :disable="!activeAccount"
            @click="openCompose()"
          >
            <q-tooltip>E-Mail verfassen</q-tooltip>
          </q-btn>
          <q-btn
            flat
            round
            dense
            icon="refresh"
            aria-label="E-Mails aktualisieren"
            :loading="mail.accountsLoading || mail.messagesLoading"
            @click="refresh"
          >
            <q-tooltip>E-Mails aktualisieren</q-tooltip>
          </q-btn>
        </div>
      </div>
      <q-separator />

      <AppStateMessage v-if="mail.mailboxesLoadFailed" class="col q-pa-lg" title="Die Mail-Ordner konnten nicht geladen werden.">
        <template #actions>
          <q-btn outline no-caps color="primary" label="Erneut laden" @click="selectAccount(selectedAccountId)" />
        </template>
      </AppStateMessage>
      <div v-else class="mail-layout col">
        <div v-show="!mobileDetail" class="column no-wrap mail-list-pane">
          <q-form class="row items-center no-wrap q-pa-sm q-gutter-sm" @submit.prevent="searchMessages">
            <q-input
              v-model="searchQuery"
              class="col"
              outlined
              dense
              clearable
              maxlength="500"
              label="In diesem Ordner suchen"
              :disable="!selectedMailboxKey"
              @clear="searchMessages"
            />
            <q-btn flat round icon="search" type="submit" aria-label="E-Mails suchen" :disable="!selectedMailboxKey" />
          </q-form>
          <q-scroll-area class="col">
            <q-list separator>
              <q-item v-if="mail.messagesLoading && !mail.messages.length" class="q-pa-lg">
                <q-item-section><q-skeleton v-for="row in 4" :key="row" type="text" /></q-item-section>
              </q-item>
              <q-item v-else-if="mail.messagesLoadFailed && !mail.messages.length" class="q-pa-lg">
                <q-item-section>
                  <q-item-label>E-Mails konnten nicht geladen werden.</q-item-label>
                  <q-btn flat no-caps color="primary" label="Erneut laden" class="self-start q-mt-sm" @click="refresh" />
                </q-item-section>
              </q-item>
              <q-item v-else-if="!mail.messages.length && !mail.messagesLoading" class="q-pa-lg">
                <q-item-section class="text-center app-muted">{{ mail.query ? 'Keine E-Mails gefunden.' : 'Dieser Ordner ist leer.' }}</q-item-section>
              </q-item>
              <q-item
                v-for="message in mail.messages"
                :key="message.token"
                clickable
                v-ripple
                :active="mail.detail?.token === message.token"
                active-class="app-surface-tonal text-primary"
                @click="openMessage(message)"
              >
                <q-item-section avatar>
                  <q-icon :name="message.isDraft ? 'drafts' : message.unread ? 'mark_email_unread' : 'mail'" :color="message.unread ? 'primary' : undefined" />
                </q-item-section>
                <q-item-section>
                  <q-item-label :class="{ 'text-weight-bold': message.unread }" lines="1">{{ message.subject || '(Kein Betreff)' }}</q-item-label>
                  <q-item-label caption lines="1">{{ sender(message) }}</q-item-label>
                  <q-item-label caption lines="2">{{ message.snippet || message.preview || '' }}</q-item-label>
                </q-item-section>
                <q-item-section side top>
                  <span class="text-caption">{{ formatAbsoluteDateTime(message.receivedAt || message.sentAt || message.date) }}</span>
                  <q-icon v-if="message.hasAttachments" name="attach_file" size="16px" class="q-mt-xs" />
                </q-item-section>
              </q-item>
            </q-list>
            <div v-if="mail.nextCursor || (mail.messagesLoadFailed && mail.messages.length)" class="q-pa-sm">
              <q-btn
                outline
                no-caps
                color="primary"
                class="full-width"
                :label="mail.messagesLoadFailed ? 'Erneut versuchen' : 'Weitere laden'"
                :loading="mail.messagesLoading"
                @click="loadMore"
              />
            </div>
          </q-scroll-area>
        </div>

        <div v-show="!$q.screen.lt.md || mobileDetail" class="column no-wrap mail-detail-pane">
          <q-toolbar v-if="$q.screen.lt.md && mobileDetail">
            <q-btn flat round dense icon="arrow_back" aria-label="Zurück zur E-Mail-Liste" @click="backToList" />
            <q-toolbar-title class="text-subtitle1">E-Mail</q-toolbar-title>
          </q-toolbar>
          <q-separator v-if="$q.screen.lt.md && mobileDetail" />
          <div v-if="mail.detailLoading" class="col flex flex-center"><q-spinner color="primary" size="36px" /></div>
          <AppStateMessage v-else-if="mail.detailLoadFailed" class="col q-pa-lg" title="Die E-Mail konnte nicht geladen werden." />
          <AppStateMessage v-else-if="!mail.detail" class="col q-pa-xl" icon="mail_outline" description="Wähle eine E-Mail aus." />
          <q-scroll-area v-else class="col">
            <article class="q-pa-md">
              <div class="row items-start no-wrap q-gutter-sm">
                <div class="col">
                  <h3 class="text-h6 q-my-none">{{ mail.detail.subject || '(Kein Betreff)' }}</h3>
                  <div class="text-body2 q-mt-sm">Von: {{ sender(mail.detail) }}</div>
                  <div class="text-caption app-muted">An: {{ recipients(mail.detail.to) }}</div>
                  <div v-if="mail.detail.cc?.length" class="text-caption app-muted">Cc: {{ recipients(mail.detail.cc) }}</div>
                  <time class="text-caption app-muted" :datetime="mail.detail.receivedAt || mail.detail.sentAt || mail.detail.date">
                    {{ formatAbsoluteDateTime(mail.detail.receivedAt || mail.detail.sentAt || mail.detail.date) }}
                  </time>
                </div>
                <q-btn v-if="mail.detail.isDraft" flat round dense icon="edit" aria-label="Entwurf bearbeiten" @click="openCompose(mail.detail)" />
                <q-btn flat round dense color="negative" icon="delete" aria-label="E-Mail löschen" @click="deleteCurrent" />
              </div>
              <q-separator class="q-my-md" />
              <div class="mail-plain-text">{{ plainBody }}</div>
              <div v-if="mail.detail.attachments?.length" class="q-mt-lg">
                <div class="text-subtitle2 q-mb-xs">Anhänge</div>
                <q-list bordered separator class="rounded-borders">
                  <q-item v-for="attachment in mail.detail.attachments" :key="attachment.token" clickable tag="a" :href="attachmentUrl(attachment.token)" download>
                    <q-item-section avatar><q-icon name="attach_file" /></q-item-section>
                    <q-item-section>{{ attachment.filename || attachment.name || 'Anhang' }}</q-item-section>
                    <q-item-section side><q-icon name="download" /></q-item-section>
                  </q-item>
                </q-list>
              </div>
            </article>
          </q-scroll-area>
        </div>
      </div>
    </template>

    <q-dialog v-model="composeOpen" :transition-duration="0" :maximized="$q.screen.lt.sm" :persistent="composeSaving">
      <q-card class="mail-compose app-surface column">
        <q-card-section class="row items-center no-wrap">
          <div class="text-h6">{{ compose.token ? 'Entwurf bearbeiten' : 'E-Mail verfassen' }}</div>
          <q-space />
          <q-btn flat round dense icon="close" aria-label="Editor schließen" :disable="composeSaving" @click="composeOpen = false" />
        </q-card-section>
        <q-separator />
        <q-form class="col column no-wrap" @submit.prevent="sendCompose">
          <q-card-section class="col scroll q-gutter-md">
            <q-banner v-if="compose.sendPayload" rounded class="bg-warning text-dark">
              Der Versandstatus ist unklar. Ein erneuter Versuch verwendet unverändert dieselbe E-Mail.
            </q-banner>
             <q-input dense v-model="compose.to" outlined label="An" hint="Mehrere Adressen mit Komma trennen." :disable="Boolean(compose.sendPayload)" />
            <div class="row q-col-gutter-sm">
              <q-input dense v-model="compose.cc" outlined label="Cc" class="col-12 col-sm-6" :disable="Boolean(compose.sendPayload)" />
              <q-input dense v-model="compose.bcc" outlined label="Bcc" class="col-12 col-sm-6" :disable="Boolean(compose.sendPayload)" />
            </div>
            <q-input dense v-model="compose.subject" outlined label="Betreff" :disable="Boolean(compose.sendPayload)" />
            <q-input dense v-model="compose.body" outlined type="textarea" autogrow label="Nachricht" :disable="Boolean(compose.sendPayload)" />
            <q-file dense v-model="compose.files" outlined multiple use-chips append clearable label="Anhänge hinzufügen" :disable="Boolean(compose.sendPayload)">
              <template #prepend><q-icon name="attach_file" /></template>
            </q-file>
            <q-list v-if="compose.existingAttachments.length" bordered separator class="rounded-borders">
              <q-item v-for="attachment in compose.existingAttachments" :key="attachment.token">
                <q-item-section avatar><q-icon name="attach_file" /></q-item-section>
                <q-item-section>{{ attachment.filename || attachment.name || 'Anhang' }}</q-item-section>
                <q-item-section side>
                  <div class="row no-wrap">
                    <q-btn flat round dense icon="download" :href="attachmentUrl(attachment.token)" download aria-label="Anhang herunterladen" />
                    <q-btn flat round dense icon="close" aria-label="Anhang aus Entwurf entfernen" :disable="Boolean(compose.sendPayload)" @click="removeExistingAttachment(attachment.token)" />
                  </div>
                </q-item-section>
              </q-item>
            </q-list>
          </q-card-section>
          <q-separator />
          <q-card-actions class="q-pa-md">
            <q-btn v-if="compose.token" flat no-caps color="negative" label="Entwurf löschen" :disable="Boolean(composeSaving) || Boolean(compose.sendPayload)" @click="deleteComposeDraft" />
            <q-space />
            <q-btn flat no-caps label="Als Entwurf speichern" :loading="composeSaving === 'draft'" :disable="Boolean(composeSaving) || Boolean(compose.sendPayload)" @click="saveComposeDraft" />
            <q-btn unelevated no-caps type="submit" color="primary" icon="send" :label="compose.sendPayload ? 'Erneut senden' : 'Senden'" :loading="composeSaving === 'send'" :disable="Boolean(composeSaving)" />
          </q-card-actions>
        </q-form>
      </q-card>
    </q-dialog>
  </section>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import { useConfirm } from 'src/composables/confirm'
import { formatAbsoluteDateTime } from 'src/utils/date-time'
import { randomUuid } from 'src/utils/random-id'
import { useMailStore } from 'stores/mail'

defineOptions({ name: 'MailApp' })
const $q = useQuasar()
const { confirm } = useConfirm()
const mail = useMailStore()
const selectedAccountId = ref(null)
const selectedMailboxKey = ref(null)
const selectedToken = ref(null)
const searchQuery = ref('')
const composeOpen = ref(false)
const composeSaving = ref(false)
const compose = reactive(emptyCompose())
const activeAccount = computed(() => mail.accounts.find((account) => account.id === selectedAccountId.value))
const accountOptions = computed(() => mail.accounts.map((account) => ({ label: account.name, value: account.id })))
const mailboxOptions = computed(() => mail.mailboxes.map((mailbox) => ({
  label: `${mailbox.label || mailbox.name}${mailbox.unreadCount ? ` (${mailbox.unreadCount})` : ''}`,
  value: mailbox.key
})))
const mobileDetail = computed(() => $q.screen.lt.md && Boolean(selectedToken.value))
const plainBody = computed(() => mail.detail?.text || mail.detail?.bodyText || mail.detail?.body?.content || (typeof mail.detail?.body === 'string' ? mail.detail.body : ''))

function emptyCompose() {
  return {
    token: null, to: '', cc: '', bcc: '', subject: '', body: '', files: [], existingAttachments: [],
    idempotencyKey: randomUuid(), sendPayload: null
  }
}

function addresses(value) {
  return String(value || '').split(',').map((address) => address.trim()).filter(Boolean)
}

function composePayload() {
  return {
    to: addresses(compose.to), cc: addresses(compose.cc), bcc: addresses(compose.bcc),
    subject: compose.subject.trim(), body: compose.body,
    files: compose.files || [], existingAttachments: compose.existingAttachments
  }
}

function sender(message) {
  const value = message.from || message.sender
  if (typeof value === 'string') return value
  return value?.name ? `${value.name} <${value.address}>` : value?.address || 'Unbekannt'
}

function recipients(value) {
  const values = Array.isArray(value) ? value : value ? [value] : []
  return values.map((recipient) => typeof recipient === 'string'
    ? recipient
    : recipient.name ? `${recipient.name} <${recipient.address}>` : recipient.address).filter(Boolean).join(', ')
}

function attachmentUrl(token) {
  return `/api/mail/accounts/${encodeURIComponent(selectedAccountId.value)}/attachments/${encodeURIComponent(token)}`
}

async function loadAccounts() {
  try {
    await mail.fetchAccounts()
    if (!mail.accounts.length) return
    const accountId = mail.accounts.some((account) => account.id === selectedAccountId.value)
      ? selectedAccountId.value
      : mail.accounts[0].id
    await selectAccount(accountId)
  } catch {
    // Axios errors are displayed centrally; confirmed account state remains visible.
  }
}

async function selectAccount(accountId) {
  selectedAccountId.value = accountId
  selectedMailboxKey.value = null
  selectedToken.value = null
  searchQuery.value = ''
  try {
    const mailboxes = await mail.fetchMailboxes(accountId)
    if (!mailboxes?.length || selectedAccountId.value !== accountId) return
    const mailbox = mailboxes.find((item) => item.role === 'inbox' || item.specialUse === 'inbox') || mailboxes[0]
    selectedMailboxKey.value = mailbox.key
    await mail.fetchMessages(accountId, mailbox.key)
  } catch {
    // Axios errors are displayed centrally; the retry state remains available.
  }
}

async function selectMailbox(mailboxKey) {
  selectedMailboxKey.value = mailboxKey
  selectedToken.value = null
  searchQuery.value = ''
  try {
    await mail.fetchMessages(selectedAccountId.value, mailboxKey)
  } catch {
    // Axios errors are displayed centrally; confirmed messages remain available on pagination failure.
  }
}

async function refresh() {
  if (!selectedAccountId.value || !selectedMailboxKey.value) return loadAccounts()
  try {
    await mail.fetchMessages(selectedAccountId.value, selectedMailboxKey.value, null, mail.query)
  } catch {
    // Axios errors are displayed centrally.
  }
}

async function searchMessages() {
  if (!selectedAccountId.value || !selectedMailboxKey.value) return
  selectedToken.value = null
  mail.clearDetail()
  try {
    await mail.fetchMessages(selectedAccountId.value, selectedMailboxKey.value, null, searchQuery.value)
  } catch {
    // Axios errors are displayed centrally.
  }
}

async function loadMore() {
  try {
    await mail.fetchMessages(selectedAccountId.value, selectedMailboxKey.value, mail.nextCursor, mail.query)
  } catch {
    // Existing messages remain canonical and visible.
  }
}

async function openMessage(message) {
  selectedToken.value = message.token
  mail.clearDetail()
  try {
    const detail = await mail.fetchMessage(selectedAccountId.value, message.token)
    if (detail && message.isDraft) detail.isDraft = true
  } catch {
    // Axios errors are displayed centrally.
  }
}

function backToList() {
  selectedToken.value = null
  mail.clearDetail()
}

function openCompose(draft = null) {
  if (draft?.bodyTruncated || draft?.attachmentsTruncated) {
    $q.notify({
      type: 'warning',
      message: 'Dieser Entwurf ist zu groß, um ihn ohne Datenverlust zu bearbeiten.'
    })
    return
  }
  Object.assign(compose, emptyCompose(), draft ? {
    token: draft.token,
    to: recipients(draft.to), cc: recipients(draft.cc), bcc: recipients(draft.bcc),
    subject: draft.subject || '', body: draft.text || draft.bodyText || draft.body || '',
    existingAttachments: draft.attachments || []
  } : {})
  composeOpen.value = true
}

async function saveComposeDraft() {
  composeSaving.value = 'draft'
  try {
    const draft = await mail.saveDraft(selectedAccountId.value, compose.token, composePayload())
    compose.token = draft.token
    compose.existingAttachments = draft.attachments || compose.existingAttachments
    compose.files = []
    selectedToken.value = draft.token
    $q.notify({ type: 'positive', message: 'Entwurf gespeichert' })
    const [detailResult] = await Promise.allSettled([
      mail.fetchMessage(selectedAccountId.value, draft.token),
      refresh()
    ])
    if (detailResult.status === 'fulfilled' && detailResult.value) {
      detailResult.value.isDraft = true
      compose.existingAttachments = detailResult.value.attachments || []
    }
    return draft
  } catch {
    return null
  } finally {
    composeSaving.value = false
  }
}

async function sendCompose() {
  if (![...addresses(compose.to), ...addresses(compose.cc), ...addresses(compose.bcc)].length) {
    $q.notify({ type: 'warning', message: 'Gib mindestens einen Empfänger an.' })
    return
  }
  composeSaving.value = 'send'
  try {
    const payload = compose.sendPayload || composePayload()
    compose.sendPayload = payload
    const result = compose.token
      ? await mail.sendDraft(
        selectedAccountId.value, compose.token, payload, compose.idempotencyKey
      )
      : await mail.sendMessage(selectedAccountId.value, payload, compose.idempotencyKey)
    composeOpen.value = false
    $q.notify({
      type: result.sentCopyConfirmed === true ? 'positive' : 'warning',
      message: result.sentCopyConfirmed === true
        ? 'E-Mail an den Mailserver übergeben und unter „Gesendet“ gespeichert.'
        : result.sentCopyConfirmed === false
          ? 'E-Mail an den Mailserver übergeben. Die Kopie unter „Gesendet“ konnte nicht bestätigt werden. Nicht erneut senden.'
          : 'E-Mail an den Mailserver übergeben. Die Ablage unter „Gesendet“ ist unbekannt. Nicht erneut senden.'
    })
    await refresh()
  } catch {
    // The editor and confirmed mail state remain available for retry.
  } finally {
    composeSaving.value = false
  }
}

function removeExistingAttachment(token) {
  compose.existingAttachments = compose.existingAttachments.filter((attachment) => attachment.token !== token)
}

async function deleteComposeDraft() {
  if (!await confirm('Diesen Entwurf endgültig löschen?', 'Entwurf löschen', { okLabel: 'Löschen', okColor: 'negative' })) return
  composeSaving.value = 'delete'
  try {
    await mail.deleteDraft(selectedAccountId.value, compose.token)
    composeOpen.value = false
    selectedToken.value = null
    $q.notify({ type: 'positive', message: 'Entwurf gelöscht' })
  } catch {
    // The draft remains visible after a failed request.
  } finally {
    composeSaving.value = false
  }
}

async function deleteCurrent() {
  if (!mail.detail) return
  const draft = mail.detail.isDraft
  if (!await confirm(
    draft ? 'Diesen Entwurf endgültig löschen?' : 'Diese E-Mail endgültig löschen?',
    draft ? 'Entwurf löschen' : 'E-Mail löschen',
    { okLabel: 'Löschen', okColor: 'negative' }
  )) return
  try {
    if (draft) await mail.deleteDraft(selectedAccountId.value, mail.detail.token)
    else await mail.deleteMessage(selectedAccountId.value, mail.detail.token)
    selectedToken.value = null
    $q.notify({ type: 'positive', message: draft ? 'Entwurf gelöscht' : 'E-Mail gelöscht' })
  } catch {
    // The current message remains visible after a failed request.
  }
}

watch(() => mail.accounts, (accounts) => {
  if (!accounts.length) {
    selectedAccountId.value = null
    selectedMailboxKey.value = null
    selectedToken.value = null
    mail.clearDetail()
  } else if (selectedAccountId.value && !accounts.some((account) => account.id === selectedAccountId.value)) {
    void selectAccount(accounts[0].id)
  }
})
onMounted(loadAccounts)
</script>

<style scoped>
.mail-layout {
  display: grid;
  grid-template-columns: minmax(15rem, 38%) minmax(0, 1fr);
  min-height: 0;
}

.mail-list-pane {
  min-width: 0;
  border-right: 1px solid var(--app-surface-border);
}

.mail-detail-pane {
  min-width: 0;
  min-height: 0;
}

.mail-plain-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.mail-compose {
  width: min(48rem, calc(100vw - 2rem));
  max-height: min(52rem, calc(100vh - 2rem));
}

@media (max-width: 1023px) {
  .mail-layout {
    display: block;
    min-height: 0;
  }

  .mail-list-pane,
  .mail-detail-pane {
    height: 100%;
    border-right: 0;
  }
}
</style>
