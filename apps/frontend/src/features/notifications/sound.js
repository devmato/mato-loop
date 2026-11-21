import { claimNotificationSound } from './sound-claim'

export const NOTIFICATION_SOUND_KEY = 'matoLoopNotificationSoundEnabled'
let context

export function readNotificationSoundSetting() {
  try {
    return window.localStorage.getItem(NOTIFICATION_SOUND_KEY) === 'true'
  } catch {
    return false
  }
}

export function saveNotificationSoundSetting(enabled) {
  try {
    window.localStorage.setItem(NOTIFICATION_SOUND_KEY, String(enabled))
  } catch {
    // Keep the setting for this page if storage is unavailable.
  }
}

function playNotificationTone() {
  if (context?.state !== 'running') return false
  try {
    const now = context.currentTime
    const oscillator = context.createOscillator()
    const gain = context.createGain()
    oscillator.frequency.setValueAtTime(660, now)
    oscillator.frequency.setValueAtTime(880, now + 0.09)
    gain.gain.setValueAtTime(0, now)
    gain.gain.linearRampToValueAtTime(0.13, now + 0.015)
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.22)
    oscillator.connect(gain).connect(context.destination)
    oscillator.start(now)
    oscillator.stop(now + 0.23)
    return true
  } catch {
    return false
  }
}

export async function unlockNotificationSound({ preview = false, enabled = () => true } = {}) {
  try {
    const AudioContext = window.AudioContext || window.webkitAudioContext
    if (!AudioContext) return
    context ??= new AudioContext()
    if (context.state !== 'running') await context.resume()
    if (preview && enabled()) playNotificationTone()
  } catch {
    // Autoplay restrictions must not interfere with the notification UI.
  }
}

export async function playNewNotificationSound(ids, enabled, allowFallback) {
  if (!ids.length || context?.state !== 'running') return
  const play = () => enabled() && playNotificationTone()
  try {
    await claimNotificationSound(ids, play)
  } catch {
    // When shared storage is blocked, prefer native push if it can supply the sound.
    if (allowFallback()) play()
  }
}
