export function chatBottomDistance(target) {
  if (!target) return 0
  return Math.max(0, target.scrollHeight - target.clientHeight - target.scrollTop)
}

export function chatBottomOffset(target) {
  if (!target) return 0
  return Math.max(0, target.scrollHeight - target.clientHeight)
}

export function scrollChatToBottom(target) {
  if (!target) return
  target.scrollTop = chatBottomOffset(target)
}

export function chatShouldFollowBottom(following, userScrolledUp, userReachedBottom) {
  if (userReachedBottom) return true
  if (userScrolledUp) return false
  return following
}

export function chatReadingAnchor(target, elements = []) {
  const viewport = target?.getBoundingClientRect?.()
  if (!viewport) return null
  for (const element of elements) {
    const bounds = element.getBoundingClientRect()
    if (bounds.bottom <= viewport.top || bounds.top >= viewport.bottom) continue
    const id = element.dataset?.renderId || element.dataset?.messageId
    if (id) return { id, messageId: element.dataset.messageId || id, offsetTop: bounds.top - viewport.top }
  }
  return null
}

export function restoreChatReadingAnchor(target, element, anchor) {
  if (!target || !element || !anchor) return
  const viewportTop = target.getBoundingClientRect().top
  target.scrollTop += element.getBoundingClientRect().top - viewportTop - anchor.offsetTop
}
