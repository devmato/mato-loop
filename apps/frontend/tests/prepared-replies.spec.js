// @vitest-environment jsdom
import { createApp, h, nextTick, ref } from 'vue'
import { Quasar, QCard, QDialog } from 'quasar'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('quasar', () => import('quasar/dist/quasar.client.js'))

import MessageContent from '../src/features/chat/components/MessageContent.vue'

let app, root

afterEach(() => {
  app?.unmount()
  root?.remove()
})

describe('prepared Markdown replies', () => {
  it('notifies scroll consumers after the streamed Markdown reaches the DOM', async () => {
    const content = ref('First paragraph')
    const rendered = vi.fn(() => root.querySelector('p:last-child')?.textContent)
    root = document.createElement('div')
    app = createApp({ render: () => h(MessageContent, { content: content.value, streaming: true, onRendered: rendered }) })
    app.use(Quasar, { components: { QCard, QDialog } })
    app.mount(root)
    content.value += '\n\nSecond paragraph'
    await vi.waitFor(() => expect(rendered).toHaveLastReturnedWith('Second paragraph'))
  })

  it('ignores equivalent file actions but renders changed filenames', async () => {
    const fileActions = ref([{ filename: 'Support-Meeting-Protokoll.md' }])
    const rendered = vi.fn()
    root = document.createElement('div')
    app = createApp({
      render: () => h(MessageContent, {
        content: '**Support-Meeting-Protokoll.md**', fileActions: fileActions.value, onRendered: rendered
      })
    })
    app.use(Quasar, { components: { QCard, QDialog } })
    app.mount(root)
    await nextTick()
    await nextTick()
    rendered.mockClear()
    fileActions.value = fileActions.value.map((action) => ({ ...action }))
    await nextTick()
    await nextTick()
    const unchangedRenders = rendered.mock.calls.length
    fileActions.value = [{ filename: 'Other.md' }]
    await nextTick()
    expect([unchangedRenders, root.querySelector('[data-markdown-file]')]).toEqual([0, null])
  })

  it('emits only a matching host-provided reply', () => {
    const reply = vi.fn()
    root = document.createElement('div')
    document.body.append(root)
    app = createApp({
      render: () => h(MessageContent, {
        content: '[Kunde 123](#reply-customer-123)',
        preparedReplies: [{ id: 'customer-123', content: 'Verwende ERP-Kunde 123.' }],
        onPreparedReply: reply
      })
    })
    app.use(Quasar, { components: { QCard, QDialog } })
    app.mount(root)

    root.querySelector('a').click()

    expect(reply).toHaveBeenCalledOnce()
    expect(reply).toHaveBeenCalledWith('Verwende ERP-Kunde 123.')
  })
})
