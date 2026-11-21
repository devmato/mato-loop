import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useSessionTabsStore } from './session-tabs-store'
import {
  chatBottomDistance,
  chatReadingAnchor,
  chatShouldFollowBottom,
  restoreChatReadingAnchor,
  scrollChatToBottom
} from './scroll'

// Keep interactive prepends smaller than the rendered slice so the current
// message stays mounted while Quasar updates its range.
const SCROLL_HISTORY_PAGE_SIZE = 8
const STORED_MESSAGE_ID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i

export function useChatViewport({ chat, route, scrollArea, scrollContent, virtualScroll, displayMessages, setWorkHistoryExpanded }) {
  const tabs = useSessionTabsStore()
  const routeSessionId = computed(() => typeof route.query.session === 'string' ? route.query.session : null)
  const routeMessageId = computed(() => typeof route.query.message === 'string' ? route.query.message : null)
  const followingBottom = ref(true)
  const transcriptPreparing = ref(Boolean(chat.current))
  const positionLoadFailed = ref(false)
  const highlightedMessageId = ref(null)
  let target, observer, operation, scheduledFrame, userScrollTimer, highlightTimer
  let disposed = false
  let pending = false
  let resetSizes = false
  let revealMessageId = null
  let readingAnchor = null
  let userScrolling = false
  let touching = false
  let lastScrollTop = 0
  let stalledHistoryCursor = null
  let historyRequest = null
  let positionSessionId = chat.current?.id || null

  function savePosition() {
    if (tabs.items.some(tab => tab.key === positionSessionId) && !transcriptPreparing.value) {
      const anchor = !followingBottom.value && (operation ? readingAnchor : captureAnchor() || readingAnchor)
      if (anchor) tabs.readingPositions[positionSessionId] = anchor
      else delete tabs.readingPositions[positionSessionId]
    }
  }
  watch(routeSessionId, savePosition, { flush: 'sync' })

  const elements = () => [...(scrollContent.value?.querySelectorAll('[data-message-id]') || [])]
  const findElement = id => elements().find(element => element.dataset.messageId === id || element.dataset.renderId === id)
  const captureAnchor = () => chatReadingAnchor(target, elements())
  const rowId = message => message.renderId || message.id
  function findAnchorIndex(anchor) {
    if (!anchor) return -1
    const index = displayMessages.value.findIndex(message => rowId(message) === anchor.id)
    return index >= 0 ? index : displayMessages.value.findIndex(message => message.id === anchor.messageId)
  }
  const isCurrent = request => !disposed && operation === request && !chat.loading
    && virtualScroll.value === request.view && chat.current?.id === request.sessionId
    && routeSessionId.value === request.routeId

  function beginLayout(view, prepending = false) {
    return (operation = { view, sessionId: chat.current?.id, routeId: routeSessionId.value, prepending })
  }

  function cancelLayout() {
    clearTimeout(highlightTimer)
    highlightedMessageId.value = null
    if (scheduledFrame != null) window.cancelAnimationFrame(scheduledFrame)
    scheduledFrame = null
    if (operation?.frame != null) window.cancelAnimationFrame(operation.frame)
    operation?.resume?.()
    operation = null
  }

  async function rendered(request) {
    await nextTick()
    if (!isCurrent(request)) return false
    await new Promise(resolve => {
      request.resume = resolve
      request.frame = window.requestAnimationFrame(() => {
        request.frame = null
        request.resume = null
        resolve()
      })
    })
    await nextTick()
    return isCurrent(request)
  }

  function scheduleTranscriptLayout(reset = false) {
    if (disposed) return
    pending = true
    resetSizes ||= reset
    if (userScrolling || operation || scheduledFrame != null) return
    scheduledFrame = window.requestAnimationFrame(() => {
      scheduledFrame = null
      void layoutTranscript()
    })
  }

  async function loadUntil(findIndex, request) {
    let index = findIndex()
    while (index < 0 && isCurrent(request) && chat.current?.nextMessageCursor) {
      const cursor = chat.current.nextMessageCursor
      await chat.loadOlderMessages(request.sessionId)
      if (!isCurrent(request)) break
      index = findIndex()
      if (chat.current.nextMessageCursor === cursor) break
    }
    return index
  }

  async function loadReadingAnchor(anchor, request) {
    const index = findAnchorIndex(anchor)
    if (index >= 0 || !anchor || !chat.current?.nextMessageCursor) return index
    // Local transcript rows (e.g. context summaries) have no message endpoint.
    if (STORED_MESSAGE_ID.test(anchor.messageId) && !await chat.hasStoredMessage(request.sessionId, anchor.messageId)) return -1
    if (!isCurrent(request)) return -1
    return loadUntil(() => findAnchorIndex(anchor), request)
  }

  function retryPositionLoad() {
    positionLoadFailed.value = false
    scheduleTranscriptLayout()
  }

  async function position(request, index, edge, anchor, force = false) {
    const id = index >= 0 ? rowId(displayMessages.value[index]) : null
    if (id && (force || !findElement(id))) {
      request.view.scrollTo(index, `${edge}-force`)
      if (request.prepending) {
        // Compensate before paint, then correct against Quasar's measured slice.
        await nextTick()
        if (!isCurrent(request)) return false
        restoreChatReadingAnchor(target, findElement(id), anchor)
        lastScrollTop = target?.scrollTop || 0
      }
      if (!await rendered(request)) return false
    }
    const element = id ? findElement(id) : null
    if (id && !element) return false
    if (edge === 'end') scrollChatToBottom(target)
    else if (edge === 'center' && element && target) {
      restoreChatReadingAnchor(target, element, {
        offsetTop: Math.max(0, (target.clientHeight - element.getBoundingClientRect().height) / 2)
      })
    } else restoreChatReadingAnchor(target, element, anchor)
    lastScrollTop = target?.scrollTop || 0
    return element || true
  }

  // Virtual-scroll events are notifications, not completion promises: reset may emit none.
  async function layoutTranscript() {
    if (disposed || userScrolling || operation || positionLoadFailed.value || chat.loading || chat.olderMessagesLoading || !virtualScroll.value
      || routeSessionId.value && chat.current?.id !== routeSessionId.value) return
    const request = beginLayout(virtualScroll.value)
    pending = false
    const messageId = revealMessageId
    const opening = transcriptPreparing.value
    let anchor = followingBottom.value ? null : readingAnchor || (!opening ? captureAnchor() : null)
    try {
      const matches = message => message.id === messageId || message.workHistory?.entries?.some(entry => (
        entry.message?.id === messageId || entry.batchId === messageId
      ))
      let searchIndex = -1
      try {
        if (messageId) searchIndex = await loadUntil(() => displayMessages.value.findIndex(matches), request)
        if (!isCurrent(request)) return
        if (searchIndex < 0 && opening && !followingBottom.value) {
          const index = await loadReadingAnchor(anchor, request)
          if (!isCurrent(request)) return
          if (index < 0) {
            // Do not reveal the incoming transcript at an inherited offset.
            anchor = readingAnchor = null
            delete tabs.readingPositions[request.sessionId]
            followingBottom.value = true
          }
        }
      } catch {
        if (isCurrent(request)) positionLoadFailed.value = true
        return
      }
      const searchedMessage = searchIndex >= 0 ? displayMessages.value[searchIndex] : null
      if (searchedMessage) {
        followingBottom.value = false
        anchor = readingAnchor = null
        highlightedMessageId.value = searchedMessage.id
        if (searchedMessage.id !== messageId) {
          setWorkHistoryExpanded(searchedMessage.workHistory.id || searchedMessage.id, true)
        }
      }
      await nextTick()
      if (!isCurrent(request)) return
      request.refreshing = true
      if (resetSizes) request.view.reset?.()
      else request.view.refresh?.(-1)
      request.refreshing = false
      resetSizes = false
      if (!await rendered(request)) return
      const messages = displayMessages.value
      const index = searchedMessage ? messages.findIndex(matches) : followingBottom.value ? messages.length - 1 : findAnchorIndex(anchor)
      const edge = searchedMessage ? 'center' : followingBottom.value ? 'end' : 'start'
      const element = await position(request, index, edge, anchor, Boolean(searchedMessage || opening && anchor))
      if (!isCurrent(request) || !element) return
      transcriptPreparing.value = false
      // Focus only after Vue has removed inert, never while initial positioning is hidden.
      await nextTick()
      if (!isCurrent(request)) return
      if (searchedMessage) {
        element.focus?.({ preventScroll: true })
        clearTimeout(highlightTimer)
        highlightTimer = window.setTimeout(() => { highlightedMessageId.value = null }, 2500)
      }
      if (revealMessageId === messageId) revealMessageId = null
      if (!followingBottom.value) readingAnchor = captureAnchor() || anchor
      // Fetch one page at a time until collapsed history fills the viewport,
      // or prepended messages move the reading anchor away from the top edge.
      if (!messageId) maybeLoadOlderHistory()
    } finally {
      if (isCurrent(request)) {
        operation = null
        if (pending) scheduleTranscriptLayout()
      }
    }
  }

  function maybeLoadOlderHistory() {
    const cursor = chat.current?.nextMessageCursor
    if (!transcriptPreparing.value && !revealMessageId && !positionLoadFailed.value && target?.clientHeight > 0
      && (target.scrollHeight <= target.clientHeight + 1
        || !followingBottom.value && target.scrollTop <= target.clientHeight)
      && cursor && !chat.olderMessagesLoadFailed && cursor !== stalledHistoryCursor) {
      void loadOlderHistory()
    }
  }

  async function loadOlderHistory() {
    if (disposed || chat.loading || historyRequest || !chat.current?.nextMessageCursor || chat.olderMessagesLoading
      || chat.current.id !== routeSessionId.value) return
    const sessionId = chat.current.id
    const cursor = chat.current.nextMessageCursor
    const request = { sessionId, firstMessage: chat.current.messages?.[0]?.id, anchor: captureAnchor() || readingAnchor }
    historyRequest = request
    try {
      await chat.loadOlderMessages(sessionId, followingBottom.value ? undefined : SCROLL_HISTORY_PAGE_SIZE)
      await request.positioned
      if (disposed || historyRequest !== request || chat.current?.id !== sessionId) return
      stalledHistoryCursor = chat.current.nextMessageCursor === cursor ? cursor : null
    } catch { /* The store exposes the retry state; automatic loading stops until retry. */ }
    finally {
      if (historyRequest === request) {
        historyRequest = null
        scheduleTranscriptLayout()
      }
    }
  }

  async function positionPrependedHistory(anchor) {
    cancelLayout()
    const request = beginLayout(virtualScroll.value, true)
    try {
      await nextTick()
      if (!isCurrent(request)) return
      const index = findAnchorIndex(anchor)
      if (index >= 0 && await position(request, index, 'start', anchor, true) && isCurrent(request)) {
        readingAnchor = captureAnchor() || anchor
      }
    } finally {
      if (operation === request) operation = null
    }
  }

  function handleVirtualScroll() {
    if (operation?.prepending) return
    if (operation && !operation.refreshing) pending = true
    if (!operation && userScrolling && !followingBottom.value) {
      const anchor = captureAnchor()
      readingAnchor = anchor || readingAnchor
      if (anchor) maybeLoadOlderHistory()
    }
    if (!operation && (transcriptPreparing.value || revealMessageId)) scheduleTranscriptLayout()
  }

  function beginActivityResize() {
    if (!chat.current) return
    cancelLayout()
    revealMessageId = null
    positionLoadFailed.value = false
    followingBottom.value = false
    readingAnchor = captureAnchor()
  }

  function updateFollowState() {
    const movedUp = target.scrollTop < lastScrollTop
    lastScrollTop = target.scrollTop
    if (transcriptPreparing.value || operation?.prepending) return
    if (operation && !userScrolling) return
    followingBottom.value = chatShouldFollowBottom(
      followingBottom.value, userScrolling && movedUp, userScrolling && !movedUp && chatBottomDistance(target) <= 64
    )
    const anchor = !operation && !followingBottom.value ? captureAnchor() : null
    if (!operation) readingAnchor = followingBottom.value ? null : anchor || readingAnchor
    if (userScrolling && movedUp) {
      if (!anchor && !transcriptPreparing.value && displayMessages.value.length) {
        // Wait for Quasar's scroll event rather than prepending to an offscreen slice.
        if (target.scrollTop <= 0) virtualScroll.value?.scrollTo(0, 'start')
      } else {
        maybeLoadOlderHistory()
      }
    }
    if (userScrolling && !touching) scheduleUserScrollEnd()
  }

  function beginUserScroll() {
    cancelLayout()
    revealMessageId = null
    positionLoadFailed.value = false
    userScrolling = true
    clearTimeout(userScrollTimer)
  }

  function scheduleUserScrollEnd() {
    clearTimeout(userScrollTimer)
    userScrollTimer = window.setTimeout(() => {
      userScrolling = false
      // Momentum scrolling can continue after touchend; capture only when it settles.
      if (!transcriptPreparing.value && !followingBottom.value) readingAnchor = captureAnchor() || readingAnchor
      scheduleTranscriptLayout()
    }, 180)
  }

  function handleTouchStart() { if (!transcriptPreparing.value) { touching = true; beginUserScroll() } }
  function handleTouchMove() { if (!transcriptPreparing.value && !userScrolling) beginUserScroll() }
  function handleTouchEnd() { touching = false; if (userScrolling) scheduleUserScrollEnd() }
  function handleWheel(event) {
    if (transcriptPreparing.value || !event.deltaY) return
    beginUserScroll()
    scheduleUserScrollEnd()
  }
  function handleScrollKey(event) {
    if (transcriptPreparing.value || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey
      || event.target?.closest?.('input, textarea, select, [contenteditable="true"]')
      || !['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) return
    beginUserScroll()
    scheduleUserScrollEnd()
  }
  function handleScrollAreaMouseDown(event) {
    if (transcriptPreparing.value || event.button !== 0 || !target || target.contains(event.target)) return
    beginUserScroll()
    window.removeEventListener('mouseup', scheduleUserScrollEnd)
    window.addEventListener('mouseup', scheduleUserScrollEnd, { once: true })
  }

  function enableFollowMode() {
    cancelLayout()
    revealMessageId = null
    positionLoadFailed.value = false
    touching = userScrolling = false
    clearTimeout(userScrollTimer)
    followingBottom.value = true
    readingAnchor = null
    scheduleTranscriptLayout()
  }

  watch(() => tabs.followLatestRequest, request => {
    if (request && request.sessionId === routeSessionId.value && request.sessionId === chat.current?.id) {
      enableFollowMode()
    }
  })

  watch([() => chat.current?.id, routeSessionId, routeMessageId], ([sessionId], previous = []) => {
    cancelLayout()
    positionLoadFailed.value = false
    revealMessageId = routeMessageId.value
    if (sessionId !== previous[0] || routeSessionId.value !== previous[1]) {
      positionSessionId = sessionId || null
      stalledHistoryCursor = null
      historyRequest = null
      clearTimeout(userScrollTimer)
      window.removeEventListener('mouseup', scheduleUserScrollEnd)
      touching = userScrolling = false
      readingAnchor = sessionId ? tabs.readingPositions[sessionId] || null : null
      followingBottom.value = !readingAnchor
      transcriptPreparing.value = Boolean(sessionId)
      resetSizes = true
    }
    scheduleTranscriptLayout()
  }, { immediate: true, flush: 'sync' })
  watch(displayMessages, () => {
    const request = historyRequest
    if (request && !request.positioned && !followingBottom.value && virtualScroll.value
      && !revealMessageId && routeSessionId.value === request.sessionId
      && chat.current?.id === request.sessionId && chat.current.messages?.[0]?.id !== request.firstMessage) {
      const anchor = captureAnchor() || request.anchor || readingAnchor
      if (anchor) {
        request.positioned = positionPrependedHistory(anchor)
        return
      }
    }
    scheduleTranscriptLayout()
  }, { flush: 'pre' })
  watch(() => chat.loading, loading => {
    if (loading) cancelLayout()
    else scheduleTranscriptLayout()
  }, { flush: 'sync' })
  watch(() => chat.olderMessagesLoading, loading => {
    if (!loading) scheduleTranscriptLayout()
  })
  watch(() => chat.activeContext.sessionProfile?.messageSize || 'standard', () => scheduleTranscriptLayout(true), { flush: 'pre' })
  watch(virtualScroll, () => {
    cancelLayout()
    scheduleTranscriptLayout()
  }, { flush: 'post' })

  const listeners = {
    scroll: updateFollowState, touchstart: handleTouchStart, touchmove: handleTouchMove,
    touchend: handleTouchEnd, touchcancel: handleTouchEnd, wheel: handleWheel
  }
  onMounted(() => {
    target = scrollArea.value?.getScrollTarget?.() || null
    lastScrollTop = target?.scrollTop || 0
    for (const [event, listener] of Object.entries(listeners)) target?.addEventListener(event, listener, { passive: true })
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => scheduleTranscriptLayout())
      if (scrollContent.value) observer.observe(scrollContent.value)
      if (target) observer.observe(target)
    }
    scheduleTranscriptLayout()
  })
  onBeforeUnmount(() => {
    savePosition()
    disposed = true
    cancelLayout()
    observer?.disconnect()
    for (const [event, listener] of Object.entries(listeners)) target?.removeEventListener(event, listener)
    window.removeEventListener('mouseup', scheduleUserScrollEnd)
    clearTimeout(userScrollTimer)
    clearTimeout(highlightTimer)
  })

  return {
    followingBottom, transcriptPreparing, highlightedMessageId, positionLoadFailed, retryPositionLoad,
    scheduleTranscriptLayout, loadOlderHistory, enableFollowMode,
    beginActivityResize, finishActivityResize: () => scheduleTranscriptLayout(),
    handleMessageRendered: () => scheduleTranscriptLayout(), handleVirtualScroll,
    handleScrollKey, handleScrollAreaMouseDown,
    scrollToTop: () => { if (target) target.scrollTop = 0 }
  }
}
