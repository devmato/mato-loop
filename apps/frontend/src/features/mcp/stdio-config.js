import { absoluteComputerPath } from 'src/features/files/state'

export function stdioForm(server) {
  return {
    executionAgentId: server?.executionAgentId || null,
    executable: server?.executable || '',
    argsText: JSON.stringify(server?.args || []),
    workingDirectory: server?.workingDirectory || '',
    idleTimeoutSeconds: server?.idleTimeoutSeconds ?? 1800,
    environmentText: JSON.stringify(server?.environment || {}, null, 2),
    secrets: [],
    removeEnvironmentSecrets: []
  }
}

export function stdioPayload(form) {
  return {
    transport: 'stdio',
    executionAgentId: form.executionAgentId,
    executable: normalizedPath(form.executable),
    args: JSON.parse(form.argsText),
    workingDirectory: normalizedPath(form.workingDirectory),
    idleTimeoutSeconds: Number(form.idleTimeoutSeconds),
    environment: JSON.parse(form.environmentText),
    environmentSecrets: Object.fromEntries(form.secrets.map(({ key, value }) => [key.trim(), value])),
    removeEnvironmentSecrets: form.removeEnvironmentSecrets
  }
}

function normalizedPath(value) {
  const path = value.trim()
  return /^[A-Za-z]:[\\/]/u.test(path)
    ? path[0].toUpperCase() + path.slice(1).replaceAll('\\', '/')
    : path
}

export function absolutePathRule(value) {
  const path = typeof value === 'string' ? normalizedPath(value) : ''
  if (path.startsWith('\\\\') || path.startsWith('//')) return 'UNC-Pfade werden nicht unterstützt. Verwende einen absoluten Laufwerkspfad.'
  const windows = /^[A-Z]:\//u.test(path)
  const parts = path.slice(windows ? 3 : 1).split('/')
  return (absoluteComputerPath(path) && path.length <= 4096 && !path.includes('\\')
    && ![...path].some((character) => {
      const code = character.charCodeAt(0)
      return code < 32 || (code >= 127 && code <= 159)
    })
    && (path === '/' || /^[A-Z]:\/$/u.test(path) || parts.every((part) => !['', '.', '..'].includes(part)))
    && (!windows || parts.every((part) => !/[ .]$/u.test(part)
      && !/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$/iu.test(part)
      && !/[<>:"|?*]/u.test(part)))) || 'Gib einen absoluten Pfad auf dem gewählten Computer ein.'
}

export function argsRule(value) {
  try {
    const args = JSON.parse(value)
    if (Array.isArray(args) && args.every((item) => typeof item === 'string' && !item.includes('\0'))) return true
  } catch { /* Report a controlled validation message. */ }
  return 'Gib ein JSON-Array mit Zeichenketten ein, z. B. ["--port", "3000"].'
}

export function environmentRule(value) {
  try {
    const environment = JSON.parse(value)
    if (environment && !Array.isArray(environment) && typeof environment === 'object'
      && Object.entries(environment).every(([key, item]) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) && typeof item === 'string' && !item.includes('\0'))) return true
  } catch { /* Report a controlled validation message. */ }
  return 'Gib ein JSON-Objekt mit Variablennamen und Textwerten ein, z. B. {"LANG": "de_DE.UTF-8"}.'
}
