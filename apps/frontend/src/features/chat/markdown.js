import { tex } from '@mdit/plugin-tex'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import katex from 'katex'
import MarkdownIt from 'markdown-it'
import taskLists from 'markdown-it-task-lists'

import { safeAttachmentUrl, safeFileDownloadUrl } from 'src/features/files/attachments'
import { canonicalResearchSourceUrl } from './research-sources'

const ALLOWED_TAGS = [
  'a', 'annotation', 'blockquote', 'br', 'button', 'code', 'del', 'div', 'em', 'h1', 'h2',
  'h3', 'h4', 'h5', 'h6', 'hr', 'img', 'input', 'li', 'math', 'menclose', 'merror',
  'mfrac', 'mi', 'mn', 'mo', 'mover', 'mpadded', 'mphantom', 'mroot', 'mrow', 'mspace',
  'msqrt', 'mstyle', 'msub', 'msubsup', 'msup', 'mtable', 'mtd', 'mtext', 'mtr',
  'munder', 'munderover', 'ol', 'p', 'path', 'pre', 's', 'semantics', 'span', 'strong',
  'svg', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'ul'
]
const ALLOWED_ATTR = [
  'accent', 'accentunder', 'alt', 'aria-hidden', 'aria-label', 'aria-live', 'checked',
  'class', 'columnalign', 'columnlines', 'columnspacing', 'd', 'data-markdown-code', 'data-markdown-image',
  'data-markdown-file', 'data-markdown-mermaid',
  'decoding', 'disabled', 'display', 'displaystyle', 'encoding', 'fence', 'height', 'hidden',
  'href', 'loading', 'lspace', 'mathbackground', 'mathcolor', 'mathvariant', 'maxsize',
  'minsize', 'movablelimits', 'preserveaspectratio', 'referrerpolicy', 'rel', 'role',
  'rowlines', 'rowspacing', 'scriptlevel', 'separator', 'src', 'stretchy', 'style',
  'symmetric', 'target', 'title', 'type', 'viewbox', 'voffset', 'width', 'xmlns'
]
const LINK_PROTOCOL = /^(?:https?:|mailto:)/i
const IMAGE_PROTOCOL = /^https?:/i
const SAFE_FRAGMENT = /^#[A-Za-z0-9_-]+$/
const LANGUAGE_NAME = /^[A-Za-z0-9_+-]+$/
const STREAMING_MARKDOWN_ENVELOPE = /^ {0,3}(`{3,}|~{3,})[\t ]*(?:markdown|md)[\t ]*\r?\n/i
const MAX_MERMAID_BLOCKS = 12

function currentOrigin() {
  return typeof window !== 'undefined' && window.location?.origin
    ? window.location.origin
    : 'http://localhost'
}

function normalizedLanguage(value) {
  const language = String(value || '').trim().split(/\s+/)[0]
  return language && LANGUAGE_NAME.test(language) ? language : ''
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function highlightedCode(source, languageName) {
  const language = normalizedLanguage(languageName)
  if (language && hljs.getLanguage(language)) {
    try {
      const result = hljs.highlight(source, { language, ignoreIllegals: true })
      return `<pre class="chat-markdown__code"><code class="hljs language-${language}">${result.value}</code></pre>`
    } catch {
      // Unknown language aliases and malformed snippets remain readable as plain code.
    }
  }
  return `<pre class="chat-markdown__code"><code>${escapeHtml(source)}</code></pre>`
}

function isClosedStreamingFence(token, source) {
  const endLine = token.map?.[1]
  const marker = token.markup
  if (!endLine || !marker) return false
  const line = String(source || '').split(/\r?\n/)[endLine - 1]
  if (line === undefined) return false
  const candidate = line.trimStart()
  let markerLength = 0
  while (candidate[markerLength] === marker[0]) markerLength += 1
  return markerLength >= marker.length
    && candidate.slice(markerLength).trim() === ''
    && line.length - candidate.length <= 3
}

function streamingMarkdownSource(content) {
  const source = String(content || '')
  const opening = STREAMING_MARKDOWN_ENVELOPE.exec(source)
  if (!opening) return source
  const body = source.slice(opening[0].length)
  const marker = opening[1]
  const closing = new RegExp(
    `(?:^|\\r?\\n) {0,3}${marker[0]}{${marker.length},}[\\t ]*(?:\\r?\\n[\\t ]*)*$`
  )
  return body.replace(closing, '')
}

function renderedMath(content, displayMode) {
  try {
    return katex.renderToString(content, {
      displayMode,
      macros: {},
      maxExpand: 1000,
      maxSize: 20,
      output: 'htmlAndMathml',
      strict: 'error',
      throwOnError: true,
      trust: false
    })
  } catch {
    const delimiter = displayMode ? '$$' : '$'
    return `<span class="chat-markdown__math-error">${delimiter}${escapeHtml(content)}${delimiter}</span>`
  }
}

function precedingBackslashes(source, position, minimum = 0) {
  let count = 0
  let index = position - 1
  while (index >= minimum && source.charCodeAt(index) === 92) {
    count += 1
    index -= 1
  }
  return count
}

function dollarBoundary(state, position) {
  const before = state.src.charCodeAt(position - 1)
  const after = state.src.charCodeAt(position + 1)
  return {
    canClose: !(after >= 48 && after <= 57) && !state.md.utils.isSpace(before),
    canOpen: !state.md.utils.isSpace(after)
  }
}

function literalDollar(state, silent) {
  if (!silent) state.pending += '$'
  state.pos += 1
  return true
}

function safeInlineDollar(state, silent) {
  if (state.src[state.pos] !== '$') return false
  if (!dollarBoundary(state, state.pos).canOpen) return literalDollar(state, silent)

  let candidate = state.pos + 1
  while ((candidate = state.src.indexOf('$', candidate)) !== -1) {
    if (precedingBackslashes(state.src, candidate, state.pos + 1) % 2 === 1) {
      candidate += 1
      continue
    }
    const intervening = state.src.slice(state.pos + 1, candidate)
    if (intervening.includes('\n') || intervening.includes('`')) {
      return literalDollar(state, silent)
    }
    const boundary = dollarBoundary(state, candidate)
    if (candidate > state.pos + 1 && boundary.canClose) {
      if (!silent) {
        const token = state.push('math_inline', 'math', 0)
        token.content = intervening
        token.markup = '$'
      }
      state.pos = candidate + 1
      return true
    }
    if (boundary.canOpen) return literalDollar(state, silent)
    candidate += 1
  }
  return literalDollar(state, silent)
}

function safeHtmlBreak(state, silent) {
  const match = /^<br\s*\/?>/i.exec(state.src.slice(state.pos))
  if (!match) return false
  if (!silent) state.push('hardbreak', 'br', 0)
  state.pos += match[0].length
  return true
}

const markdown = new MarkdownIt({
  breaks: true,
  highlight: highlightedCode,
  html: false,
  linkify: true,
  typographer: false
})
  .use(taskLists, { enabled: false })
  .use(tex, {
    allowInlineWithSpace: false,
    delimiters: 'all',
    mathFence: false,
    render: renderedMath
  })

markdown.inline.ruler.at('math_inline_dollar', safeInlineDollar)
markdown.inline.ruler.before('text', 'safe_html_break', safeHtmlBreak)

DOMPurify.addHook('uponSanitizeAttribute', (node, data) => {
  if (data.attrName === 'style' && !node.closest?.('.katex')) data.keepAttr = false
})

export function safeMarkdownLinkUrl(value) {
  const raw = String(value || '').trim()
  if (SAFE_FRAGMENT.test(raw)) return raw
  if (safeFileDownloadUrl(raw)) return raw
  if (!LINK_PROTOCOL.test(raw) && !raw.startsWith('//')) return null
  try {
    const parsed = new URL(raw, currentOrigin())
    if (!['http:', 'https:', 'mailto:'].includes(parsed.protocol)) return null
    if (parsed.username || parsed.password) return null
    return parsed.href
  } catch {
    return null
  }
}

export function classifyMarkdownImageUrl(value) {
  const raw = String(value || '').trim()
  const directAttachment = safeAttachmentUrl(raw)
  if (directAttachment) {
    return { kind: 'internal', url: directAttachment, origin: currentOrigin(), host: '' }
  }
  if (!IMAGE_PROTOCOL.test(raw) && !raw.startsWith('//')) return { kind: 'invalid' }
  try {
    const parsed = new URL(raw, currentOrigin())
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
      return { kind: 'invalid' }
    }
    if (
      parsed.origin === currentOrigin()
      && !parsed.search
      && !parsed.hash
      && safeAttachmentUrl(parsed.pathname)
    ) {
      return { kind: 'internal', url: parsed.pathname, origin: parsed.origin, host: parsed.host }
    }
    return { kind: 'external', url: parsed.href, origin: parsed.origin, host: parsed.host }
  } catch {
    return { kind: 'invalid' }
  }
}

function imageHtml(image, alt, title, requestId = null) {
  const safeAlt = markdown.utils.escapeHtml(alt)
  const safeTitle = title ? ` title="${markdown.utils.escapeHtml(title)}"` : ''
  const referrerPolicy = image.kind === 'external' ? ' referrerpolicy="no-referrer"' : ''
  const requestAttribute = requestId === null ? '' : ` data-markdown-image="${requestId}"`
  return `<img class="chat-markdown__image" src="${markdown.utils.escapeHtml(image.url)}" alt="${safeAlt}"${safeTitle} loading="lazy" decoding="async"${referrerPolicy}${requestAttribute}>`
}

function configureRenderRules() {
  markdown.renderer.rules.fence = (tokens, index, options, env) => {
    const token = tokens[index]
    const language = normalizedLanguage(token.info).toLowerCase()
    if (
      language === 'mermaid'
      && env.mermaidBlocks.length < MAX_MERMAID_BLOCKS
    ) {
      const mermaidIndex = String(env.mermaidBlocks.length)
      env.mermaidBlocks.push(token.content)
      if (env.streaming && !isClosedStreamingFence(token, env.source)) {
        return `<div class="chat-markdown__mermaid-block chat-markdown__mermaid-block--streaming">
          <div class="chat-markdown__mermaid-output" role="status" aria-live="polite">
            <span class="chat-markdown__mermaid-status">Diagramm wird erstellt …</span>
          </div>
        </div>`
      }
      let actions = ''
      if (env.interactive && !env.streaming) {
        const codeIndex = String(env.codeBlocks.length)
        env.codeBlocks.push(token.content)
        actions = `<div class="chat-markdown__block-actions">
          <button type="button" class="chat-markdown__block-action chat-markdown__mermaid-download material-icons" aria-label="Mermaid-Diagramm herunterladen" title="Diagramm herunterladen">download</button>
          <button type="button" class="chat-markdown__block-action chat-markdown__source-copy material-icons" data-markdown-code="${codeIndex}" aria-label="Mermaid-Quelltext kopieren" title="Quelltext kopieren">content_copy</button>
          <button type="button" class="chat-markdown__block-action chat-markdown__mermaid-fullscreen material-icons" aria-label="Diagramm im Vollbild öffnen" title="Im Vollbild öffnen">fullscreen</button>
        </div>`
      }
      return `<div class="chat-markdown__mermaid-block">
        ${actions}
        <div class="chat-markdown__mermaid-output" data-markdown-mermaid="${mermaidIndex}" role="status" aria-live="polite">
          <span class="chat-markdown__mermaid-status">Diagramm wird erstellt …</span>
        </div>
      </div>`
    }
    const cacheKey = `${token.info}\0${token.content}`
    const code = env.codeCache.get(cacheKey) ?? highlightedCode(token.content, token.info)
    env.nextCodeCache.set(cacheKey, code)
    if (!env.interactive) return code
    const codeIndex = String(env.codeBlocks.length)
    env.codeBlocks.push(token.content)
    return `<div class="chat-markdown__code-block">
      <div class="chat-markdown__block-actions">
        <button type="button" class="chat-markdown__block-action chat-markdown__source-download material-icons" data-markdown-code="${codeIndex}" aria-label="Codeblock herunterladen" title="Codeblock herunterladen">download</button>
        <button type="button" class="chat-markdown__block-action chat-markdown__source-copy material-icons" data-markdown-code="${codeIndex}" aria-label="Codeblock kopieren" title="Codeblock kopieren">content_copy</button>
      </div>
      ${code}
    </div>`
  }

  markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
    const token = tokens[index]
    const filename = token.markup === 'linkify' ? tokens[index + 1]?.content : null
    const fileIndex = env.fileActions.findIndex((action) => action.filename === filename)
    if (fileIndex >= 0) {
      token.attrSet('href', '#')
      token.attrSet('data-markdown-file', String(fileIndex))
      token.attrSet('class', 'chat-markdown__link')
      return self.renderToken(tokens, index, options)
    }
    const href = safeMarkdownLinkUrl(token.attrGet('href'))
    if (href) {
      token.attrSet('href', href)
      const researchSource = env.researchSources.get(canonicalResearchSourceUrl(href))
      token.attrSet(
        'class',
        researchSource
          ? 'chat-markdown__link chat-markdown__source-link'
          : 'chat-markdown__link'
      )
      if (researchSource?.title) token.attrSet('title', researchSource.title)
      if (href.startsWith('http://') || href.startsWith('https://')) {
        token.attrSet('target', '_blank')
        token.attrSet('rel', 'noopener noreferrer')
      }
    } else {
      token.attrSet('class', 'chat-markdown__link chat-markdown__link--invalid')
      const hrefIndex = token.attrIndex('href')
      if (hrefIndex >= 0) token.attrs.splice(hrefIndex, 1)
    }
    return self.renderToken(tokens, index, options)
  }

  markdown.renderer.rules.image = (tokens, index, options, env) => {
    const token = tokens[index]
    if (!env.allowImages) {
      return `<span class="chat-markdown__image-unavailable">[Bild: ${markdown.utils.escapeHtml(token.content || 'Bild')}]</span>`
    }
    const image = classifyMarkdownImageUrl(token.attrGet('src'))
    const alt = token.content || 'Bild'
    const title = token.attrGet('title') || ''
    if (image.kind === 'internal') {
      return imageHtml(image, alt, title)
    }
    if (image.kind !== 'external') {
      return `<span class="chat-markdown__image-unavailable">[Bild: ${markdown.utils.escapeHtml(alt)}]</span>`
    }

    const requestId = String(env.externalImages.length)
    env.externalImages.push({ ...image, alt, title })
    const safeHost = markdown.utils.escapeHtml(image.host)
    const safeAlt = markdown.utils.escapeHtml(alt)
    const state = env.imageStates[image.url]
    const trusted = env.trustedOrigins.has(image.origin)
    if (trusted && state === 'ready') return imageHtml(image, alt, title, requestId)
    if (env.streaming) {
      return `<span class="chat-markdown__image-placeholder">
        <span class="chat-markdown__image-title">${safeAlt}</span>
        <span class="chat-markdown__image-note">Das externe Bild von ${safeHost} kann nach Abschluss der Antwort geprüft werden.</span>
      </span>`
    }
    if (state === 'checking' || (trusted && !state)) {
      return `<span class="chat-markdown__image-placeholder chat-markdown__image-placeholder--checking" role="status" aria-live="polite">
        <span class="chat-markdown__image-title">${safeAlt}</span>
        <span class="chat-markdown__image-note">Bild von ${safeHost} wird geprüft …</span>
      </span>`
    }
    if (state === 'failed') {
      return `<span class="chat-markdown__image-placeholder chat-markdown__image-placeholder--failed">
        <span class="chat-markdown__image-title">${safeAlt}</span>
        <span class="chat-markdown__image-note">Das Bild von ${safeHost} konnte nicht geladen oder decodiert werden.</span>
        <button type="button" class="chat-markdown__image-button" data-markdown-image="${requestId}" aria-label="Bild von ${safeHost} erneut prüfen">Erneut versuchen</button>
      </span>`
    }
    return `<span class="chat-markdown__image-placeholder">
      <span class="chat-markdown__image-title">${safeAlt}</span>
      <span class="chat-markdown__image-note">Externes Bild von ${safeHost} blockiert. Beim Laden wird deine IP-Adresse an diesen Anbieter übertragen.</span>
      <button type="button" class="chat-markdown__image-button" data-markdown-image="${requestId}" aria-label="Bilder von ${safeHost} in dieser Sitzung laden">Bilder von ${safeHost} laden</button>
    </span>`
  }
}

configureRenderRules()

export function renderMarkdown(
  content,
  {
    allowImages = true,
    codeCache = new Map(),
    fileActions = [],
    imageStates = {},
    interactive = true,
    researchSources = [],
    streaming = false,
    trustedImageOrigins = []
  } = {}
) {
  const source = streaming ? streamingMarkdownSource(content) : String(content || '')
  const sourceMap = new Map()
  for (const source of researchSources) {
    const url = canonicalResearchSourceUrl(source?.url)
    if (url) sourceMap.set(url, source)
  }
  const env = {
    codeCache,
    nextCodeCache: new Map(),
    codeBlocks: [],
    externalImages: [],
    fileActions,
    mermaidBlocks: [],
    allowImages,
    imageStates,
    interactive,
    researchSources: sourceMap,
    streaming,
    source,
    trustedOrigins: new Set(trustedImageOrigins)
  }
  const rendered = markdown.render(source, env)
  const html = DOMPurify.sanitize(rendered, {
    ALLOWED_ATTR,
    ALLOWED_TAGS,
    ALLOW_DATA_ATTR: false,
    RETURN_TRUSTED_TYPE: false,
    SANITIZE_NAMED_PROPS: true
  })
  codeCache.clear()
  for (const [key, value] of env.nextCodeCache) codeCache.set(key, value)
  return {
    codeBlocks: env.codeBlocks,
    html,
    externalImages: env.externalImages,
    mermaidBlocks: env.mermaidBlocks
  }
}
