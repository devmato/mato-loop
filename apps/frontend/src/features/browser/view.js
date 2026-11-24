// The private relay carries length-prefixed JSON, not a public CDP connection.
// Opening a relay can wait for a browser tool holding the action lock.
const STREAM_OPEN_TIMEOUT = 200000
const FRAME_TIMEOUT = 8000

export class BrowserView extends EventTarget {
  constructor(host, url) {
    super()
    this.readOnly = true
    this._fit = true
    this.state = null
    this.pending = null
    this.drawing = false
    this.closed = false
    this.buffer = new Uint8Array()
    this.canvas = document.createElement('canvas')
    this.canvas.className = 'fit'
    this.canvas.style.objectFit = 'contain'
    this.canvas.style.touchAction = 'none'
    this.canvas.setAttribute('aria-hidden', 'true')
    this.input = document.createElement('textarea')
    this.input.className = 'browser-view-input absolute-top-left no-pointer-events'
    this.input.setAttribute('aria-label', 'Entfernter Browser. Mit Strg+Alt+Escape zur lokalen Steuerung.')
    this.input.autocapitalize = 'off'
    this.input.autocomplete = 'off'
    this.input.spellcheck = false
    host.append(this.canvas, this.input)
    this.socket = new WebSocket(url)
    this.socket.binaryType = 'arraybuffer'
    this.socket.onmessage = ({ data }) => this.receive(data)
    this.socket.onclose = () => { if (!this.closed) this.dispatchEvent(new Event('disconnect')) }
    this.socket.onerror = () => { if (!this.closed) this.dispatchEvent(new Event('disconnect')) }
    for (const type of ['pointerdown', 'pointerup', 'pointermove', 'pointerleave', 'pointercancel', 'wheel']) {
      this.canvas.addEventListener(type, (event) => this.pointer(event), { passive: false })
    }
    this.canvas.addEventListener('contextmenu', (event) => event.preventDefault())
    for (const type of ['keydown', 'keyup']) this.input.addEventListener(type, (event) => this.key(event))
    this.input.addEventListener('compositionstart', () => { this.composing = true })
    this.input.addEventListener('compositionend', (event) => {
      this.composing = false
      this.send({ kind: 'text', text: event.data })
      this.input.value = ''
    })
    this.input.addEventListener('input', (event) => {
      if (this.composing) return
      if (event.inputType === 'deleteContentBackward') {
        for (const phase of ['down', 'up']) this.send({ kind: 'key', event: phase, key: 'Backspace', code: 'Backspace', keyCode: 8 })
      } else if (this.input.value) this.send({ kind: 'text', text: this.input.value })
      this.input.value = ''
    })
    this.input.addEventListener('blur', () => this.releaseInput())
  }

  receive(data) {
    if (this.closed) return
    if (!(data instanceof ArrayBuffer)) return this.disconnect(true)
    const bytes = new Uint8Array(data)
    if (bytes.length + this.buffer.length > 4 * 1024 * 1024 + 65540) return this.disconnect(true)
    const buffer = new Uint8Array(this.buffer.length + bytes.length)
    buffer.set(this.buffer)
    buffer.set(bytes, this.buffer.length)
    this.buffer = buffer
    try {
      while (this.buffer.length >= 4) {
        const size = new DataView(this.buffer.buffer, this.buffer.byteOffset, 4).getUint32(0)
        if (!size || size > 4 * 1024 * 1024) throw new Error('Invalid browser frame size')
        if (this.buffer.length < size + 4) break
        const message = JSON.parse(new TextDecoder().decode(this.buffer.subarray(4, size + 4)))
        this.buffer = this.buffer.slice(size + 4)
        if (!['frame', 'state'].includes(message.type) || typeof message.inputEpoch !== 'string'
          || typeof message.manual !== 'boolean' || !(message.width >= 320 && message.width <= 3840)
          || !(message.height >= 240 && message.height <= 2160)) throw new Error('Invalid browser frame')
        if (!this.state) {
          clearTimeout(this.frameTimer)
          this.frameTimer = null
        }
        if (this.state?.inputEpoch !== message.inputEpoch || this.state?.tabId !== message.tabId) this.releaseInput()
        this.state = message
        this.watchFrame()
        this.updateCursor()
        this.dispatchEvent(new Event('state'))
        if (!this.connected) { this.connected = true; this.dispatchEvent(new Event('connect')) }
        if (!message.tabId) {
          // An empty browser has no image to wait for, but its tab controls must work.
          if (!this.hasCurrentFrame) {
            this.pending = null
            this.canvas.width = 0
            this.drawn = message
            this.dispatchEvent(new Event('ready'))
          }
        } else if (message.type === 'frame') {
          if (typeof message.data !== 'string' || !/^[A-Za-z0-9+/]*={0,2}$/u.test(message.data)) throw new Error('Invalid browser image')
          this.pending = message
          this.draw()
        }
      }
    } catch {
      this.disconnect(true)
    }
  }

  draw() {
    if (this.drawing || !this.pending || this.closed) return
    const frame = this.pending
    this.pending = null
    this.drawing = true
    const image = new Image()
    image.onload = () => {
      if (!this.closed && this.state.inputEpoch === frame.inputEpoch && this.state.tabId === frame.tabId) {
        this.canvas.width = image.naturalWidth
        this.canvas.height = image.naturalHeight
        this.canvas.getContext('2d').drawImage(image, 0, 0)
        const wasCurrent = this.hasCurrentFrame
        this.drawn = frame
        this.watchFrame()
        if (!wasCurrent) this.dispatchEvent(new Event('ready'))
      }
      this.drawing = false
      this.draw()
    }
    image.onerror = () => { this.drawing = false; this.disconnect(true) }
    image.src = `data:image/jpeg;base64,${frame.data}`
  }

  watchFrame(visible = this.visible) {
    this.visible = visible
    // Static pages need no new image. A website dialog can pause the screencast.
    const waiting = visible && !this.closed && !this.state?.dialog
      && (!this.state || (this.state.tabId && !this.hasCurrentFrame))
    if (!waiting) {
      clearTimeout(this.frameTimer)
      this.frameTimer = null
    } else if (this.frameTimer == null) {
      this.frameTimer = setTimeout(() => {
        this.frameTimer = null
        if (!document.hidden) this.disconnect(true)
      }, this.state ? FRAME_TIMEOUT : STREAM_OPEN_TIMEOUT)
    }
  }

  get hasCurrentFrame() {
    return this.drawn && this.drawn.tabId === this.state?.tabId
      && this.drawn.inputEpoch === this.state?.inputEpoch
  }

  send(event) {
    if (this.closed || !this.state?.manual || !this.state.tabId || this.socket.readyState !== 1) return
    if (event.kind === 'dialog' ? (!this.state.dialog || (this.readOnly && !this.dialogEnabled))
      : this.readOnly || !this.hasCurrentFrame) return
    const payload = new TextEncoder().encode(JSON.stringify({ tabId: this.state.tabId, inputEpoch: this.state.inputEpoch, ...event }))
    if (payload.length > 32768 || this.socket.bufferedAmount > 65536) return
    const packet = new Uint8Array(payload.length + 4)
    new DataView(packet.buffer).setUint32(0, payload.length)
    packet.set(payload, 4)
    this.socket.send(packet)
  }

  releaseInput() { this.send({ kind: 'release' }) }
  updateCursor() {
    const cursor = this.state?.cursor
    // Only CSS keywords may cross the relay, never website-supplied cursor URLs.
    this.canvas.style.cursor = this.hovering && !this.readOnly && this.state?.manual
      && /^(auto|default|none|context-menu|help|pointer|progress|wait|cell|crosshair|text|vertical-text|alias|copy|move|no-drop|not-allowed|grab|grabbing|all-scroll|col-resize|row-resize|(n|s|e|w|ne|nw|se|sw|ew|ns|nesw|nwse)-resize|zoom-in|zoom-out)$/u.test(cursor)
      ? cursor : 'default'
  }
  get fit() { return this._fit }
  set fit(value) { this._fit = value; this.canvas.style.objectFit = value ? 'contain' : 'none' }
  focus() { if (!this.readOnly) this.input.focus({ preventScroll: true }) }
  blur() { this.releaseInput(); this.input.blur() }
  disconnect(failed = false) {
    this.releaseInput()
    this.closed = true
    this.watchFrame(false)
    this.socket.close()
    if (failed) this.dispatchEvent(new Event('disconnect'))
  }

  key(event) {
    if (this.readOnly || this.composing || event.isComposing || event.key === 'Process'
      || event.key === 'Unidentified' || event.keyCode === 229 || (!event.code && event.key.length !== 1)) return
    const altGraph = event.getModifierState('AltGraph')
    if (!altGraph && (event.ctrlKey || event.metaKey) && ['c', 'v'].includes(event.key.toLowerCase())) return
    event.preventDefault()
    const meta = event.key === 'Meta'
    if (!event.code || (altGraph && event.key.length === 1)) {
      if (event.type === 'keydown') this.send({ kind: 'text', text: event.key })
    } else this.send({ kind: 'key', event: event.type === 'keydown' ? 'down' : 'up',
      key: meta ? 'Control' : event.key, code: meta ? event.code.replace('Meta', 'Control') : event.code,
      keyCode: meta ? 17 : event.keyCode,
      text: event.type === 'keydown' && event.key.length === 1
        && !event.ctrlKey && !event.metaKey && !event.altKey ? event.key : '' })
  }

  pointer(event) {
    if (this.readOnly || !this.drawn) return
    event.preventDefault()
    if (event.type === 'pointercancel') { this.touch = null; this.releaseInput(); this.leave(); return }
    if (event.type === 'pointerleave') { if (!event.buttons) this.leave(); return }
    const rect = this.canvas.getBoundingClientRect()
    const scale = this.fit ? Math.min(rect.width / this.canvas.width, rect.height / this.canvas.height) : 1
    const x = (event.clientX - rect.left - (rect.width - this.canvas.width * scale) / 2) / scale
    const y = (event.clientY - rect.top - (rect.height - this.canvas.height * scale) / 2) / scale
    if (x < 0 || y < 0 || x > this.drawn.width || y > this.drawn.height) {
      if (event.type === 'pointerup') { this.touch = null; this.releaseInput() }
      if (!event.buttons) this.leave()
      return
    }
    if (!this.hovering) { this.hovering = true; this.updateCursor() }
    if (event.type === 'pointerdown') { this.focus(); this.canvas.setPointerCapture(event.pointerId) }
    const modifiers = (event.altKey ? 1 : 0) | (event.ctrlKey || event.metaKey ? 2 : 0) | (event.shiftKey ? 8 : 0)
    if (event.pointerType === 'touch') {
      if (event.type === 'pointerdown') this.touch = { x, y, moved: false }
      else if (this.touch && event.type === 'pointermove') {
        const deltaX = this.touch.x - x, deltaY = this.touch.y - y
        if (Math.abs(deltaX) + Math.abs(deltaY) > 4 || this.touch.moved) {
          this.send({ kind: 'mouse', event: 'wheel', x, y, deltaX, deltaY, modifiers })
          this.touch = { x, y, moved: true }
        }
      } else if (this.touch && event.type === 'pointerup') {
        if (!this.touch.moved) {
          this.send({ kind: 'mouse', event: 'down', x, y, modifiers })
          this.send({ kind: 'mouse', event: 'up', x, y, modifiers })
        }
        this.touch = null
      }
      return
    }
    const buttons = ['left', 'middle', 'right']
    if (event.type === 'pointerdown') {
      const previous = this.lastClick
      this.lastClick = { x, y, time: event.timeStamp, count: previous && event.timeStamp - previous.time < 500
        && Math.abs(previous.x - x) < 5 && Math.abs(previous.y - y) < 5 ? previous.count % 3 + 1 : 1 }
    }
    const action = { kind: 'mouse', x, y, modifiers, button: buttons[event.button] || 'left', clickCount: this.lastClick?.count || 1 }
    if (event.type === 'wheel') {
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? this.drawn.height : 1
      this.send({ ...action, event: 'wheel', deltaX: Math.max(-10000, Math.min(10000, event.deltaX * unit)),
        deltaY: Math.max(-10000, Math.min(10000, event.deltaY * unit)) })
    } else this.send({ ...action, event: { pointerdown: 'down', pointerup: 'up', pointermove: 'move' }[event.type] })
  }

  leave() {
    if (!this.hovering) return
    this.hovering = false
    this.canvas.style.cursor = 'default'
    this.send({ kind: 'mouse', event: 'move', x: this.drawn.width, y: this.drawn.height })
  }
}
