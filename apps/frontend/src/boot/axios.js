import axios from 'axios'
import { getActivePinia } from 'pinia'
import { boot } from 'quasar/wrappers'
import { decodeBlobApiError, errorDescriptor, isApiConnectionFailure } from 'src/utils/api-errors'
import {
  notifyBackendReachable,
  notifyBackendUnavailable,
  notifyError
} from 'src/utils/notifications'

const api = axios.create({
  baseURL: '/api',
  withCredentials: true
})

const SESSION_INVALIDATION_CODES = new Set([
  'auth.accountDisabled',
  'auth.notAuthenticated',
  'auth.sessionExpired',
  'auth.userMissing'
])
let sessionInvalidation = null
let adminRefresh = null

export async function recoverControlledAuth(error) {
  const code = errorDescriptor(error)?.code
  const pinia = getActivePinia()
  if (SESSION_INVALIDATION_CODES.has(code)) {
    if (sessionInvalidation?.pinia !== pinia) {
      const request = import('src/stores/auth')
        .then(({ useAuthStore }) => useAuthStore(pinia).invalidateSession())
        .finally(() => {
        if (sessionInvalidation?.request === request) sessionInvalidation = null
      })
      sessionInvalidation = { pinia, request }
    }
    await sessionInvalidation.request
  } else if (code === 'auth.adminRequired' && !error.config?.authRefresh) {
    adminRefresh ||= import('src/stores/auth')
      .then(({ useAuthStore }) => useAuthStore(pinia).fetchMe({
        authRefresh: true,
        suppressErrorToast: true
      }))
      .catch(() => {})
      .finally(() => { adminRefresh = null })
    await adminRefresh
  }
}

api.interceptors.response.use(
  (response) => {
    notifyBackendReachable()
    return response
  },
  async (error) => {
    if (axios.isCancel(error)) return Promise.reject(error)
    const decoded = await decodeBlobApiError(error)
    await recoverControlledAuth(decoded)
    if (isApiConnectionFailure(decoded)) {
      notifyBackendUnavailable(probeBackend, decoded.config?.suppressErrorToast ? null : decoded)
    }
    else {
      if (decoded.response) notifyBackendReachable()
      if (!decoded.config?.suppressErrorToast) notifyError(decoded)
    }
    return Promise.reject(decoded)
  }
)

async function probeBackend(signal) {
  const { data } = await axios.get('/api/health/live', { signal, timeout: 3000 })
  return data?.status === 'ok'
}

export default boot(({ app }) => {
  app.config.globalProperties.$axios = axios
  app.config.globalProperties.$api = api
})

export { api }
