import { describe, expect, it } from 'vitest'

import {
  chatBottomDistance,
  chatBottomOffset,
  chatReadingAnchor,
  chatShouldFollowBottom,
  restoreChatReadingAnchor,
  scrollChatToBottom
} from '../src/features/chat/scroll'

describe('chat scroll geometry', () => {
  it('calculates the remaining distance and clamps non-scrollable content', () => {
    expect(chatBottomDistance({ clientHeight: 640, scrollHeight: 1840, scrollTop: 1176 })).toBe(24)
    expect(chatBottomDistance({ clientHeight: 640, scrollHeight: 500, scrollTop: 0 })).toBe(0)
    expect(chatBottomOffset({ clientHeight: 640, scrollHeight: 500 })).toBe(0)
  })

  it('moves the scroll target to the current bottom immediately', () => {
    const target = { clientHeight: 640, scrollHeight: 1920, scrollTop: 1200 }

    scrollChatToBottom(target)

    expect(target.scrollTop).toBe(1280)
  })

  it('follows explicit user movement without reacting to programmatic scrolls', () => {
    expect(chatShouldFollowBottom(false, false, false)).toBe(false)
    expect(chatShouldFollowBottom(false, false, true)).toBe(true)
    expect(chatShouldFollowBottom(true, false, false)).toBe(true)
    expect(chatShouldFollowBottom(true, true, false)).toBe(false)
    expect(chatShouldFollowBottom(true, true, true)).toBe(true)
  })

  it('captures the first visible message with its viewport offset', () => {
    const target = { getBoundingClientRect: () => ({ top: 100, bottom: 700 }) }
    const elements = [
      {
        dataset: { messageId: 'above' },
        getBoundingClientRect: () => ({ top: 0, bottom: 80 })
      },
      {
        dataset: { messageId: 'visible', renderId: 'optimistic' },
        getBoundingClientRect: () => ({ top: 60, bottom: 180 })
      }
    ]

    expect(chatReadingAnchor(target, elements)).toEqual({ id: 'optimistic', messageId: 'visible', offsetTop: -40 })
  })

  it('restores the captured message offset after layout changes', () => {
    const target = {
      scrollTop: 900,
      getBoundingClientRect: () => ({ top: 100 })
    }
    const element = { getBoundingClientRect: () => ({ top: 240 }) }

    restoreChatReadingAnchor(target, element, { id: 'visible', offsetTop: -40 })

    expect(target.scrollTop).toBe(1080)
  })
})
