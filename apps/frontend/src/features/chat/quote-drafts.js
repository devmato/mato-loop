const QUOTE = '> [!QUOTE]'
const COMMENT = '> [!COMMENT]'

function codeFence(fence, line) {
  const delimiter = /^ {0,3}(`{3,}|~{3,})(.*)$/u.exec(line)
  if (!delimiter) return fence
  if (!fence) return delimiter[1]
  return delimiter[1][0] === fence[0] && delimiter[1].length >= fence.length && !delimiter[2].trim() ? null : fence
}

function closeCodeFence(text) {
  if (!text.includes('```') && !text.includes('~~~')) return text
  const fence = text.split('\n').reduce(codeFence, null)
  return text + (fence ? `\n${fence}` : '')
}

// Readable Markdown keeps quotes intact across existing draft, queue and undo flows.
export function quotedMessage(blocks) {
  const block = (marker, value) => [marker, ...value.split('\n').map(line => `> ${line}`)].join('\n')
  return blocks.filter(item => item.type === 'quote' || item.text).map((item, index, items) => {
    if (item.type === 'text') {
      return items[index + 1]?.type === 'quote' ? closeCodeFence(item.text) : item.text
    }
    return [block(QUOTE, item.text), ...(item.comment ? [block(COMMENT, item.comment)] : [])].join('\n\n')
  }).join('\n\n')
}

export function parseQuotedMessage(content = '') {
  if (!content.includes(QUOTE)) return [{ type: 'text', text: content }]
  const lines = content.split('\n')
  const blocks = []
  let cursor = 0
  let start = 0
  let fence = null
  function readBlock() {
    const start = ++cursor
    while (lines[cursor] === '>' || lines[cursor]?.startsWith('> ')) cursor += 1
    const value = lines.slice(start, cursor).map(line => line.slice(2)).join('\n')
    if (lines[cursor] === '') cursor += 1
    return value
  }
  while (cursor < lines.length) {
    const line = lines[cursor]
    fence = codeFence(fence, line)
    if (!fence && line === QUOTE) {
      const quoteStart = cursor
      const text = readBlock()
      if (text.trim()) {
        const preceding = lines.slice(start, quoteStart).join('\n')
        blocks.push({ type: 'text', text: preceding.endsWith('\n') ? preceding.slice(0, -1) : preceding })
        const comment = lines[cursor] === COMMENT ? readBlock() : ''
        blocks.push({ type: 'quote', text, comment })
        start = cursor
        continue
      }
      cursor = quoteStart
    }
    cursor += 1
  }
  blocks.push({ type: 'text', text: lines.slice(start).join('\n') })
  return blocks
}

export function readComposerDraft(content) {
  const blocks = parseQuotedMessage(content)
  const quotes = blocks.filter(block => block.type === 'quote')
  return {
    text: blocks.filter(block => block.type === 'text' && block.text).map(block => block.text).join('\n\n'),
    quotes,
    quoteMarkdown: quotedMessage(quotes)
  }
}

function appendQuoteMarkdown(text, quoteMarkdown) {
  return text && quoteMarkdown ? `${text}\n\n${quoteMarkdown}` : text || quoteMarkdown
}

export function composerDraftContent(text, quoteMarkdown) {
  return appendQuoteMarkdown(quoteMarkdown ? closeCodeFence(text) : text, quoteMarkdown)
}

export function editComposerDraft(draft, text, quotes = draft.quotes) {
  const quoteMarkdown = quotes === draft.quotes ? draft.quoteMarkdown : quotedMessage(quotes)
  const closedText = quoteMarkdown ? closeCodeFence(text) : text
  const content = appendQuoteMarkdown(closedText, quoteMarkdown)
  // Only the editable text can introduce literal Markdown quotes. Never parse the cached suffix back.
  const pasted = closedText.includes(QUOTE) ? readComposerDraft(closedText) : null
  return pasted?.quotes.length ? {
    content,
    text: pasted.text,
    quotes: [...pasted.quotes, ...quotes],
    quoteMarkdown: appendQuoteMarkdown(pasted.quoteMarkdown, quoteMarkdown)
  } : { content, text: closedText, quotes, quoteMarkdown }
}
