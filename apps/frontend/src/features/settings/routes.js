export const settingsRoutes = [
  {
    path: 'general', name: 'settings-general', component: () => import('./UserGeneralSettingsPage.vue'),
    meta: { settingsLabel: 'Grundeinstellungen', settingsIcon: 'settings' }
  },
  {
    path: 'personalization', name: 'settings-personalization',
    component: () => import('src/features/personalization/PersonalizationSettingsPage.vue'),
    meta: { settingsLabel: 'Personalisierung', settingsIcon: 'manage_accounts' }
  },
  {
    path: 'providers', name: 'settings-providers', component: () => import('src/features/providers/ProviderSettingsPage.vue'),
    meta: { settingsLabel: 'Provider', settingsIcon: 'dns' }
  },
  { path: 'model-defaults', name: 'settings-model-defaults', component: () => import('./ModelDefaultsPage.vue'), meta: { settingsLabel: 'Standardmodelle', settingsIcon: 'stars' } },
  {
    path: 'execution-agents', name: 'settings-execution-agents', component: () => import('src/features/computers/ExecutionAgentSettingsPage.vue'),
    meta: { settingsLabel: 'Computer', settingsIcon: 'computer' }
  },
  {
    path: 'integrations', name: 'settings-integrations', component: () => import('src/features/integrations/IntegrationSettingsPage.vue'),
    meta: { settingsLabel: 'Integrationen', settingsIcon: 'extension' },
    children: [
      { path: 'new', name: 'settings-integration-create', component: () => import('src/features/integrations/IntegrationEditorPage.vue') },
      { path: ':integrationId/edit', name: 'settings-integration-edit', component: () => import('src/features/integrations/IntegrationEditorPage.vue') },
      { path: ':integrationId/resources', name: 'settings-integration-resources', redirect: (to) => ({ name: 'settings-integration-edit', params: to.params, query: { ...to.query, tab: 'permissions' } }) }
    ]
  },
  {
    path: 'mcp', name: 'settings-mcp', component: () => import('src/features/mcp/McpSettingsPage.vue'),
    meta: { settingsLabel: 'MCP-Server', settingsIcon: 'hub' },
    children: [{ path: ':serverId/tools', name: 'settings-mcp-tools', component: () => import('src/features/integrations/IntegrationAccessPage.vue') }]
  },
  {
    path: 'skills', name: 'settings-skills', component: () => import('src/features/skills/SkillSettingsPage.vue'),
    meta: { settingsLabel: 'Skills', settingsIcon: 'school' },
    children: [
      { path: 'new', name: 'settings-skill-create', component: () => import('src/features/skills/SkillEditorPage.vue') },
      { path: ':skillId', name: 'settings-skill-edit', component: () => import('src/features/skills/SkillEditorPage.vue') }
    ]
  },
  {
    path: 'agents', name: 'settings-agents',
    component: () => import('src/features/agent-profiles/AgentProfileSettingsPage.vue'),
    meta: { settingsLabel: 'Agenten', settingsIcon: 'smart_toy' },
    children: [
      { path: 'new', name: 'settings-agent-create', component: () => import('src/features/agent-profiles/AgentProfileEditorPage.vue') },
      { path: ':agentId', name: 'settings-agent-edit', component: () => import('src/features/agent-profiles/AgentProfileEditorPage.vue') }
    ]
  },
  {
    path: 'session-templates', name: 'settings-session-templates', component: () => import('./SessionTemplatesPage.vue'),
    meta: { settingsLabel: 'Sessionvorlagen', settingsIcon: 'dashboard_customize' }
  },
  {
    path: 'tools', name: 'settings-tools', component: () => import('src/features/settings/ToolSettingsPage.vue'),
    meta: { settingsLabel: 'Tools', settingsIcon: 'build' }
  },
  {
    path: 'memory', name: 'settings-memory', component: () => import('src/features/memory/MemorySettingsPage.vue'),
    meta: { settingsLabel: 'Memory', settingsIcon: 'psychology' }
  },
  {
    path: 'channels', name: 'settings-channels', component: () => import('src/features/channels/ChannelSettingsPage.vue'),
    meta: { settingsLabel: 'Kanäle', settingsIcon: 'forum' }
  },
  {
    path: 'debug-access', name: 'settings-debug-access', component: () => import('src/features/settings/DebugAccessSettingsPage.vue'),
    meta: { settingsLabel: 'Debug-Zugang', settingsIcon: 'bug_report', requiresAdmin: true }
  },
  { path: 'persona', name: 'settings-persona', redirect: { name: 'settings-personalization', query: { tab: 'assistant' } } },
  { path: 'notifications', name: 'settings-notifications', redirect: { name: 'admin-notifications' }, meta: { requiresAdmin: true } },
  { path: 'personal-variables', name: 'settings-personal-variables', redirect: { name: 'personal-variables' } },
  { path: 'approvals', name: 'settings-approvals', redirect: { name: 'chat', query: { controlCenter: 'notifications' } } },
  { path: 'automations', name: 'settings-automations', component: () => import('src/features/automations/AutomationLinkPage.vue') },
  { path: 'automations/new', name: 'settings-automation-create', component: () => import('src/features/automations/AutomationLinkPage.vue') },
  { path: 'automations/:automationId/edit', name: 'settings-automation-edit', redirect: (to) => ({ name: 'settings-automation-detail', params: to.params, query: to.query }) },
  { path: 'automations/:automationId', name: 'settings-automation-detail', component: () => import('src/features/automations/AutomationLinkPage.vue') }
]

export const adminRoutes = [
  {
    path: 'general', name: 'admin-general', component: () => import('src/features/settings/GeneralSettingsPage.vue'),
    meta: { settingsLabel: 'Grundeinstellungen', settingsIcon: 'settings' }
  },
  {
    path: 'appearance', name: 'admin-appearance', component: () => import('./AppearanceSettingsPage.vue'),
    meta: { settingsLabel: 'Darstellung', settingsIcon: 'palette' }
  },
  {
    path: 'notifications', name: 'admin-notifications', component: () => import('src/features/notifications/NotificationSettingsPage.vue'),
    meta: { settingsLabel: 'Benachrichtigungen', settingsIcon: 'notifications_active' }
  },
  {
    path: 'users', name: 'admin-users', component: () => import('src/features/auth/UserSettingsPage.vue'),
    meta: { settingsLabel: 'Benutzer', settingsIcon: 'group' }
  },
  {
    path: 'providers', name: 'admin-providers', component: () => import('src/features/providers/ProviderSettingsPage.vue'),
    props: { admin: true }, meta: { settingsLabel: 'Provider', settingsIcon: 'dns' }
  },
  { path: 'model-defaults', name: 'admin-model-defaults', component: () => import('./ModelDefaultsPage.vue'), props: { admin: true }, meta: { settingsLabel: 'Standardmodelle', settingsIcon: 'stars' } },
  {
    path: 'tools', name: 'admin-tools', component: () => import('src/features/settings/ToolSettingsPage.vue'),
    props: { admin: true }, meta: { settingsLabel: 'Tools', settingsIcon: 'build' }
  },
  {
    path: 'integrations', name: 'admin-integrations', component: () => import('src/features/integrations/IntegrationSettingsPage.vue'),
    props: { admin: true }, meta: { settingsLabel: 'Integrationen', settingsIcon: 'extension' },
    children: [
      { path: 'new', name: 'admin-integration-create', component: () => import('src/features/integrations/IntegrationEditorPage.vue') },
      { path: ':integrationId/edit', name: 'admin-integration-edit', component: () => import('src/features/integrations/IntegrationEditorPage.vue') },
      { path: ':integrationId/resources', name: 'admin-integration-resources', redirect: (to) => ({ name: 'admin-integration-edit', params: to.params, query: { ...to.query, tab: 'permissions' } }) }
    ]
  },
  {
    path: 'mcp', name: 'admin-mcp', component: () => import('src/features/mcp/McpSettingsPage.vue'),
    props: { admin: true }, meta: { settingsLabel: 'MCP-Server', settingsIcon: 'hub' },
    children: [{ path: ':serverId/tools', name: 'admin-mcp-tools', component: () => import('src/features/integrations/IntegrationAccessPage.vue') }]
  },
  {
    path: 'skills', name: 'admin-skills', component: () => import('src/features/skills/SkillSettingsPage.vue'),
    props: { admin: true }, meta: { settingsLabel: 'Skills', settingsIcon: 'school' },
    children: [
      { path: 'new', name: 'admin-skill-create', component: () => import('src/features/skills/SkillEditorPage.vue') },
      { path: ':skillId', name: 'admin-skill-edit', component: () => import('src/features/skills/SkillEditorPage.vue') }
    ]
  },
  {
    path: 'personas', name: 'admin-personas', component: () => import('src/features/personalization/AgentIdentitySettingsPage.vue'),
    props: { admin: true }, meta: { settingsLabel: 'Personas', settingsIcon: 'face' }
  },
  {
    path: 'agents', name: 'admin-agents', component: () => import('src/features/agent-profiles/AgentProfileSettingsPage.vue'),
    props: { admin: true }, meta: { settingsLabel: 'Agenten', settingsIcon: 'smart_toy' },
    children: [
      { path: 'new', name: 'admin-agent-create', component: () => import('src/features/agent-profiles/AgentProfileEditorPage.vue') },
      { path: ':agentId', name: 'admin-agent-edit', component: () => import('src/features/agent-profiles/AgentProfileEditorPage.vue') }
    ]
  },
  {
    path: 'session-templates', name: 'admin-session-templates', component: () => import('./SessionTemplatesPage.vue'),
    props: { admin: true }, meta: { settingsLabel: 'Sessionvorlagen', settingsIcon: 'dashboard_customize' }
  },
  {
    path: 'channels', name: 'admin-channels', component: () => import('src/features/channels/ChannelSettingsPage.vue'),
    props: { admin: true }, meta: { settingsLabel: 'Kanäle', settingsIcon: 'forum' }
  },
  {
    path: 'instance-limits', name: 'admin-instance-limits', component: () => import('src/features/settings/InstanceLimitsPage.vue'),
    meta: { settingsLabel: 'Instanzlimits', settingsIcon: 'tune' }
  }
]
