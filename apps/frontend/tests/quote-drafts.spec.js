import { expect, it } from 'vitest'
import { editComposerDraft, parseQuotedMessage, quotedMessage, readComposerDraft } from '../src/features/chat/quote-drafts'

it('round-trips text and quotes in their original order', () => {
  const blocks = [
    { type: 'text', text: 'Before the quote' },
    { type: 'quote', text: 'First paragraph\n\n> [!COMMENT]\n```js\nconst value = 1\n```', comment: 'Why?\n\nPlease explain.' },
    { type: 'text', text: 'Between the quotes' },
    { type: 'quote', text: 'Another answer', comment: '' },
    { type: 'text', text: '\nMy own message\n> An ordinary Markdown quote' }
  ]
  expect(parseQuotedMessage(quotedMessage(blocks))).toEqual(blocks)
})

it.each(['Normal text\n\nSecond paragraph', '> A normal quote', '> [!QUOTE]\n\nNot a quote card', '```markdown\n> [!QUOTE]\n> An example, not a quote card\n```'])('leaves ordinary messages unchanged: %s', text => {
  expect(parseQuotedMessage(text)).toEqual([{ type: 'text', text }])
})

it('still reads quotes saved at the beginning of earlier drafts', () => {
  expect(parseQuotedMessage('> [!QUOTE]\n> Saved quote\n\n> [!COMMENT]\n> Saved comment\n\nMy message')).toEqual([
    { type: 'text', text: '' }, { type: 'quote', text: 'Saved quote', comment: 'Saved comment' }, { type: 'text', text: 'My message' }
  ])
})

it('closes an unfinished code block before appending a quote', () => {
  const quote = { type: 'quote', text: 'Selected answer', comment: 'Explain this' }
  expect(parseQuotedMessage(quotedMessage([{ type: 'text', text: '```js\nconst n = 1' }, quote]))).toEqual([
    { type: 'text', text: '```js\nconst n = 1\n```' }, quote, { type: 'text', text: '' }
  ])
})

it('keeps existing quote cards in place when editing the text of a quoted draft', () => {
  const draft = readComposerDraft('Before\n\n> [!QUOTE]\n> Saved quote\n\n> [!COMMENT]\n> Saved comment')
  expect(editComposerDraft(draft, 'After').quotes).toBe(draft.quotes)
})
