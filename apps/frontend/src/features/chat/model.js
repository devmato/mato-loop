import { api } from 'src/boot/axios'
import { absoluteComputerPath } from 'src/features/files/state'

const SESSION_PAGE_SIZE = 20
const ACTIVE_TITLE_GENERATION_STATUSES = new Set(['pending', 'running'])
const FILESYSTEM_MUTATION_TOOLS = new Set([
  'write_file',
  'edit_file',
  'create_directory',
  'delete_path',
  'copy_path',
  'move_path'
])
const OUTBOX_RETRY_MS = 4000

function absoluteToolPath(path, currentDirectory) {
  if (absoluteComputerPath(path)) return path
  if (!path || !absoluteComputerPath(currentDirectory)) return null
  const parts = `${currentDirectory}/${path}`.split('/')
  const drive = /^[A-Z]:\//u.test(currentDirectory) ? parts.shift() : null
  const normalized = []
  for (const part of parts) {
    if (!part || part === '.') continue
    if (part === '..') normalized.pop()
    else normalized.push(part)
  }
  return drive ? `${drive}/${normalized.join('/')}` : `/${normalized.join('/')}`
}

function controlCenterFileTarget(root, path, session) {
  if (!path) return null
  const name = path.split('/').filter(Boolean).at(-1)
  if (!name) return null
  if (['files', 'session'].includes(root)) {
    return {
      scope: 'session',
      path,
      name,
      rootIdentity: session.id,
      readOnly: false
    }
  }
  if (!['computer', 'workspace'].includes(root)) return null
  const absolutePath = absoluteToolPath(path, session.currentDirectory)
  if (!absolutePath || !session.preferredExecutionAgentId || !session.currentDirectory) return null
  return {
    scope: 'filesystem',
    path: absolutePath,
    name,
    executionAgentId: session.preferredExecutionAgentId,
    currentDirectory: session.currentDirectory,
    readOnly: false
  }
}

export function compareMessages(left, right) {
  // Partial responses can precede a direct message by only one microsecond.
  return Date.parse(left.createdAt) - Date.parse(right.createdAt)
    || Number(left.createdAt?.match(/\.\d+/u)?.[0] || 0) - Number(right.createdAt?.match(/\.\d+/u)?.[0] || 0)
    || (left.createdAt && right.createdAt ? left.id.localeCompare(right.id) : 0)
}

function mergeMessages(existing = [], incoming = []) {
  const replacements = new Map(incoming.map((message) => [message.id, message]))
  const merged = existing.map((message) => {
    const replacement = replacements.get(message.id)
    return replacement && message.renderId
      ? { ...replacement, renderId: message.renderId } : replacement || message
  })
  const existingIds = new Set(existing.map((message) => message.id))
  merged.push(...incoming.filter((message) => !existingIds.has(message.id)))
  return merged.sort(compareMessages)
}

async function sessionStateAfterMutation(sessionId, messageId) {
  try {
    await api.get(`/sessions/${sessionId}/messages/${messageId}`, { suppressErrorToast: true })
    return {
      deleted: false,
      messageRemoved: false,
      session: null
    }
  } catch (error) {
    if (error?.response?.status !== 404) throw error
  }
  try {
    const { data } = await api.get(`/sessions/${sessionId}`, { suppressErrorToast: true })
    return { deleted: false, messageRemoved: true, session: data }
  } catch (error) {
    if (error?.response?.status === 404) return { deleted: true, messageRemoved: true, session: null }
    throw error
  }
}

export { SESSION_PAGE_SIZE, ACTIVE_TITLE_GENERATION_STATUSES, FILESYSTEM_MUTATION_TOOLS, OUTBOX_RETRY_MS, absoluteToolPath, controlCenterFileTarget, mergeMessages, sessionStateAfterMutation }
