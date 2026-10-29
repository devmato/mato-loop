<template>
  <div class="markdown-viewer">
    <div
      ref="contentRoot"
      class="chat-markdown"
      @click="handleClick"
      @error.capture="handleImageError"
      @pointerdown="handlePointerDown"
      v-html="rendered.html"
    />

    <q-dialog v-model="mermaidDialog" :transition-duration="0" maximized @hide="clearMermaidImage">
      <q-card v-if="mermaidImage" class="app-file-dialog">
        <ImageViewerPanel
          :image="mermaidImage"
          fullscreen
          @close="mermaidDialog = false"
          @toggle-fullscreen="mermaidDialog = false"
        />
      </q-card>
    </q-dialog>
  </div>
</template>

<script setup>
import 'katex/dist/katex.min.css'

import { copyToClipboard, useQuasar } from 'quasar'
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

import ImageViewerPanel from 'src/features/files/components/ImageViewerPanel.vue'
import { renderMarkdown } from 'src/utils/markdown'
import { verifyExternalImage } from 'src/utils/image-verification'
import { mermaidErrorMessage, renderMermaid } from 'src/utils/mermaid'

defineOptions({ name: 'MessageContent' })

const props = defineProps({
  content: {
    type: String,
    default: ''
  },
  allowImages: {
    type: Boolean,
    default: true
  },
  interactive: {
    type: Boolean,
    default: true
  },
  forceLight: Boolean,
  fileActions: {
    type: Array,
    default: () => []
  },
  streaming: Boolean,
  researchSources: {
    type: Array,
    default: () => []
  },
  preparedReplies: {
    type: Array,
    default: () => []
  },
  trustedImageOrigins: {
    type: Array,
    default: () => []
  }
})
const emit = defineEmits(['open-file', 'prepared-reply', 'rendered', 'trust-image-origin'])
const $q = useQuasar()
const contentRoot = ref(null)
const rendered = ref({ codeBlocks: [], html: '', externalImages: [], mermaidBlocks: [] })
const imageStates = ref({})
const verificationTasks = new Map()
const codeCache = new Map()
const mermaidDialog = ref(false)
const mermaidImage = ref(null)
const STREAM_RENDER_INTERVAL_MS = 50
let renderTimer = null
let linkPointerActive = false
let linkReleaseTimer = null
let streamRenderStarted = false
let renderVersion = 0
let mermaidObjectUrl = ''

function renderContent() {
  if (mermaidDialog.value) mermaidDialog.value = false
  renderTimer = null
  const version = ++renderVersion
  rendered.value = renderMarkdown(props.content, {
    codeCache,
    allowImages: props.allowImages,
    fileActions: props.fileActions,
    imageStates: imageStates.value,
    interactive: props.interactive,
    researchSources: props.researchSources,
    streaming: props.streaming,
    trustedImageOrigins: props.trustedImageOrigins
  })
  syncImageVerifications()
  if (!props.streaming) hydrateMermaid(version)
}

async function hydrateMermaid(version) {
  await nextTick()
  if (version !== renderVersion || !contentRoot.value) return
  const blocks = rendered.value.mermaidBlocks
  for (const placeholder of contentRoot.value.querySelectorAll('[data-markdown-mermaid]')) {
    const blockIndex = Number(placeholder.dataset.markdownMermaid)
    const source = blocks[blockIndex]
    if (typeof source !== 'string') continue
    const dark = !props.forceLight && $q.dark.isActive
    try {
      const svg = await renderMermaid(source, { dark })
      if (version !== renderVersion || !placeholder.isConnected) return
      placeholder.innerHTML = svg
      placeholder.removeAttribute('aria-live')
      placeholder.removeAttribute('role')
      const diagram = placeholder.querySelector('svg')
      if (diagram && !diagram.hasAttribute('aria-label') && !diagram.hasAttribute('aria-labelledby')) {
        diagram.setAttribute('aria-label', 'Mermaid-Diagramm')
      }
    } catch (reason) {
      if (version !== renderVersion || !placeholder.isConnected) return
      const error = document.createElement('div')
      error.className = 'chat-markdown__mermaid-error'
      error.textContent = mermaidErrorMessage(reason)
      placeholder.replaceChildren(error)
      placeholder.removeAttribute('aria-live')
      placeholder.setAttribute('role', 'alert')
    }
  }
  if (version === renderVersion) emit('rendered')
}

function scheduleRender() {
  if (linkPointerActive) return
  if (!props.streaming) {
    streamRenderStarted = false
    if (renderTimer !== null) window.clearTimeout(renderTimer)
    renderContent()
    return
  }
  if (!streamRenderStarted) {
    streamRenderStarted = true
    renderContent()
    return
  }
  if (renderTimer === null) renderTimer = window.setTimeout(renderContent, STREAM_RENDER_INTERVAL_MS)
}

function handlePointerDown(event) {
  const link = event.target.closest?.('a[href]')
  if (!props.streaming || !link || !event.currentTarget.contains(link)) return
  linkPointerActive = true
  if (renderTimer !== null) {
    window.clearTimeout(renderTimer)
    renderTimer = null
  }
  window.clearTimeout(linkReleaseTimer)
  window.addEventListener('pointerup', releaseLinkPointer)
  window.addEventListener('pointercancel', releaseLinkPointer)
}

function releaseLinkPointer() {
  window.removeEventListener('pointerup', releaseLinkPointer)
  window.removeEventListener('pointercancel', releaseLinkPointer)
  linkReleaseTimer = window.setTimeout(() => {
    linkPointerActive = false
    scheduleRender()
  })
}

function setImageState(url, status) {
  imageStates.value = { ...imageStates.value, [url]: status }
  scheduleRender()
}

function verifyImage(image) {
  if (props.streaming || !image?.url || verificationTasks.has(image.url)) return
  setImageState(image.url, 'checking')
  const task = verifyExternalImage(image.url)
  verificationTasks.set(image.url, task)
  task.promise.then((result) => {
    if (verificationTasks.get(image.url) !== task) return
    verificationTasks.delete(image.url)
    if (result.status === 'cancelled') return
    setImageState(image.url, result.status)
  })
}

function syncImageVerifications() {
  const currentUrls = new Set(rendered.value.externalImages.map((image) => image.url))
  let nextStates = imageStates.value
  for (const [url, task] of verificationTasks) {
    if (currentUrls.has(url)) continue
    task.cancel()
    verificationTasks.delete(url)
    if (Object.hasOwn(nextStates, url)) {
      if (nextStates === imageStates.value) nextStates = { ...imageStates.value }
      delete nextStates[url]
    }
  }
  if (nextStates !== imageStates.value) imageStates.value = nextStates
  if (props.streaming) return
  for (const image of rendered.value.externalImages) {
    const trusted = props.trustedImageOrigins.includes(image.origin)
    if (trusted && !imageStates.value[image.url]) verifyImage(image)
  }
}

async function handleClick(event) {
  const fileLink = event.target.closest?.('[data-markdown-file]')
  if (fileLink && event.currentTarget.contains(fileLink)) {
    const action = props.fileActions[Number(fileLink.dataset.markdownFile)]
    if (action) {
      event.preventDefault()
      emit('open-file', action)
      return
    }
  }

  const replyLink = event.target.closest?.('a[href^="#reply-"]')
  if (replyLink && event.currentTarget.contains(replyLink)) {
    const id = replyLink.getAttribute('href').slice('#reply-'.length)
    const reply = props.preparedReplies.find((item) => item?.id === id)
    if (reply && typeof reply.content === 'string') {
      event.preventDefault()
      emit('prepared-reply', reply.content)
      return
    }
  }

  const mermaidButton = event.target.closest?.('.chat-markdown__mermaid-fullscreen')
  if (mermaidButton && event.currentTarget.contains(mermaidButton)) {
    const diagram = mermaidButton.closest('.chat-markdown__mermaid-block')?.querySelector('svg')
    if (diagram) openMermaidImage(diagram.outerHTML)
    return
  }

  const mermaidDownload = event.target.closest?.('.chat-markdown__mermaid-download')
  if (mermaidDownload && event.currentTarget.contains(mermaidDownload)) {
    const diagram = mermaidDownload.closest('.chat-markdown__mermaid-block')?.querySelector('svg')
    if (diagram) downloadFile(diagram.outerHTML, 'image/svg+xml;charset=utf-8', 'Mermaid-Diagramm.svg')
    return
  }

  const sourceDownload = event.target.closest?.('.chat-markdown__source-download')
  if (sourceDownload && event.currentTarget.contains(sourceDownload)) {
    const code = rendered.value.codeBlocks[Number(sourceDownload.dataset.markdownCode)]
    if (typeof code === 'string') downloadFile(code, 'text/plain;charset=utf-8', 'Codeblock.txt')
    return
  }

  const copyButton = event.target.closest?.('.chat-markdown__source-copy')
  if (copyButton && event.currentTarget.contains(copyButton)) {
    const code = rendered.value.codeBlocks[Number(copyButton.dataset.markdownCode)]
    if (typeof code !== 'string') return
    const mermaid = Boolean(copyButton.closest('.chat-markdown__mermaid-block'))
    try {
      await copyToClipboard(code)
      $q.notify({ type: 'positive', message: mermaid ? 'Mermaid-Quelltext kopiert' : 'Code kopiert' })
    } catch {
      $q.notify({
        type: 'negative',
        message: mermaid ? 'Mermaid-Quelltext konnte nicht kopiert werden' : 'Code konnte nicht kopiert werden'
      })
    }
    return
  }

  const button = event.target.closest?.('[data-markdown-image]')
  if (!button || !event.currentTarget.contains(button)) return
  const image = rendered.value.externalImages[Number(button.dataset.markdownImage)]
  if (!image?.origin) return
  if (!props.trustedImageOrigins.includes(image.origin)) emit('trust-image-origin', image.origin)
  verifyImage(image)
}

function downloadFile(content, type, name) {
  const url = URL.createObjectURL(new Blob([content], { type }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function openMermaidImage(svg) {
  clearMermaidImage()
  mermaidObjectUrl = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml;charset=utf-8' }))
  mermaidImage.value = { url: mermaidObjectUrl, name: 'Mermaid-Diagramm.svg' }
  mermaidDialog.value = true
}

function clearMermaidImage() {
  if (mermaidObjectUrl) URL.revokeObjectURL(mermaidObjectUrl)
  mermaidObjectUrl = ''
  mermaidImage.value = null
}

function handleImageError(event) {
  const target = event.target
  if (!target.matches?.('img[data-markdown-image]')) return
  const image = rendered.value.externalImages[Number(target.dataset.markdownImage)]
  if (image?.url) setImageState(image.url, 'failed')
}

watch(
  () => [
    props.allowImages,
    props.content,
    props.forceLight,
    props.interactive,
    // Only filenames affect markup; clicks resolve the current file targets.
    JSON.stringify(props.fileActions.map((action) => action.filename)),
    props.researchSources,
    props.streaming,
    props.trustedImageOrigins,
    $q.dark.isActive
  ],
  scheduleRender,
  { immediate: true }
)

onBeforeUnmount(() => {
  renderVersion += 1
  if (renderTimer !== null) window.clearTimeout(renderTimer)
  window.clearTimeout(linkReleaseTimer)
  window.removeEventListener('pointerup', releaseLinkPointer)
  window.removeEventListener('pointercancel', releaseLinkPointer)
  clearMermaidImage()
  for (const task of verificationTasks.values()) task.cancel()
  verificationTasks.clear()
})
</script>
