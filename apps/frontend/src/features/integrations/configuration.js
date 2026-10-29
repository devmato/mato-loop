export const integrationTypes = [
  { label: 'Websuche', value: 'search' },
  { label: 'Webabruf', value: 'webfetch' },
  { label: 'Kalender & Aufgaben', value: 'calendar' },
  { label: 'Mail', value: 'mail' },
  { label: 'Support', value: 'support' },
  { label: 'Wissensdatenbank', value: 'knowledge' },
  { label: 'Rezepte & Essensplanung', value: 'recipes' },
  { label: 'Docker-Verwaltung', value: 'infrastructure' },
  { label: 'Musikbibliothek', value: 'music' },
  { label: '3D-Modellierung', value: 'openscad' },
  { label: 'Programmierung', value: 'coding' },
  { label: 'Browser', value: 'browser' }
]

export const providerDefaults = {
  search: { provider: 'searxng', label: 'SearXNG', placeholder: 'https://search.example.com' },
  webfetch: { provider: 'jina', label: 'Jina Reader', placeholder: 'https://r.jina.ai' },
  calendar: { provider: 'caldav', label: 'CalDAV', placeholder: 'https://calendar.example.com' },
  mail: { provider: 'imap_smtp', label: 'IMAP/SMTP', placeholder: '' },
  support: { provider: 'tbs-desk', label: 'tbs.Desk', placeholder: 'http://192.168.1.30:9000' },
  knowledge: { provider: 'bookstack', label: 'BookStack', placeholder: 'https://wiki.example.com' },
  recipes: { provider: 'mealie', label: 'Mealie', placeholder: 'http://mealie.example.com' },
  infrastructure: { provider: 'arcane', label: 'Arcane', placeholder: 'https://arcane.example.com' },
  music: { provider: 'sonobin', label: 'SonoBin', placeholder: 'https://sonobin.example.com' },
  openscad: { provider: 'openscad', label: 'OpenSCAD', placeholder: '' },
  coding: { provider: 'opencode', label: 'OpenCode', placeholder: 'http://localhost:4096' },
  browser: { provider: 'browser', label: 'Browser', placeholder: '' }
}
