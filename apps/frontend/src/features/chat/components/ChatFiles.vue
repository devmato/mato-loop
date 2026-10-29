<template>
  <div v-if="items.length" class="chat-files">
    <div v-if="label" class="tool-call__section-label">{{ label }}</div>
    <article v-for="attachment in items" :key="attachment.id" class="chat-files__item">
      <div class="chat-files__row row no-wrap items-center text-caption">
        <q-icon :name="attachmentIcon(attachment)" class="app-muted col-auto" size="18px" />
        <span class="col ellipsis" :title="attachmentMetadata(attachment)">{{ attachment.filename || 'Datei' }}</span>
        <q-btn
          v-if="hasDirectPreview(attachment) || (!readOnly && attachment.fileAction?.kind === 'file')"
          flat round dense size="sm" icon="visibility"
          class="chat-files__action col-auto app-muted"
          :aria-label="`${attachment.filename || 'Datei'} ansehen`"
          @click="openAttachment(attachment)"
        />
        <q-btn
          v-if="attachment.kind === 'image' && attachment.safeUrl && !attachment.safeUrl.startsWith('blob:')"
          flat round dense size="sm" icon="open_in_new"
          class="chat-files__action col-auto app-muted"
          :href="attachment.safeUrl"
          target="_blank"
          rel="noopener noreferrer"
          :aria-label="`${attachment.filename || 'Bild'} in neuem Tab öffnen`"
        />
        <q-btn
          v-if="attachment.downloadUrl"
          flat round dense size="sm" icon="download"
          class="chat-files__action col-auto app-muted"
          :href="attachment.downloadUrl"
          :download="attachment.filename || 'datei'"
          :aria-label="`${attachment.filename || 'Datei'} herunterladen`"
        />
        <q-icon v-else name="link_off" class="app-muted" size="18px" aria-label="Keine sichere interne Downloadadresse verfügbar" />
      </div>

      <button
        v-if="attachment.kind === 'image'"
        type="button"
        class="chat-files__image q-mt-xs"
        :disabled="!attachment.safeUrl || loadErrors[attachment.id]"
        :aria-label="`${attachment.filename || 'Bild'} im Vollbild öffnen`"
        @click="openAttachment(attachment)"
      >
        <img
          v-if="attachment.safeUrl && !loadErrors[attachment.id]"
          :src="attachment.safeUrl"
          :alt="attachment.filename || 'Bildanhang'"
          loading="lazy"
          decoding="async"
          @error="loadErrors[attachment.id] = true"
        >
        <span v-else class="chat-files__image-error column items-center justify-center text-caption app-muted">
          <q-icon name="broken_image" size="28px" />
          Bild konnte nicht geladen werden.
        </span>
      </button>

      <template v-else-if="attachment.kind === 'audio'">
        <audio
          v-if="attachment.audioSources.some((source) => audioMimeTypeSupported(source.type))"
          controls preload="metadata" class="full-width q-mt-xs"
          :aria-label="`${attachment.filename || 'Audio'} abspielen`"
        >
          <source v-for="source in attachment.audioSources" :key="source.src" :src="source.src" :type="source.type">
          Dein Browser kann diese Audiodatei nicht wiedergeben.
        </audio>
        <div v-else class="text-caption app-muted q-mt-xs">
          {{ attachment.safeUrl
            ? 'Dein Browser unterstützt die Wiedergabe dieses Audioformats nicht. Du kannst die Datei herunterladen.'
            : 'Die Audiodatei ist nicht über eine sichere interne Adresse verfügbar.' }}
        </div>
      </template>

      <template v-else-if="attachment.kind === 'video'">
        <video
          v-if="attachment.safeUrl && videoMimeTypeSupported(attachment.mimeType)"
          controls preload="metadata" playsinline class="chat-files__video block full-width q-mt-xs"
          :aria-label="`${attachment.filename || 'Video'} abspielen`"
        >
          <source :src="attachment.safeUrl" :type="attachment.mimeType">
          Dein Browser kann diese Videodatei nicht wiedergeben.
        </video>
        <div v-else class="text-caption app-muted q-mt-xs">
          {{ attachment.safeUrl
            ? 'Dein Browser unterstützt die Wiedergabe dieses Videoformats nicht. Du kannst die Datei herunterladen.'
            : 'Die Videodatei ist nicht über eine sichere interne Adresse verfügbar.' }}
        </div>
      </template>
    </article>

    <q-dialog v-model="attachmentDialog" :transition-duration="0" maximized @hide="selectedAttachment = null">
      <q-card v-if="selectedAttachment" class="app-file-dialog">
        <FileViewerPanel
          :image="selectedAttachment.kind === 'image' ? {
            url: selectedAttachment.safeUrl,
            downloadUrl: selectedAttachment.downloadUrl,
            name: selectedAttachment.filename || 'Bild'
          } : null"
          :model="isModel(selectedAttachment) ? {
            url: selectedAttachment.safeUrl,
            downloadUrl: selectedAttachment.downloadUrl,
            name: selectedAttachment.filename || '3D-Modell',
            format: modelFormat(selectedAttachment)
          } : null"
          :pdf="isPdf(selectedAttachment) ? {
            url: selectedAttachment.safeUrl,
            downloadUrl: selectedAttachment.downloadUrl,
            name: selectedAttachment.filename || 'PDF'
          } : null"
          fullscreen
          @close="attachmentDialog = false"
          @toggle-fullscreen="attachmentDialog = false"
        />
      </q-card>
    </q-dialog>
  </div>
</template>

<script setup>
import { reactive, ref, watchEffect } from 'vue'
import {
  attachmentIcon,
  attachmentMetadata,
  audioMimeTypeSupported,
  providedFileAttachments,
  supportedAttachments,
  videoMimeTypeSupported
} from 'src/utils/attachments'
import FileViewerPanel from 'src/features/files/components/FileViewerPanel.vue'
import { useInstanceLimitsStore } from 'stores/instance-limits'

defineOptions({ name: 'ChatFiles' })

const props = defineProps({
  attachments: { type: Array, default: () => [] },
  actions: { type: Array, default: () => [] },
  label: { type: String, default: '' },
  readOnly: Boolean
})
const emit = defineEmits(['open-file'])
const instanceLimits = useInstanceLimitsStore()
const items = ref([])
const loadErrors = reactive({})
watchEffect((onCleanup) => {
  const urls = []
  items.value = supportedAttachments([
    ...props.attachments,
    ...providedFileAttachments(props.actions, instanceLimits)
  ]).map((attachment) => {
    if (!(attachment.file instanceof Blob)) return attachment
    const url = URL.createObjectURL(attachment.file)
    urls.push(url)
    return {
      ...attachment,
      filename: attachment.file.name || attachment.name,
      sizeBytes: attachment.file.size,
      safeUrl: url,
      downloadUrl: url,
      audioSources: attachment.kind === 'audio' ? [{ src: url, type: attachment.mimeType }] : []
    }
  })
  onCleanup(() => urls.forEach((url) => URL.revokeObjectURL(url)))
})
const attachmentDialog = ref(false)
const selectedAttachment = ref(null)

function hasDirectPreview(attachment) {
  return attachment.safeUrl && !loadErrors[attachment.id]
    && (attachment.kind === 'image' || isModel(attachment) || isPdf(attachment))
    && (!attachment.fileAction || attachment.safeUrl !== attachment.downloadUrl)
}

function openAttachment(attachment) {
  if (hasDirectPreview(attachment)) {
    selectedAttachment.value = attachment
    attachmentDialog.value = true
  } else if (!props.readOnly && attachment.fileAction?.kind === 'file') {
    emit('open-file', attachment.fileAction)
  }
}

function modelFormat(attachment) {
  return attachment?.mimeType === 'model/vnd.off' || attachment?.filename?.toLocaleLowerCase().endsWith('.off')
    ? 'off'
    : 'stl'
}

function isPdf(attachment) {
  return attachment?.mimeType === 'application/pdf'
}

function isModel(attachment) {
  return ['model/stl', 'model/vnd.off'].includes(attachment?.mimeType)
    || /\.(stl|off)$/i.test(attachment?.filename || '')
}
</script>
