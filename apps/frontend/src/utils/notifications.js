import { Notify } from 'quasar'
import { readonly, ref } from 'vue'
import { apiErrorMessage } from 'src/utils/api-errors'

const unavailable = ref(false)
export const backendUnavailable = readonly(unavailable)
let checkTimer = null
let outage = null
const pendingErrors = new Set()

export function notifyBackendUnavailable(probe, error = null) {
  if (error && !unavailable.value) pendingErrors.add(apiErrorMessage(error))
  if (outage) return
  const attempt = new AbortController()
  outage = attempt
  async function check() {
    const reachable = await probe(attempt.signal).catch(() => false)
    if (outage !== attempt) return
    if (reachable) {
      notifyBackendReachable()
      return
    }
    // A confirmed outage has its own status notice, not one toast per failed request.
    pendingErrors.clear()
    unavailable.value = true
    checkTimer = setTimeout(check, 5000)
  }
  checkTimer = setTimeout(check, 1500)
}

export function notifyBackendReachable() {
  if (!outage) return
  clearTimeout(checkTimer)
  outage.abort()
  outage = null
  unavailable.value = false
  // If the app remained reachable, report the failed foreground requests instead.
  for (const message of pendingErrors) Notify.create({ type: 'negative', message })
  pendingErrors.clear()
}

export function notifyError(error, fallback) {
  Notify.create({
    type: 'negative',
    message: apiErrorMessage(error, fallback)
  })
}
