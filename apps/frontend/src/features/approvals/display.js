import { cleanUrlForDisplay } from 'src/utils/webfetch'

const REDACTED_KEY = /(authorization|body|content|credential|password|secret|token|api[_-]?key)/i
const URL_KEY = /(url|uri)$/i
const MAX_STRING_LENGTH = 300
const MAX_ARRAY_ITEMS = 20
const MAX_OBJECT_KEYS = 30
const MAX_DEPTH = 4
const MAX_DETAILS_LENGTH = 5000

function detailName(details, keys) {
  if (!details || typeof details !== 'object') return null
  for (const key of keys) {
    const value = details[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return null
}

export function memoryApprovalDisplay(approval) {
  const payload = approval?.payload && typeof approval.payload === 'object' ? approval.payload : {}
  const targets = Array.isArray(payload.targetMemories)
    ? payload.targetMemories.filter((item) => item && typeof item === 'object')
    : []
  const action = {
    memory_update: {
      title: 'Erinnerung ändern',
      summary: 'Dreaming möchte eine bestehende Erinnerung aktualisieren.',
      targetLabel: 'Bisherige Erinnerung',
      icon: 'psychology'
    },
    memory_merge: {
      title: 'Erinnerungen zusammenführen',
      summary: 'Dreaming möchte mehrere Erinnerungen zu einer Erinnerung zusammenführen.',
      targetLabel: 'Bisherige Erinnerungen',
      icon: 'psychology'
    },
    memory_remove: {
      title: 'Erinnerung dauerhaft entfernen',
      summary: 'Dreaming möchte diese Erinnerung unwiderruflich löschen.',
      targetLabel: 'Zu entfernende Erinnerung',
      icon: 'delete_forever',
      destructive: true
    },
    memory_pin: {
      title: payload.requestedPin ? 'Erinnerung: Immer im Kontext' : 'Erinnerung: Bei passender Anfrage',
      summary: 'Dreaming möchte die Berücksichtigung einer Erinnerung ändern.',
      targetLabel: 'Bisherige Erinnerung',
      icon: 'push_pin'
    }
  }[approval?.actionKind] || {
    title: 'Memory-Änderung prüfen',
    summary: 'Dreaming möchte eine Erinnerung ändern.',
    targetLabel: 'Bisherige Erinnerung',
    icon: 'psychology'
  }
  const afterRequired = !['memory_remove', 'memory_pin'].includes(approval?.actionKind)
  return {
    ...action,
    payload,
    targets,
    invalid: !targets.length
      || !Array.isArray(approval?.availableDecisions)
      || (afterRequired && (!payload.after || typeof payload.after !== 'object'))
  }
}

export function approvalResource(approval, sessions = []) {
  const scope = String(approval?.resourceScope || '')
  if (approval?.capability === 'memory.write') {
    return { label: 'Dauerhafte Erinnerungen', value: 'Persönliche Memories' }
  }
  if (['shell_command', 'shell_action', 'execution_administrator'].includes(approval?.resourceType)) {
    return {
      label: approval?.resourceType === 'shell_action' || approval?.actionKind === 'shell_run' ? 'Arbeitsverzeichnis' : 'Datei oder Ordner',
      format: 'code',
      value: approval?.currentDirectory || approval?.resourceDisplay || 'Pfad nicht verfügbar'
    }
  }
  if (['path', 'root_path', 'execution_root_path', 'execution_path', 'session_path', 'administrator_path'].includes(approval?.resourceType)) {
    return {
      label: 'Datei oder Ordner',
      format: 'code',
      value: approval?.resourceDisplay || (approval?.resourceType === 'path' ? scope : '') || 'Pfad nicht verfügbar'
    }
  }
  if (approval?.resourceType === 'execution_directory') {
    return {
      label: approval?.capability?.startsWith('filesystem.') || approval?.capability === 'file.provide'
        ? 'Datei oder Ordner' : 'Ordner und Unterordner',
      format: 'code',
      value: approval?.resourceDisplay || 'Pfad nicht verfügbar'
    }
  }
  if (approval?.resourceType === 'execution_agent') {
    return {
      label: 'Computer',
      value: approval?.resourceDisplay || 'Ausgewählter Computer'
    }
  }
  if (approval?.resourceType === 'execution_workspace') {
    return {
      label: 'Aktueller Ordner',
      format: 'code',
      value: approval?.resourceDisplay || 'Ausgewählter Computer'
    }
  }
  if (approval?.resourceType === 'url') {
    const url = approval?.resourceDisplay
      || approval?.details?.arguments?.url
      || approval?.details?.url
      || (/^[0-9a-f]{64}$/i.test(scope) ? null : scope)
    return {
      label: 'Webadresse',
      value: url ? cleanUrlForDisplay(url) : 'Exakt freigegebene Webadresse'
    }
  }
  if (approval?.resourceType === 'session') {
    const name = detailName(approval, ['resourceDisplay'])
      || detailName(approval.details, ['sessionName', 'sessionTitle', 'resourceName'])
    const sessionTitle = sessions.find((session) => session.id === scope)?.title
    const currentSession = scope && scope === approval.sessionId
    return {
      label: 'Session',
      value: name
        ? `„${name}“`
        : currentSession
          ? 'Aktuelle Session'
          : sessionTitle
            ? `„${sessionTitle}“`
            : 'Andere Session'
    }
  }
  if (approval?.resourceType === 'session_collection') {
    const name = detailName(approval.details, ['collectionName', 'resourceName'])
    const allSessions = !scope || ['*', 'all', 'sessions'].includes(scope.toLowerCase())
    return {
      label: 'Session-Sammlung',
      value: name ? `„${name}“` : allSessions ? 'Alle Sessions' : 'Ausgewählte Sessions'
    }
  }
  if (approval?.resourceType === 'network_service') {
    return { label: 'Netzwerkdienst', value: approval?.resourceDisplay || 'Konfigurierte Websuche' }
  }
  if (approval?.resourceType === 'integration_operation') {
    return {
      label: 'Integrationsressource',
      value: approval?.resourceDisplay || 'Exakt angeforderte Integrationsaktion'
    }
  }
  if (approval?.resourceType === 'integration_resource') {
    return {
      label: 'Integrationsbereich',
      value: approval?.resourceDisplay || 'Gebundener Integrationsbereich'
    }
  }
  if (approval?.resourceType === 'memory_collection') {
    return { label: 'Memory-Sammlung', value: approval?.resourceDisplay || 'Eigene Memories' }
  }
  if (approval?.resourceType === 'user_profile') {
    return { label: 'Persönliches Profil', value: approval?.resourceDisplay || 'Eigenes persönliches Profil' }
  }
  if (approval?.resourceType === 'personal_variable') {
    return { label: 'Persönliche Variable', value: approval?.resourceDisplay ? `„${approval.resourceDisplay}“` : 'Ausgewählte Variable' }
  }
  if (approval?.resourceType === 'personal_variable_collection') {
    return { label: 'Variablen & Secrets', value: 'Eigene Variablen und Secrets' }
  }
  if (approval?.resourceType === 'skill') {
    return { label: 'Skill', value: approval?.resourceDisplay ? `„${approval.resourceDisplay}“` : 'Ausgewählter Skill' }
  }
  if (approval?.resourceType === 'skill_collection') {
    return {
      label: 'Skill-Sammlung',
      value: approval?.resourceDisplay ? `„${approval.resourceDisplay}“` : 'Persönliche Skills'
    }
  }
  if (approval?.resourceType === 'automation_collection') {
    return { label: 'Automationen', value: approval?.resourceDisplay || 'Eigene Automationen' }
  }
  if (approval?.resourceType === 'automation') {
    return { label: 'Automation', value: approval?.resourceDisplay || 'Ausgewählte Automation' }
  }
  if (approval?.resourceType === 'project_session_path') {
    return { label: 'Datei einer Projekt-Session', value: approval?.resourceDisplay || 'Ausgewählter Bereich', format: 'code' }
  }
  const label = {
    execution_agent_collection: 'Computer',
    media_model: 'Medienmodell',
    agent_identity: 'Agent-Persona',
    personal_settings: 'Persönliche Einstellungen',
    project: 'Projekt'
  }[approval?.resourceType]
  return {
    label: label || 'Angeforderte Ressource',
    value: approval?.resourceDisplay || 'Ausgewählter Bereich'
  }
}

export function approvalActionDisplay(approval) {
  const payload = approval?.payload || approval?.details || {}
  const chips = {
    read: { label: 'Lesen', color: '#547457', darkColor: '#a4bca8' },
    read_sensitive: { label: 'Vertrauliche Daten lesen', color: '#817047', darkColor: '#c4b286' },
    write: { label: 'Schreiben', color: '#817047', darkColor: '#c4b286' },
    delete: { label: 'Löschen', color: '#996464', darkColor: '#cfa1a1' },
    navigate: { label: 'Navigieren', color: '#6e6e6e', darkColor: '#bbbbbb' }
  }
  const shellCategories = payload.shellCategories
  // A shell inspection can report more categories than this particular grant authorizes.
  const categories = payload.sessionGrants?.length
    ? payload.sessionGrants.map((grant) => grant.category)
    : Array.isArray(shellCategories) && shellCategories.length
      ? shellCategories
      : ['read', 'write'].includes(payload.shellClassification) ? [payload.shellClassification] : []
  if (!categories.length || categories.some((category) => !Object.hasOwn(chips, category))) return null
  const unique = [...new Set(categories.flatMap((category) => category === 'write' ? ['read', 'write'] : [category]))]
  return {
    chips: unique.map((category) => chips[category]),
    incomplete: payload.shellAnalysisComplete === false
  }
}

export function approvalGrantOptions(approval) {
  const payload = approval?.payload || approval?.details || {}
  return (payload.grantOptions || []).map((option) => {
    if (option.id === 'requested' && ['shell_command', 'execution_administrator'].includes(approval.resourceType)
      && approval.actionKind === 'shell_run'
      && (!option.scopeDisplay?.length || approval.resourceType === 'execution_administrator')) {
      return { id: option.id, label: 'Diesen Befehl' }
    }
    if (option.scopeDisplay?.length) {
      return { id: option.id, label: option.scopeDisplay.join(' · ') }
    }
    return { id: option.id, label: option.display?.join(' · ') || option.label }
  })
}

function sanitize(value, depth, seen, key = '', truncate = true) {
  if (value == null || typeof value === 'boolean' || typeof value === 'number') return value
  if (typeof value === 'string') {
    if (URL_KEY.test(key)) return cleanUrlForDisplay(value, truncate ? MAX_STRING_LENGTH : Infinity)
    return truncate && value.length > MAX_STRING_LENGTH
      ? `${value.slice(0, MAX_STRING_LENGTH)}... [gekürzt]`
      : value
  }
  if (typeof value !== 'object') return String(value)
  if (seen.has(value)) return '[Zirkuläre Referenz]'
  if (truncate && depth >= MAX_DEPTH) return '[Weitere Ebenen gekürzt]'
  seen.add(value)
  if (Array.isArray(value)) {
    const items = (truncate ? value.slice(0, MAX_ARRAY_ITEMS) : value)
      .map((item) => sanitize(item, depth + 1, seen, '', truncate))
    if (truncate && value.length > MAX_ARRAY_ITEMS) items.push(`[${value.length - MAX_ARRAY_ITEMS} weitere Einträge]`)
    return items
  }
  const result = {}
  const entries = Object.entries(value)
  for (const [key, item] of truncate ? entries.slice(0, MAX_OBJECT_KEYS) : entries) {
    result[key] = REDACTED_KEY.test(key) ? '[Ausgeblendet]' : sanitize(item, depth + 1, seen, key, truncate)
  }
  if (truncate && entries.length > MAX_OBJECT_KEYS) result._gekürzt = `${entries.length - MAX_OBJECT_KEYS} weitere Felder`
  return result
}

export function permissionGrantDisplay(grant, sessions = []) {
  if (grant?.resourceType === 'session_secret') {
    return { label: 'Session-Variable / Secret', value: grant.resourceDisplay, icon: 'key' }
  }
  if (grant?.resourceType === 'run_action') {
    return { label: grant.command ? 'Bestätigter Shell-Befehl' : 'Bestätigte Aktion', value: grant.resourceDisplay || 'Mit den freigegebenen Werten', icon: grant.command ? 'terminal' : 'verified_user' }
  }
  const resource = approvalResource(grant, sessions)
  const category = {
    'category.read': { label: 'Lesen', icon: 'visibility' },
    'category.read_sensitive': { label: 'Vertrauliche Daten lesen', icon: 'key' },
    'category.write': { label: 'Lesen und Schreiben', icon: 'edit' },
    'category.delete': { label: 'Löschen', icon: 'delete_outline' }
  }[grant?.capability]
  if (category) {
    return {
      ...category,
      label: grant.resourceType === 'shell_action' ? `Exakter Shell-Befehl · ${category.label}`
        : grant.resourceType === 'shell_computer' ? `Shell · ${category.label}`
          : grant.recursive ? `${category.label} · einschließlich Unterpfaden` : category.label,
      value: resource.value
    }
  }
  if (grant?.capability === 'execution.directory') {
    return { label: 'Im Ordnerbaum navigieren', value: resource.value, icon: 'drive_file_move' }
  }
  return {
    label: 'Tool-Zugriff',
    value: resource.value,
    icon: 'verified_user'
  }
}

export function technicalDetailsDisplay(details) {
  if (details == null) return { text: '', fullText: '', truncated: false }
  const formatted = JSON.stringify(sanitize(details, 0, new WeakSet()), null, 2)
  const text = formatted.length > MAX_DETAILS_LENGTH
    ? `${formatted.slice(0, MAX_DETAILS_LENGTH)}\n... [Details gekürzt]`
    : formatted
  const fullText = JSON.stringify(sanitize(details, 0, new WeakSet(), '', false), null, 2)
  return { text, fullText, truncated: text !== fullText }
}
