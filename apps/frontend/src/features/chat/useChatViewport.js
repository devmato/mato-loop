import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useSessionTabsStore } from './session-tabs-store'
import { mergeMessages } from './model'
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
const HISTORY_PROGRESS_DELAY = 250
const STORED_MESSAGE_ID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i

export function useChatViewport({ chat, route, scrollArea, scrollContent, virtualScroll, displayMessages, historyWindow, setWorkHistoryExpanded }) {
  const tabs = useSessionTabsStore()
  const routeSessionId = computed(() => typeof route.query.session === 'string' ? route.query.session : null)
  const routeMessageId = computed(() => typeof route.query.message === 'string' ? route.query.message : null)
  const followingBottom = ref(true)
  const transcriptPreparing = ref(Boolean(chat.current))
  const positionLoadFailed = ref(false)
  const showHistoryProgress = ref(false)
  const historyBottomSpace = ref(0)
  const highlightedMessageId = ref(null)
  let target, observer, operation, scheduledFrame, userScrollTimer, highlightTimer
  let disposed = false
  let pending = false
  let resetSizes = false
  let revealMessageId = null
  let readingAnchor = null
  let deferredReconciliation = null
  let restoreReconciledAnchor = false
  let userScrolling = false
  let touching = false
  let touchStartY = null
  let browsingHistory = false
  let lastScrollTop = 0
  const stalledHistoryCursor = ref(null)
  const historyCursor = computed(() => historyWindow.value ? historyWindow.value.nextCursor : chat.current?.nextMessageCursor)
  const historyItems = computed(() => historyWindow.value?.items || chat.current?.messages || [])
  const windowPageFailed = ref(false)
  const olderHistoryFailed = computed(() => Boolean(historyCursor.value)
    && (historyWindow.value ? windowPageFailed.value : chat.olderMessagesLoadFailed || historyCursor.value === stalledHistoryCursor.value))
  let historyRequest = null
  let historyProgressTimer
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
  const isCurrent = request => !disposed && operation === request
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
    operation?.controller?.abort()
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
    if (index >= 0 || !anchor || !historyCursor.value) return index
    const windowResult = await jumpToMessageWindow(anchor.messageId, request)
    if (windowResult === 'missing' || !isCurrent(request)) return -1
    if (windowResult === 'loaded') return findAnchorIndex(anchor)
    return loadUntil(() => findAnchorIndex(anchor), request)
  }

  async function jumpToMessageWindow(id, request) {
    // Local transcript rows (e.g. context summaries) have no stored message ID.
    if (!STORED_MESSAGE_ID.test(id)) return 'fallback'
    request.controller = new AbortController()
    const result = await chat.fetchMessageWindow(request.sessionId, id, request.controller.signal)
    if (!isCurrent(request) || !result) return 'missing'
    if (!result.items?.length) return 'fallback'
    transcriptPreparing.value = true
    historyWindow.value = result
    windowPageFailed.value = false
    resetSizes = true
    return 'loaded'
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
    } else {
      if (request.reserveAnchorSpace && target && element && anchor && scrollContent.value) {
        // scrollHeight is at least clientHeight even if the actual window is
        // shorter. Calculate padding from the content, not that lower bound.
        const desired = target.scrollTop + element.getBoundingClientRect().top - target.getBoundingClientRect().top - anchor.offsetTop
        if (desired > 0 && desired > target.scrollHeight - target.clientHeight) {
          const height = scrollContent.value.getBoundingClientRect().height
          historyBottomSpace.value += Math.ceil(desired + target.clientHeight - Math.min(height, target.scrollHeight))
          await nextTick()
          if (!isCurrent(request)) return false
        }
      }
      restoreChatReadingAnchor(target, element, anchor)
    }
    lastScrollTop = target?.scrollTop || 0
    return element || true
  }

  // Virtual-scroll events are notifications, not completion promises: reset may emit none.
  async function layoutTranscript() {
    if (disposed || userScrolling || operation || positionLoadFailed.value || chat.olderMessagesLoading || !virtualScroll.value
      || routeSessionId.value && chat.current?.id !== routeSessionId.value) return
    const request = beginLayout(virtualScroll.value)
    pending = false
    const messageId = revealMessageId
    const opening = transcriptPreparing.value
    let anchor = followingBottom.value ? null : readingAnchor || (!opening ? captureAnchor() : null)
    try {
      const matches = message => message.id === messageId || message.workHistory?.entries?.some(entry => (
        entry.message?.id === messageId || entry.batchId === messageId || entry.result?.id === messageId
      ))
      let findTarget = () => displayMessages.value.findIndex(matches)
      let searchIndex = -1
      try {
        if (messageId) {
          searchIndex = findTarget()
          if (searchIndex < 0) {
            if (chat.loading) return
            if (historyCursor.value || chat.current?.messages?.some(message => message.id === messageId)) {
              const result = await jumpToMessageWindow(messageId, request)
              if (!isCurrent(request)) return
              if (result === 'loaded') {
                // Some assistant rows are folded away entirely. This window
                // only contains the requested section; use its visible reply.
                if (findTarget() < 0) findTarget = () => displayMessages.value.findLastIndex(message => message.role === 'assistant')
                searchIndex = findTarget()
                if (searchIndex < 0) historyWindow.value = null
              } else if (result === 'fallback') searchIndex = await loadUntil(findTarget, request)
            }
          }
        }
        if (!isCurrent(request)) return
        if (searchIndex < 0 && (opening || restoreReconciledAnchor) && !followingBottom.value) {
          if (chat.loading && findAnchorIndex(anchor) < 0) return
          const index = await loadReadingAnchor(anchor, request)
          if (!isCurrent(request)) return
          if (index < 0) {
            // Do not reveal the incoming transcript at an inherited offset.
            anchor = readingAnchor = null
            delete tabs.readingPositions[request.sessionId]
            followingBottom.value = true
            historyBottomSpace.value = 0
            historyWindow.value = null
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
        if (searchedMessage.id !== messageId && searchedMessage.workHistory) {
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
      const index = searchedMessage ? findTarget() : followingBottom.value ? messages.length - 1 : findAnchorIndex(anchor)
      const edge = searchedMessage ? 'center' : followingBottom.value ? 'end' : 'start'
      request.reserveAnchorSpace = (opening || restoreReconciledAnchor) && edge === 'start'
      const element = await position(request, index, edge, anchor, Boolean(searchedMessage || request.reserveAnchorSpace && anchor))
      if (!isCurrent(request) || !element) return
      transcriptPreparing.value = false
      restoreReconciledAnchor = false
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
      // An underfilled preview must not move after it becomes visible. Only
      // fill ahead of a reader who has started browsing toward older messages.
      if (!messageId) maybeLoadOlderHistory()
    } finally {
      if (isCurrent(request)) {
        operation = null
        if (pending) scheduleTranscriptLayout()
      }
    }
  }

  function maybeLoadOlderHistory() {
    const cursor = historyCursor.value
    if (browsingHistory && !followingBottom.value && !transcriptPreparing.value && !revealMessageId
      && !positionLoadFailed.value && target?.clientHeight > 0 && target.scrollTop <= target.clientHeight
      && cursor && !olderHistoryFailed.value) {
      void loadOlderHistory()
    }
  }

  function resetHistoryProgress() {
    clearTimeout(historyProgressTimer)
    showHistoryProgress.value = false
  }

  async function loadOlderHistory() {
    if (disposed || chat.loading || historyRequest || !historyCursor.value || chat.olderMessagesLoading
      || chat.current.id !== routeSessionId.value) return
    const sessionId = chat.current.id
    const cursor = historyCursor.value
    const currentWindow = historyWindow.value
    const request = {
      sessionId, firstMessage: historyItems.value[0]?.id, anchor: captureAnchor() || readingAnchor,
      controller: currentWindow ? new AbortController() : null
    }
    followingBottom.value = false
    readingAnchor = request.anchor
    historyRequest = request
    if (target && scrollContent.value && !historyBottomSpace.value && target.scrollHeight === target.clientHeight) {
      // Keep the empty space below a short preview so prepends can restore its anchor.
      historyBottomSpace.value = Math.max(0, Math.floor(target.clientHeight - scrollContent.value.getBoundingClientRect().height))
    }
    stalledHistoryCursor.value = null
    windowPageFailed.value = false
    clearTimeout(historyProgressTimer)
    if (!showHistoryProgress.value) {
      historyProgressTimer = window.setTimeout(() => { showHistoryProgress.value = true }, HISTORY_PROGRESS_DELAY)
    }
    try {
      const pageSize = request.anchor ? SCROLL_HISTORY_PAGE_SIZE : undefined
      if (currentWindow) {
        const page = await chat.fetchOlderMessages(sessionId, cursor, pageSize, request.controller.signal)
        if (historyRequest !== request || historyWindow.value !== currentWindow) return
        if (page.nextCursor === cursor) windowPageFailed.value = true
        historyWindow.value = {
          ...currentWindow,
          items: mergeMessages(page.items, currentWindow.items),
          contextSummaries: mergeMessages(page.contextSummaries, currentWindow.contextSummaries),
          nextCursor: page.nextCursor || null
        }
        await nextTick()
      } else await chat.loadOlderMessages(sessionId, pageSize)
      await request.positioned
      if (disposed || historyRequest !== request || chat.current?.id !== sessionId) return
      if (historyCursor.value === cursor) stalledHistoryCursor.value = cursor
    } catch {
      if (currentWindow && historyRequest === request) windowPageFailed.value = true
    }
    finally {
      if (historyRequest === request) {
        historyRequest = null
        clearTimeout(historyProgressTimer)
        if (olderHistoryFailed.value) resetHistoryProgress()
        else if (showHistoryProgress.value) {
          // Keep the indicator stable between consecutive requests.
          historyProgressTimer = window.setTimeout(resetHistoryProgress, HISTORY_PROGRESS_DELAY)
        }
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
    const movedDown = target.scrollTop > lastScrollTop
    lastScrollTop = target.scrollTop
    if (transcriptPreparing.value || operation?.prepending) return
    if (operation && !userScrolling) return
    followingBottom.value = chatShouldFollowBottom(
      followingBottom.value, userScrolling && movedUp, !historyWindow.value && userScrolling && movedDown && chatBottomDistance(target) <= 64
    )
    if (followingBottom.value) {
      browsingHistory = false
      historyBottomSpace.value = 0
    }
    const anchor = !operation && !followingBottom.value ? captureAnchor() : null
    if (!operation) readingAnchor = followingBottom.value ? null : anchor || readingAnchor
    if (userScrolling && movedUp) {
      browsingHistory = true
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
    if (!operation?.prepending) cancelLayout()
    revealMessageId = null
    positionLoadFailed.value = false
    restoreReconciledAnchor = false
    userScrolling = true
    clearTimeout(userScrollTimer)
  }

  function scheduleUserScrollEnd() {
    clearTimeout(userScrollTimer)
    userScrollTimer = window.setTimeout(() => {
      userScrolling = false
      // Momentum scrolling can continue after touchend; capture only when it settles.
      if (!transcriptPreparing.value && !followingBottom.value) readingAnchor = captureAnchor() || readingAnchor
      if (deferredReconciliation) {
        const previous = deferredReconciliation
        deferredReconciliation = null
        reconcileReadingPosition(previous)
      }
      scheduleTranscriptLayout()
    }, 180)
  }

  function browseFromTop() {
    // When the preview is short, an upward scroll gesture can leave scrollTop
    // unchanged. In that case the gesture, not a scroll event, starts loading.
    if (!target || target.scrollTop > 0 || !historyCursor.value) return
    browsingHistory = true
    followingBottom.value = false
    readingAnchor = captureAnchor() || readingAnchor
    maybeLoadOlderHistory()
  }
  function handleTouchStart(event) {
    if (transcriptPreparing.value) return
    touching = true
    touchStartY = event.touches?.[0]?.clientY ?? null
    beginUserScroll()
  }
  function handleTouchMove(event) {
    if (transcriptPreparing.value) return
    if (!userScrolling) beginUserScroll()
    if (touchStartY != null && event.touches?.[0]?.clientY - touchStartY > 8) browseFromTop()
  }
  function handleTouchEnd() {
    touching = false
    touchStartY = null
    if (userScrolling) scheduleUserScrollEnd()
  }
  function handleWheel(event) {
    if (transcriptPreparing.value || event.ctrlKey || !event.deltaY) return
    beginUserScroll()
    if (event.deltaY < 0) browseFromTop()
    scheduleUserScrollEnd()
  }
  function handleScrollKey(event) {
    if (transcriptPreparing.value || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey
      || event.target?.closest?.('input, textarea, select, [contenteditable="true"]')
      || !['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) return
    beginUserScroll()
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key) || (event.key === ' ' && event.shiftKey)) browseFromTop()
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
    browsingHistory = false
    historyBottomSpace.value = 0
    readingAnchor = null
    deferredReconciliation = null
    restoreReconciledAnchor = false
    if (historyWindow.value) {
      historyRequest?.controller?.abort()
      historyRequest = null
      resetHistoryProgress()
      transcriptPreparing.value = true
      historyWindow.value = null
    }
    scheduleTranscriptLayout(true)
  }

  watch(() => tabs.followLatestRequest, request => {
    if (request && request.sessionId === routeSessionId.value && request.sessionId === chat.current?.id) {
      enableFollowMode()
    }
  })

  watch([() => chat.current?.id, routeSessionId, routeMessageId], ([sessionId], previous = []) => {
    cancelLayout()
    deferredReconciliation = null
    restoreReconciledAnchor = false
    positionLoadFailed.value = false
    revealMessageId = routeMessageId.value
    browsingHistory = false
    if (historyWindow.value) {
      historyWindow.value = null
      historyRequest?.controller?.abort()
      historyRequest = null
      historyBottomSpace.value = 0
      resetHistoryProgress()
      followingBottom.value = true
      readingAnchor = null
      transcriptPreparing.value = Boolean(sessionId)
      resetSizes = true
    }
    if (sessionId !== previous[0] || routeSessionId.value !== previous[1]) {
      positionSessionId = sessionId || null
      stalledHistoryCursor.value = null
      historyRequest = null
      historyBottomSpace.value = 0
      resetHistoryProgress()
      clearTimeout(userScrollTimer)
      window.removeEventListener('mouseup', scheduleUserScrollEnd)
      touching = userScrolling = false
      touchStartY = null
      readingAnchor = sessionId ? tabs.readingPositions[sessionId] || null : null
      followingBottom.value = !readingAnchor
      transcriptPreparing.value = Boolean(sessionId)
      resetSizes = true
    }
    scheduleTranscriptLayout()
  }, { immediate: true, flush: 'sync' })

  function reconcileReadingPosition(previous) {
    if (followingBottom.value || revealMessageId || !virtualScroll.value) return
    let anchor = readingAnchor
    if (anchor && findAnchorIndex(anchor) < 0) {
      if (historyCursor.value && STORED_MESSAGE_ID.test(anchor.messageId)) {
        // A bounded reopen may not reach this row. Reopen its section directly
        // instead of paging the gap or discarding the saved position.
        restoreReconciledAnchor = true
        return
      }
      // Local rows cannot be looked up by ID. Prefer a surviving nearby row.
      const index = previous.findIndex(message => rowId(message) === anchor.id || message.id === anchor.messageId)
      const available = new Set(displayMessages.value.map(rowId))
      const neighbor = [...previous.slice(0, index + 1).reverse(), ...previous.slice(index + 1)]
        .find(message => available.has(rowId(message)))
      anchor = neighbor ? { ...anchor, id: rowId(neighbor), messageId: neighbor.id } : null
    }
    readingAnchor = anchor
    if (!anchor) {
      followingBottom.value = true
      historyBottomSpace.value = 0
    }
  }

  watch([displayMessages, () => chat.current], ([, session], [previous, oldSession]) => {
    const request = historyRequest
    if (request && !request.positioned && !followingBottom.value && virtualScroll.value
      && !revealMessageId && routeSessionId.value === request.sessionId
      && chat.current?.id === request.sessionId && historyItems.value[0]?.id !== request.firstMessage) {
      const anchor = captureAnchor() || request.anchor || readingAnchor
      if (anchor) {
        request.positioned = positionPrependedHistory(anchor)
        return
      }
    }
    const reconciled = !historyWindow.value && session && session !== oldSession && session.id === oldSession?.id && !chat.loading
    if (reconciled && transcriptPreparing.value) cancelLayout()
    if (reconciled && !transcriptPreparing.value && !followingBottom.value) {
      readingAnchor = captureAnchor() || readingAnchor
      if (userScrolling) deferredReconciliation = previous
      else {
        cancelLayout()
        reconcileReadingPosition(previous)
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
    historyRequest?.controller?.abort()
    observer?.disconnect()
    for (const [event, listener] of Object.entries(listeners)) target?.removeEventListener(event, listener)
    window.removeEventListener('mouseup', scheduleUserScrollEnd)
    clearTimeout(userScrollTimer)
    clearTimeout(highlightTimer)
    resetHistoryProgress()
  })

  return {
    followingBottom, transcriptPreparing, highlightedMessageId, positionLoadFailed, retryPositionLoad,
    scheduleTranscriptLayout, loadOlderHistory, olderHistoryFailed, showHistoryProgress, historyBottomSpace, enableFollowMode,
    beginActivityResize, finishActivityResize: () => scheduleTranscriptLayout(),
    handleMessageRendered: () => scheduleTranscriptLayout(), handleVirtualScroll,
    handleScrollKey, handleScrollAreaMouseDown,
    scrollToTop: () => { if (target) target.scrollTop = 0 }
  }
}
