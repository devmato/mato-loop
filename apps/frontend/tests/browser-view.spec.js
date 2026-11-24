// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { BrowserView } from '../src/features/browser/view'
import { FakeWebSocket } from './terminal-websocket'

let view, images
const frame = { type: 'frame', data: '/9j/2Q==', tabId: 'tab-1', inputEpoch: 'epoch-1', manual: true, width: 1000, height: 700 }
function packet(value) {
  const payload = new TextEncoder().encode(JSON.stringify(value))
  const bytes = new Uint8Array(payload.length + 4)
  new DataView(bytes.buffer).setUint32(0, payload.length)
  bytes.set(payload, 4)
  return bytes
}
beforeEach(() => {
  images = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.stubGlobal('Image', class {
    naturalWidth = 1000
    naturalHeight = 700
    constructor() { images.push(this) }
  })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() })
  view = new BrowserView(document.createElement('div'), 'ws://localhost/view')
  view.socket.open()
  view.readOnly = false
})
afterEach(() => { view.disconnect(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('retries a missing expected image, not an idle static page', () => {
  vi.useFakeTimers()
  const disconnected = vi.fn()
  view.addEventListener('disconnect', disconnected)
  view.watchFrame(true)
  view.receive(packet(frame).buffer)
  images[0].onload()
  vi.advanceTimersByTime(30000)
  expect(disconnected).not.toHaveBeenCalled()
  view.receive(packet({ ...frame, type: 'state', inputEpoch: 'epoch-2' }).buffer)
  vi.advanceTimersByTime(8000)
  expect(disconnected).toHaveBeenCalledOnce()
})

it('allows a long relay setup before starting the frame deadline', () => {
  vi.useFakeTimers()
  const disconnected = vi.fn()
  view.addEventListener('disconnect', disconnected)
  view.watchFrame(true)
  vi.advanceTimersByTime(160000)
  expect(disconnected).not.toHaveBeenCalled()
  view.receive(packet({ ...frame, type: 'state' }).buffer)
  vi.advanceTimersByTime(7999)
  expect(disconnected).not.toHaveBeenCalled()
  vi.advanceTimersByTime(1)
  expect(disconnected).toHaveBeenCalledOnce()
})

it('makes the tab controls available when the browser has no page to draw', () => {
  const ready = vi.fn()
  view.addEventListener('ready', ready)
  view.receive(packet({ ...frame, type: 'state', tabId: null }).buffer)
  expect(ready).toHaveBeenCalledOnce()
})

it('reassembles split frames and gates input on the image actually displayed', () => {
  const ready = vi.fn()
  view.addEventListener('ready', ready)
  const bytes = packet(frame)
  view.receive(bytes.slice(0, 7).buffer)
  view.receive(bytes.slice(7).buffer)
  view.send({ kind: 'text', text: 'hello' })
  expect(view.socket.send).not.toHaveBeenCalled()
  const beforeImage = ready.mock.calls.length
  images[0].onload()
  expect([beforeImage, ready.mock.calls.length]).toEqual([0, 1])
  view.send({ kind: 'text', text: 'Grüße 🌍' })
  const sent = view.socket.send.mock.calls.at(-1)[0]
  expect(JSON.parse(new TextDecoder().decode(sent.subarray(4)))).toMatchObject({ kind: 'text', inputEpoch: 'epoch-1', tabId: 'tab-1', text: 'Grüße 🌍' })
  view.receive(packet({ ...frame, inputEpoch: 'epoch-2' }).buffer)
  const count = view.socket.send.mock.calls.length
  view.send({ kind: 'text', text: 'stale' })
  expect(view.socket.send).toHaveBeenCalledTimes(count)
})

it('does not draw an image which finishes after a tab change or disconnect', () => {
  view.receive(packet(frame).buffer)
  view.receive(packet({ ...frame, type: 'state', tabId: 'tab-2', inputEpoch: 'epoch-2' }).buffer)
  images[0].onload()
  expect(view.canvas.getContext('2d').drawImage).not.toHaveBeenCalled()
})

it('connects and answers a website dialog before the first image', () => {
  view.addEventListener('connect', () => view.send({ kind: 'dialog', accept: true }))
  view.receive(packet({ ...frame, type: 'state', dialog: { type: 'alert', message: 'Hello' } }).buffer)
  expect(view.socket.send.mock.calls.map(([bytes]) => JSON.parse(new TextDecoder().decode(bytes.subarray(4)))))
    .toMatchObject([{ kind: 'dialog', accept: true }])
})

it.each([{ manual: false }, { readOnly: true }])('blocks remote input while locked (%j)', (lock) => {
  view.receive(packet({ ...frame, ...lock }).buffer)
  images[0].onload()
  if (lock.readOnly) view.readOnly = true
  view.send({ kind: 'text', text: 'blocked' })
  expect(view.socket.send).not.toHaveBeenCalled()
})

it('rejects oversized frames before allocating their payload', () => {
  const bytes = new Uint8Array(4)
  new DataView(bytes.buffer).setUint32(0, 10 * 1024 * 1024)
  view.receive(bytes.buffer)
  expect(view.closed).toBe(true)
})

it('forwards virtual-keyboard deletion without requiring a physical key code', () => {
  view.receive(packet(frame).buffer)
  images[0].onload()
  view.input.dispatchEvent(new InputEvent('input', { inputType: 'deleteContentBackward' }))
  expect(view.socket.send.mock.calls.map(([bytes]) => JSON.parse(new TextDecoder().decode(bytes.subarray(4))).event)).toEqual(['down', 'up'])
})

it('forwards physical Space as keyboard events with text on keydown', () => {
  view.receive(packet(frame).buffer)
  images[0].onload()
  for (const type of ['keydown', 'keyup']) {
    view.input.dispatchEvent(new KeyboardEvent(type, { key: ' ', code: 'Space', keyCode: 32 }))
  }
  expect(view.socket.send.mock.calls.map(([bytes]) => JSON.parse(new TextDecoder().decode(bytes.subarray(4)))))
    .toMatchObject([{ kind: 'key', event: 'down', key: ' ', text: ' ' }, { kind: 'key', event: 'up', key: ' ', text: '' }])
})

it('inserts Windows AltGr characters as text despite the synthetic Control modifier', () => {
  view.receive(packet(frame).buffer)
  images[0].onload()
  for (const type of ['keydown', 'keyup']) {
    view.input.dispatchEvent(new KeyboardEvent(type, { key: '@', code: 'KeyQ', keyCode: 81,
      ctrlKey: true, altKey: true, modifierAltGraph: true }))
  }
  expect(view.socket.send.mock.calls.map(([bytes]) => JSON.parse(new TextDecoder().decode(bytes.subarray(4)))))
    .toMatchObject([{ kind: 'text', text: '@' }])
})

it('forwards click modifiers held before the viewer receives keyboard focus', () => {
  view.receive(packet(frame).buffer)
  images[0].onload()
  vi.spyOn(view.canvas, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 1000, height: 700 })
  view.canvas.setPointerCapture = vi.fn()
  for (const type of ['pointerdown', 'pointerup']) {
    view.canvas.dispatchEvent(new MouseEvent(type, { clientX: 10, clientY: 10, ctrlKey: true, shiftKey: true }))
  }
  expect(view.socket.send.mock.calls.map(([bytes]) => JSON.parse(new TextDecoder().decode(bytes.subarray(4)))))
    .toMatchObject([{ kind: 'mouse', event: 'down', modifiers: 10 }, { kind: 'mouse', event: 'up', modifiers: 10 }])
})

it.each(['pointer', 'text', 'nwse-resize', 'url(https://example.test/cursor), pointer'])('accepts only standard cursor keywords (%s)', (cursor) => {
  view.hovering = true
  view.receive(packet({ ...frame, cursor }).buffer)
  expect(view.canvas.style.cursor).toBe(cursor.startsWith('url') ? 'default' : cursor)
})

it('clears hover and forwards leaving the viewer', () => {
  view.receive(packet(frame).buffer)
  images[0].onload()
  view.hovering = true
  view.canvas.dispatchEvent(new Event('pointerleave'))
  const sent = view.socket.send.mock.calls.at(-1)[0]
  expect(JSON.parse(new TextDecoder().decode(sent.subarray(4)))).toMatchObject({ kind: 'mouse', event: 'move', x: frame.width, y: frame.height })
})

it('restores the cursor on reentry without waiting for a changed server state', () => {
  view.hovering = true
  view.receive(packet({ ...frame, cursor: 'pointer' }).buffer)
  images[0].onload()
  vi.spyOn(view.canvas, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 1000, height: 700 })
  view.leave()
  view.canvas.dispatchEvent(new MouseEvent('pointermove', { clientX: 10, clientY: 10, cancelable: true }))
  expect(view.canvas.style.cursor).toBe('pointer')
})
