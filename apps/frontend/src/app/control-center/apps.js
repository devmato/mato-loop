import { defineAsyncComponent } from 'vue'
import {
  controlCenterGlobalSectionManifest,
  controlCenterIntegrationManifest
} from 'src/app/control-center/manifest'

const components = {
  session: defineAsyncComponent(() => import('src/features/chat/components/SessionApp.vue')),
  'agent-coding': defineAsyncComponent(() => import('src/features/coding/AgentCodingApp.vue')),
  git: defineAsyncComponent(() => import('src/features/coding/GitApp.vue')),
  files: defineAsyncComponent(() => import('src/features/files/components/FileExplorerPanel.vue')),
  automation: defineAsyncComponent(() => import('src/features/automations/AutomationApp.vue')),
  'computer-files': defineAsyncComponent(() => import('src/features/files/components/ComputerFileExplorerPanel.vue')),
  'file-viewer': defineAsyncComponent(() => import('src/features/files/components/FileViewerApp.vue')),
  mail: defineAsyncComponent(() => import('src/features/mail/MailApp.vue')),
  'tbs-desk': defineAsyncComponent(() => import('src/features/tbs-desk/TbsDeskApp.vue')),
  coding: defineAsyncComponent(() => import('src/features/coding/CodingApp.vue')),
  terminal: defineAsyncComponent(() => import('src/features/terminal/TerminalApp.vue')),
  browser: defineAsyncComponent(() => import('src/features/browser/BrowserApp.vue')),
  notifications: defineAsyncComponent(() => import('src/features/notifications/NotificationCenterApp.vue'))
}

const footerComponents = {
  session: defineAsyncComponent(() => import('src/features/chat/components/SessionFooter.vue')),
  files: defineAsyncComponent(() => import('src/features/files/components/FileExplorerFooter.vue')),
  'file-viewer': defineAsyncComponent(() => import('src/features/files/components/FileViewerFooter.vue')),
  terminal: defineAsyncComponent(() => import('src/features/terminal/TerminalFooter.vue'))
}

function withComponents(items) {
  return items.map((item) => ({
    ...item,
    component: components[item.id],
    footerComponent: footerComponents[item.id] || null
  }))
}

export const controlCenterIntegrations = Object.freeze(withComponents(controlCenterIntegrationManifest))
export const controlCenterGlobalSections = Object.freeze(withComponents(controlCenterGlobalSectionManifest))
export const controlCenterContents = Object.freeze([
  ...controlCenterIntegrations,
  ...controlCenterGlobalSections
])
export const controlCenterContentsById = new Map(controlCenterContents.map((item) => [item.id, item]))
