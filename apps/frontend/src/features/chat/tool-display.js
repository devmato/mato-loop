import { technicalDetailsDisplay } from 'src/features/approvals/display'
import { apiErrorMessage, errorDescriptor } from 'src/utils/api-errors'
import { cleanUrlForDisplay } from 'src/shared/url-display'

export const TOOL_SCRIPT = 'run_tool_script'

const TOOL_DISPLAY = {
  task: { label: 'Subagent starten', icon: 'account_tree' },
  question: { label: 'Rückfrage stellen', icon: 'help_outline' },
  read: { label: 'Datei lesen', icon: 'description' },
  glob: { label: 'Dateien suchen', icon: 'folder_open' },
  grep: { label: 'Dateiinhalte durchsuchen', icon: 'manage_search' },
  list_directory: { label: 'Ordner auflisten', icon: 'folder_open' },
  read_file: { label: 'Datei lesen', icon: 'description' },
  provide_file: { label: 'Datei bereitstellen', icon: 'download' },
  write_file: { label: 'Datei schreiben', icon: 'edit_document' },
  edit_file: { label: 'Datei bearbeiten', icon: 'edit_note' },
  create_directory: { label: 'Ordner erstellen', icon: 'create_new_folder' },
  delete_path: { label: 'Pfad löschen', icon: 'delete_outline' },
  copy_path: { label: 'Pfad kopieren', icon: 'content_copy' },
  move_path: { label: 'Pfad verschieben', icon: 'drive_file_move' },
  web_search: { label: 'Websuche', icon: 'travel_explore' },
  webfetch: { label: 'Webseite abrufen', icon: 'language' },
  fetch_transcript: { label: 'Untertitel abrufen', icon: 'subtitles' },
  generate_qr_code: { label: 'QR-Code erzeugen', icon: 'qr_code_2' },
  read_qr_code: { label: 'QR-Code lesen', icon: 'qr_code_scanner' },
  generate_image: { label: 'Bild erzeugen', icon: 'image' },
  edit_image: { label: 'Bild bearbeiten', icon: 'auto_fix_high' },
  generate_speech: { label: 'Sprache erzeugen', icon: 'record_voice_over' },
  generate_audio: { label: 'Audio erzeugen', icon: 'graphic_eq' },
  transcribe_audio: { label: 'Audio transkribieren', icon: 'mic' },
  analyze_video: { label: 'Video analysieren', icon: 'video_file' },
  analyze_audio: { label: 'Audio analysieren', icon: 'audio_file' },
  calendar_list_accounts: { label: 'Kalenderkonten auflisten', icon: 'account_circle' },
  calendar_list_calendars: { label: 'Kalender auflisten', icon: 'calendar_month' },
  calendar_query_events: { label: 'Termine lesen', icon: 'event' },
  calendar_create_event: { label: 'Termin erstellen', icon: 'event_available' },
  calendar_update_event: { label: 'Termin ändern', icon: 'edit_calendar' },
  calendar_delete_event: { label: 'Termin löschen', icon: 'event_busy' },
  tasks_list_accounts: { label: 'Aufgabenkonten auflisten', icon: 'account_circle' },
  tasks_list_lists: { label: 'Aufgabenlisten auflisten', icon: 'checklist' },
  tasks_query: { label: 'Aufgaben lesen', icon: 'task_alt' },
  tasks_create: { label: 'Aufgabe erstellen', icon: 'add_task' },
  tasks_create_many: { label: 'Fehlende Aufgaben erstellen', icon: 'playlist_add' },
  tasks_update: { label: 'Aufgabe ändern', icon: 'edit_note' },
  tasks_set_completed: { label: 'Aufgabenstatus ändern', icon: 'task_alt' },
  tasks_delete: { label: 'Aufgabe löschen', icon: 'delete_outline' },
  mail_list_accounts: { label: 'Mail-Konten auflisten', icon: 'account_circle' },
  mail_list_folders: { label: 'Mail-Ordner auflisten', icon: 'folder_open' },
  mail_search: { label: 'E-Mails suchen', icon: 'manage_search' },
  mail_get_message: { label: 'E-Mail lesen', icon: 'mail' },
  mail_create_draft: { label: 'Entwurf erstellen', icon: 'drafts' },
  mail_update_draft: { label: 'Entwurf ändern', icon: 'edit_note' },
  mail_delete_draft: { label: 'Entwurf löschen', icon: 'delete_outline' },
  mail_send_draft: { label: 'Entwurf senden', icon: 'send' },
  mail_send_message: { label: 'E-Mail senden', icon: 'send' },
  mail_delete_message: { label: 'E-Mail löschen', icon: 'delete_outline' },
  mealie_list_accounts: { label: 'Mealie-Konten auflisten', icon: 'restaurant' },
  recipes_search: { label: 'Rezepte suchen', icon: 'search' },
  recipe_get: { label: 'Rezept lesen', icon: 'menu_book' },
  recipe_create: { label: 'Rezept erstellen', icon: 'post_add' },
  recipe_update: { label: 'Rezept ändern', icon: 'edit_note' },
  recipe_delete: { label: 'Rezept löschen', icon: 'delete' },
  recipe_import_url: { label: 'Rezept aus URL importieren', icon: 'download' },
  meal_plan_get: { label: 'Essensplan lesen', icon: 'calendar_month' },
  meal_plan_add: { label: 'Essensplan-Eintrag erstellen', icon: 'event_available' },
  meal_plan_update: { label: 'Essensplan-Eintrag ändern', icon: 'edit_calendar' },
  meal_plan_delete: { label: 'Essensplan-Eintrag löschen', icon: 'event_busy' },
  session_read: { label: 'Session lesen', icon: 'forum' },
  session_tool_result_read: { label: 'Tool-Ergebnis lesen', icon: 'history' },
  session_browse: { label: 'Letzte Sessions anzeigen', icon: 'view_list' },
  session_search: { label: 'Sessions durchsuchen', icon: 'manage_search' },
  memory_retrieve: { label: 'Memory abrufen', icon: 'psychology' },
  context_search: { label: 'Kontext durchsuchen', icon: 'manage_search' },
  memory_browse: { label: 'Memories auflisten', icon: 'psychology' },
  memory_write: { label: 'Memory speichern', icon: 'save' },
  update_agent_identity: { label: 'Persona ändern', icon: 'face' },
  update_user_profile: { label: 'Persönliches Profil ändern', icon: 'manage_accounts' },
  variable_read: { label: 'Persönliche Variable lesen', icon: 'key' },
  variable_write: { label: 'Persönliche Variable speichern', icon: 'save' },
  secret_input: { label: 'Secret einmalig abfragen', icon: 'password' },
  process_exec: { label: 'Prozess ausführen', icon: 'terminal' },
  'process.exec': { label: 'Prozess ausführen', icon: 'terminal' },
  shell_run: { label: 'Shell-Befehl ausführen', icon: 'terminal' },
  'shell.run': { label: 'Shell-Befehl ausführen', icon: 'terminal' },
  list_automations: { label: 'Automationen anzeigen', icon: 'view_list' },
  get_automation: { label: 'Automation anzeigen', icon: 'schedule' },
  create_automation: { label: 'Automation erstellen', icon: 'add_task' },
  edit_automation: { label: 'Automation ändern', icon: 'edit_calendar' },
  run_automation: { label: 'Automation starten', icon: 'play_arrow' },
  delete_automation: { label: 'Automation löschen', icon: 'delete_forever' }
}

const BROWSER_LABELS = {
  browser: 'Browser verwenden',
  browser_tabs: 'Browser-Tabs',
  browser_navigate: 'Browser-Navigation',
  browser_snapshot: 'Browser-Snapshot',
  browser_take_screenshot: 'Browser-Screenshot',
  browser_click: 'Browser-Klick',
  browser_type: 'Browser-Texteingabe',
  browser_fill_form: 'Browser-Formular',
  browser_press_key: 'Browser-Tastendruck',
  browser_scroll: 'Browser-Scrollen',
  browser_diagnostics: 'Browser-Diagnose',
  browser_evaluate: 'Browser-JavaScript',
  browser_run_code: 'Browser-Python',
  browser_request_control: 'Browser-Steuerung anfordern',
  browser_release_control: 'Browser-Steuerung freigeben'
}

const GROUP_ICONS = {
  filesystem: 'folder',
  process: 'terminal',
  web: 'language',
  calendar: 'calendar_month',
  tasks: 'checklist',
  mail: 'mail',
  media: 'perm_media',
  sessions: 'forum',
  automations: 'schedule',
  memory: 'psychology',
  agent: 'smart_toy',
  coding: 'code',
  integrations: 'extension'
}
const APPROVAL_PRESENTATIONS = {
  shell_run: 'shell',
  'shell.run': 'shell'
}

function truncate(value, maxLength = 140) {
  const text = String(value || '').trim().replace(/\s+/g, ' ')
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text
}

function displayText(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : ''
}

function validToolDisplay(value) {
  const group = displayText(value?.group)
  const label = displayText(value?.label)
  return Boolean(
    group
    && GROUP_ICONS[group] === value?.icon
    && label.length <= 160
    && (value.summary === undefined || (typeof value.summary === 'string' && value.summary.length <= 140))
  )
}

export function toolIdentity(tool) {
  const name = typeof tool === 'string' ? tool : tool?.name
  if (name === TOOL_SCRIPT) return { label: 'Skriptausführung', icon: 'code' }
  const supplied = validToolDisplay(tool?.toolDisplay) ? tool.toolDisplay : null
  const group = supplied?.group || (name === 'browser' || name?.startsWith('browser_') ? 'web' : '')
  const fallback = TOOL_DISPLAY[name]
  return {
    label: displayText(supplied?.label) || fallback?.label || BROWSER_LABELS[name] || 'Tool ausführen',
    icon: GROUP_ICONS[group] || fallback?.icon || 'build'
  }
}

export function toolActivityLabel(tool) {
  return toolIdentity(tool).label
}

export function memoryWriteContent(tool) {
  if (tool?.name !== 'memory_write') return ''
  const content = tool?.arguments?.content
  if (typeof content === 'string') return content
  if (typeof content?.preview !== 'string') return ''
  return Number(content.characters) > content.preview.length
    ? `${content.preview}\n... [älterer Verlauf gekürzt]`
    : content.preview
}

export function shellCommand(tool) {
  if (!['shell_run', 'shell.run'].includes(tool?.name)) return ''
  return typeof tool?.arguments?.command === 'string' ? tool.arguments.command : ''
}

export function approvalPresentationKind(tool) {
  return shellCommand(tool) ? APPROVAL_PRESENTATIONS[tool?.name] || null : null
}

function taskDisplayItems(tool) {
  const args = tool?.arguments || {}
  const has = (key) => Object.prototype.hasOwnProperty.call(args, key)
  if (tool?.name === 'tasks_query') {
    return [
      { label: 'Aktion', value: 'Aufgaben lesen', format: 'text' },
      { label: 'Anzahl', value: `Bis zu ${args.limit || 100}`, format: 'text' },
      {
        label: 'Status',
        value: args.include_completed ? 'Offene und erledigte Aufgaben' : 'Nur offene Aufgaben',
        format: 'text'
      }
    ]
  }
  if (tool?.name === 'tasks_create') {
    return [
      { label: 'Aktion', value: 'Aufgabe erstellen', format: 'text' },
      ...(typeof args.title === 'string'
        ? [{ label: 'Aufgabe', value: args.title, format: 'text' }]
        : []),
      { label: 'Fälligkeit', value: args.due || 'Keine Fälligkeit', format: 'text' },
      ...(typeof args.description === 'string' && args.description
        ? [{ label: 'Notiz', value: args.description, format: 'text' }]
        : [])
    ]
  }
  if (tool?.name === 'tasks_create_many') {
    return [
      { label: 'Aktion', value: 'Fehlende Aufgaben erstellen', format: 'text' },
      {
        label: 'Aufgaben',
        value: Array.isArray(args.tasks) ? args.tasks.map((item) => item?.title).filter(Boolean) : [],
        format: 'list'
      }
    ]
  }
  if (tool?.name === 'tasks_update') {
    const changes = [
      ...(has('title') ? [`Titel: ${args.title}`] : []),
      ...(has('description') ? [args.description ? `Notiz: ${args.description}` : 'Notiz entfernen'] : []),
      ...(has('due') ? [args.due ? `Fälligkeit: ${args.due}` : 'Fälligkeit entfernen'] : [])
    ]
    return [
      { label: 'Aktion', value: 'Aufgabe ändern', format: 'text' },
      ...(changes.length ? [{ label: 'Änderungen', value: changes, format: 'list' }] : [])
    ]
  }
  if (tool?.name === 'tasks_set_completed') {
    return [
      { label: 'Aktion', value: 'Aufgabenstatus ändern', format: 'text' },
      { label: 'Neuer Status', value: args.completed ? 'Erledigt' : 'Offen', format: 'text' }
    ]
  }
  if (tool?.name === 'tasks_delete') {
    return [{ label: 'Aktion', value: 'Aufgabe löschen', format: 'text' }]
  }
  return []
}

const PROFILE_FIELD_LABELS = {
  full_name: 'Vollständiger Name',
  preferred_name: 'Bevorzugter Name',
  form_of_address: 'Anrede',
  pronouns: 'Pronomen',
  primary_language: 'Bevorzugte Sprache',
  primary_residence: 'Hauptwohnsitz'
}

const PROFILE_SCOPE_LABELS = {
  current_model: 'Aktueller Modelltyp',
  private_models: 'Private Modelle',
  public_models: 'Öffentliche Modelle',
  both_models: 'Private und öffentliche Modelle'
}

function profileDisplayItems(tool) {
  const args = tool?.name === 'customize_matoloop'
    ? { ...tool.arguments.changes, model_scope: tool.arguments.model_scope }
    : tool?.arguments || {}
  const changes = Object.entries(PROFILE_FIELD_LABELS)
    .filter(([field]) => Object.prototype.hasOwnProperty.call(args, field))
    .map(([field, label]) => ({
      label,
      value: args[field] == null ? 'Entfernen' : String(args[field]),
      format: 'text'
    }))
  return [
    {
      label: 'Gilt für',
      value: PROFILE_SCOPE_LABELS[args.model_scope] || PROFILE_SCOPE_LABELS.current_model,
      format: 'text'
    },
    ...changes
  ]
}

function normalizeDisplayItems(items) {
  if (!Array.isArray(items)) return []
  return items.flatMap((item) => {
    if (!item || typeof item.label !== 'string' || !['text', 'code', 'list'].includes(item.format)) return []
    const value = item.format === 'list'
      ? Array.isArray(item.value) ? item.value.map(String) : []
      : typeof item.value === 'string' ? item.value : ''
    return item.format === 'list' || value.length
      ? [{ label: item.label, value, format: item.format }]
      : []
  })
}

export function toolPresentation(tool, displayItems = []) {
  const suppliedItems = normalizeDisplayItems(displayItems)
    .filter((item) => item.label !== 'Für diese Session')
  const memory = memoryWriteContent(tool)
  if (memory && suppliedItems.every((item) => item.label === 'Berücksichtigung')) {
    return {
      summary: toolSummary(tool),
      items: [{ label: 'Memory-Inhalt', value: memory, format: 'code' }, ...suppliedItems]
    }
  }
  if (suppliedItems.length) return { summary: toolSummary(tool), items: suppliedItems }
  if (tool?.name === TOOL_SCRIPT) return { summary: '', items: [] }

  if (tool?.name === 'update_user_profile' || (tool?.name === 'customize_matoloop'
    && tool.arguments?.area === 'user_profile' && tool.arguments?.action === 'update')) {
    return { summary: toolSummary(tool), items: profileDisplayItems(tool) }
  }

  const command = shellCommand(tool)
  if (command) {
    return {
      summary: toolSummary(tool),
      items: [{ label: 'Shell-Befehl', value: command, format: 'code' }]
    }
  }
  if (['process_exec', 'process.exec'].includes(tool?.name)) {
    const args = tool?.arguments || {}
    return {
      summary: toolSummary(tool),
      items: [
        ...(typeof args.program === 'string'
          ? [{ label: 'Programm', value: args.program, format: 'code' }]
          : []),
        { label: 'Parameter', value: Array.isArray(args.args) ? args.args.map(String) : [], format: 'list' }
      ]
    }
  }
  const taskItems = taskDisplayItems(tool)
  const args = tool?.arguments || {}
  return {
    summary: toolSummary(tool),
    items: taskItems.length ? taskItems : [
      ['query', 'Suchbegriff'], ['queries', 'Suchbegriffe'], ['pattern', 'Suchmuster'],
      ['path', 'Pfad'], ['source_path', 'Quelle'], ['destination_path', 'Ziel'], ['url', 'Adresse'],
      ['title', 'Titel'], ['name', 'Name'], ['prompt', 'Auftrag'], ['text', 'Text'], ['brief', 'Auftrag'],
      ['to', 'An'], ['cc', 'Cc'], ['bcc', 'Bcc'], ['subject', 'Betreff'], ['body', 'Nachricht'],
      ['markdown', 'Inhalt'], ['content', 'Inhalt'], ['patchText', 'Patch'],
      ['display_name', 'Agent-Name'], ['instructions', 'Persona-Anweisungen'],
      ['code', 'Python-Code'], ['function', 'JavaScript'], ['element', 'Element']
    ].flatMap(([key, label]) => {
      let value = args[key]?.preview ?? args[key]
      if (Array.isArray(value)) {
        value = value.filter((item) => typeof item === 'string')
        return value.length || args[key].length === 0 ? [{ label, value, format: 'list' }] : []
      }
      if (typeof value !== 'string') return []
      if (!value) value = '(leer)'
      if (args[key]?.characters > value.length) value += '\n[Vorschau gekürzt]'
      return [{ label, value: key === 'url' ? cleanUrlForDisplay(value) : value,
        format: ['code', 'function', 'instructions', 'body', 'markdown', 'content', 'patchText'].includes(key) ? 'code' : 'text' }]
    })
  }
}

export function toolSummary(tool) {
  if (tool?.name === TOOL_SCRIPT) return ''
  const suppliedDisplay = validToolDisplay(tool?.toolDisplay)
  if (suppliedDisplay && tool.toolDisplay.summary !== undefined) return displayText(tool.toolDisplay.summary)
  const argumentsValue = tool?.arguments
  const fallback = suppliedDisplay || TOOL_DISPLAY[tool?.name] || BROWSER_LABELS[tool?.name] ? '' : tool?.name || ''
  if (!argumentsValue || typeof argumentsValue !== 'object') return fallback
  if (tool?.name === 'update_user_profile') {
    const count = Object.keys(PROFILE_FIELD_LABELS)
      .filter((field) => Object.prototype.hasOwnProperty.call(argumentsValue, field)).length
    return `${count} ${count === 1 ? 'Profilfeld' : 'Profilfelder'}`
  }
  if (tool?.name === 'question') {
    return truncate(argumentsValue.questions?.find((item) => typeof item?.question === 'string')?.question)
  }
  if (tool?.name === 'memory_browse' || (tool?.name === 'memory_retrieve' && argumentsValue.mode === 'browse')) {
    return argumentsValue.cursor_id ? 'Memory-Übersicht fortsetzen' : 'Memory-Übersicht'
  }
  if (tool?.name === 'session_browse') return 'Letzte Sessions'
  if (tool?.name === 'list_automations') return 'Eigene Automationen'
  if (['get_automation', 'run_automation', 'delete_automation'].includes(tool?.name)) {
    return argumentsValue.automation_id ? `Automation ${truncate(argumentsValue.automation_id, 36)}` : ''
  }
  if (['create_automation', 'edit_automation'].includes(tool?.name)) {
    return truncate(argumentsValue.name || argumentsValue.automation_id || '')
  }
  if (['shell_run', 'shell.run'].includes(tool?.name)) {
    return truncate(argumentsValue.command)
  }
  if (['process_exec', 'process.exec'].includes(tool?.name)) {
    const program = typeof argumentsValue.program === 'string' ? argumentsValue.program : ''
    const args = Array.isArray(argumentsValue.args) ? argumentsValue.args.join(', ') : ''
    return truncate([program, args].filter(Boolean).join(' · '))
  }
  if (tool?.name === 'tasks_create' && typeof argumentsValue.title === 'string') {
    return truncate(argumentsValue.title)
  }
  if (tool?.name === 'tasks_create_many' && Array.isArray(argumentsValue.tasks)) {
    return `${argumentsValue.tasks.length} Aufgaben`
  }
  if (tool?.name === 'meal_plan_get') {
    return truncate([argumentsValue.start_date, argumentsValue.end_date].filter(Boolean).join(' – '))
  }
  if (tool?.name === 'meal_plan_add') {
    return truncate(argumentsValue.title || argumentsValue.text || argumentsValue.date || 'Eintrag erstellen')
  }
  if (tool?.name === 'meal_plan_update') return truncate(argumentsValue.title || 'Eintrag ändern')
  if (tool?.name === 'meal_plan_delete') return 'Eintrag löschen'
  if (tool?.name === 'tasks_query') {
    return argumentsValue.include_completed ? 'Offene und erledigte Aufgaben' : 'Offene Aufgaben'
  }
  if (tool?.name === 'tasks_update') {
    return typeof argumentsValue.title === 'string' ? truncate(argumentsValue.title) : 'Aufgabe ändern'
  }
  if (tool?.name === 'tasks_set_completed') {
    return argumentsValue.completed ? 'Als erledigt markieren' : 'Als offen markieren'
  }
  if (tool?.name === 'tasks_delete') return 'Aufgabe löschen'
  if (tool?.name === 'generate_qr_code') return 'QR-Code'
  if (tool?.name === 'read_qr_code' && typeof argumentsValue.attachment_id === 'string') {
    return `Bildanhang ${truncate(argumentsValue.attachment_id, 36)}`
  }
  if (tool?.name === 'generate_speech' && typeof argumentsValue.text === 'string') {
    return truncate(argumentsValue.text)
  }
  if (['generate_image', 'edit_image', 'generate_audio'].includes(tool?.name) && typeof argumentsValue.prompt === 'string') {
    const duration = Number(argumentsValue.duration_seconds) > 0
      ? ` (${argumentsValue.duration_seconds} s)`
      : ''
    return `${truncate(argumentsValue.prompt)}${duration}`
  }
  if (tool?.name === 'transcribe_audio' && typeof argumentsValue.attachment_id === 'string') {
    return `Audioanhang ${truncate(argumentsValue.attachment_id, 36)}`
  }
  if (tool?.name === 'analyze_video' && typeof argumentsValue.path === 'string') {
    const root = argumentsValue.root ? `${argumentsValue.root}:` : ''
    return truncate(`${root}${argumentsValue.path}`)
  }
  if (tool?.name === 'session_tool_result_read' && typeof argumentsValue.message_id === 'string') {
    const offset = Number(argumentsValue.offset) || 0
    const suffix = offset > 0 ? `, ab Zeichen ${offset}` : ''
    return `Nachricht ${truncate(argumentsValue.message_id, 36)}${suffix}`
  }
  if (tool?.name === 'session_read') {
    return typeof argumentsValue.session_id === 'string'
      ? `Session ${truncate(argumentsValue.session_id, 36)}`
      : 'Aktuelle Session'
  }
  if (tool?.name === 'calendar_query_events') {
    return truncate([argumentsValue.start, argumentsValue.end].filter(Boolean).join(' – '))
  }
  if (['calendar_create_event', 'calendar_update_event'].includes(tool?.name)) {
    if (typeof argumentsValue.summary === 'string') return truncate(argumentsValue.summary)
    return truncate([argumentsValue.timing?.start, argumentsValue.timing?.end].filter(Boolean).join(' – '))
  }
  if (['read', 'glob', 'grep'].includes(tool?.name)) {
    const location = [argumentsValue.root, argumentsValue.path]
      .filter((value) => typeof value === 'string' && value)
      .join(':')
    const pattern = typeof argumentsValue.pattern === 'string' ? argumentsValue.pattern : ''
    return truncate([location, pattern].filter(Boolean).join(' · '))
  }
  if (tool?.name === 'memory_write') return truncate(memoryWriteContent(tool))
  if (tool?.name === 'update_agent_identity') {
    if (typeof argumentsValue.display_name === 'string') {
      return `Agent-Name: ${truncate(argumentsValue.display_name, 80)}`
    }
    return 'Verhalten und Charakter aktualisieren'
  }
  if (typeof argumentsValue.path === 'string') {
    const root = typeof argumentsValue.root === 'string' ? `${argumentsValue.root}:` : ''
    return truncate(`${root}${argumentsValue.path}`)
  }
  if (typeof argumentsValue.source_path === 'string' && typeof argumentsValue.destination_path === 'string') {
    const sourceRoot = argumentsValue.source_root ? `${argumentsValue.source_root}:` : ''
    const destinationRoot = argumentsValue.destination_root ? `${argumentsValue.destination_root}:` : ''
    return truncate(`${sourceRoot}${argumentsValue.source_path} -> ${destinationRoot}${argumentsValue.destination_path}`)
  }
  if (typeof argumentsValue.url === 'string') return cleanUrlForDisplay(argumentsValue.url)
  if (typeof argumentsValue.query === 'string') return truncate(argumentsValue.query)
  if (Array.isArray(argumentsValue.queries)) {
    const queries = argumentsValue.queries.filter((value) => typeof value === 'string' && value)
    if (queries.length) return truncate(queries.join(' · '))
  }
  if (typeof argumentsValue.name === 'string') return truncate(argumentsValue.name)
  if (typeof argumentsValue.sessionName === 'string') return truncate(argumentsValue.sessionName)
  return fallback
}

export function toolArgumentsDisplay(argumentsValue, toolName = null) {
  if (toolName === 'memory_write' && argumentsValue && typeof argumentsValue === 'object') {
    const metadata = { ...argumentsValue }
    delete metadata.content
    return technicalDetailsDisplay(metadata)
  }
  return technicalDetailsDisplay(argumentsValue)
}

function localizePlainToolResult(content, toolName) {
  const mediaResults = {
    generate_qr_code: ['QR code generated.', 'QR-Code erzeugt.'],
    generate_image: ['Image generated.', 'Bild erzeugt.'],
    generate_speech: ['Speech generated.', 'Sprachaufnahme erzeugt.'],
    generate_audio: ['Audio generated.', 'Audio erzeugt.']
  }
  const mediaResult = mediaResults[toolName]
  let text = mediaResult && content.startsWith(mediaResult[0])
    ? `${mediaResult[1]}${content.slice(mediaResult[0].length)}`
    : content
  if (['read', 'read_file'].includes(toolName)) {
    text = text
      .replace(/\n\n\[Lines ([\d, ]+) were truncated after 2000 characters\.\](?=\n|$)/,
        '\n\n[Zeilen $1 wurden nach 2000 Zeichen gekürzt.]')
      .replace(/\n\[More lines are available\. Continue with offset=(\d+)\.\]$/,
        '\n[Weitere Zeilen verfügbar. Mit offset=$1 weiterlesen.]')
  }
  const imageMarker = '\n\n[Image saved but not sent to the current text-only model.]'
  return ['generate_image', 'webfetch'].includes(toolName) && text.endsWith(imageMarker)
    ? `${text.slice(0, -imageMarker.length)}\n\n[Bild gespeichert, aber nicht an das aktuelle Textmodell übertragen.]`
    : text
}

export function parseToolResult(content, toolName = null, message = null) {
  if (typeof content !== 'string') return { text: '', error: null }
  if (message?.contentTruncated) {
    const error = errorDescriptor(message.resultSummary?.error)
    return {
      text: content,
      fullText: content,
      textTruncated: true,
      error,
      errorMessage: error ? apiErrorMessage(error, 'Die Tool-Aktion ist fehlgeschlagen.') : null,
      summary: message.resultSummary?.summary || ''
    }
  }
  try {
    const value = JSON.parse(content)
    const error = value?.isError === true ? { code: 'mcp.callFailed' } : errorDescriptor(value)
    const processValue = value?.output && typeof value.output === 'object' ? value.output : value
    const hasProcessOutput = typeof processValue?.stdout === 'string'
      || typeof processValue?.stderr === 'string'
    const processMetadata = hasProcessOutput ? { ...value } : null
    if (processMetadata) {
      if (processMetadata.output === processValue) delete processMetadata.output
      else {
        delete processMetadata.stdout
        delete processMetadata.stderr
        delete processMetadata.truncated
      }
    }
    const processMetadataDisplay = processMetadata && Object.keys(processMetadata).length
      ? technicalDetailsDisplay(processMetadata)
      : null
    const memoryValue = ['memory_retrieve', 'memory_browse'].includes(toolName) && Array.isArray(value?.items)
      ? {
          mode: value.mode,
          strategy: value.strategy,
          scope: value.scope,
          total: value.total,
          hasMore: value.hasMore,
          nextCursor: value.nextCursor,
          items: value.items.slice(0, 20).map((item) => ({
            id: item.id,
            category: item.category,
            pinned: item.pinned,
            audience: item.audience,
            memory: item.content
          }))
        }
      : null
    const display = memoryValue
      ? technicalDetailsDisplay(memoryValue)
      : processMetadata
        ? processMetadataDisplay || { text: '', fullText: '', truncated: false }
        : technicalDetailsDisplay(value)
    const summary = toolName === 'session_read' ? displayText(value?.session?.title) : ''
    return {
      text: display.text,
      fullText: display.fullText,
      textTruncated: display.truncated,
      error,
      errorMessage: error ? apiErrorMessage(error, 'Die Tool-Aktion ist fehlgeschlagen.') : null,
      ...(summary ? { summary: truncate(summary) } : {}),
      ...(hasProcessOutput
        ? {
            stdout: processValue.stdout || '',
            stderr: processValue.stderr || '',
            truncated: processValue.truncated === true
          }
        : {})
    }
  } catch {
    const localized = localizePlainToolResult(content, toolName)
    const text = localized.length > 5000 ? `${localized.slice(0, 5000)}\n... [Ergebnis gekürzt]` : localized
    return {
      text,
      fullText: localized,
      textTruncated: text !== localized,
      error: null,
      errorMessage: null
    }
  }
}

export function hiddenToolScript({ tool, result, approval, question }) {
  return tool?.name === TOOL_SCRIPT && !approval && !question
    && !parseToolResult(result?.content, TOOL_SCRIPT, result).error
}

export function toolStatus({ result, approval, question, active }) {
  if (approval) {
    return { name: 'approval', label: 'Freigabe erforderlich', icon: 'pending_actions', color: 'warning' }
  }
  if (question) {
    return { name: 'question', label: 'Antwort erforderlich', icon: 'help_outline', color: 'primary' }
  }
  if (result) {
    const { error } = parseToolResult(result.content, null, result)
    if (error?.code === 'permission.denied') {
      return { name: 'denied', label: 'Abgelehnt', icon: 'block', color: 'negative' }
    }
    if (error?.code === 'tool.executionInterrupted') {
      return { name: 'interrupted', label: 'Unterbrochen', icon: 'stop_circle', color: 'warning' }
    }
    if (error) return { name: 'failed', label: 'Fehlgeschlagen', icon: 'error', color: 'negative' }
    return { name: 'completed', label: 'Erledigt', icon: 'check_circle', color: 'positive' }
  }
  if (active) return { name: 'running', label: 'Wird ausgeführt', icon: null, color: 'primary' }
  return { name: 'pending', label: 'Ausstehend', icon: 'schedule', color: 'grey-7' }
}
