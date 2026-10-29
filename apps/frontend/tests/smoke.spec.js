import { describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import routes, { adminRoutes, settingsRoutes } from '../src/router/routes'

describe('MatoLoop frontend scaffold', () => {
  it('uses chat as Home and preserves dashboard and chat deep links', async () => {
    const children = routes.find((route) => Array.isArray(route.children)).children
    const router = createRouter({ history: createMemoryHistory(), routes: children
      .filter((route) => route.name === 'chat' || route.path === 'dashboard')
      .map((route) => ({ ...route, path: `/${route.path}`, component: { render: () => null } })) })
    await router.push('/')
    expect(router.currentRoute.value.name).toBe('chat')
    await router.push('/?session=session-1&project=project-1&controlCenter=files')
    expect(router.currentRoute.value.name).toBe('chat')
    expect(router.currentRoute.value.query).toEqual({ session: 'session-1', project: 'project-1', controlCenter: 'files' })
    await router.push('/chat?session=session-2')
    expect(router.currentRoute.value.query.session).toBe('session-2')
    await router.push('/dashboard?project=project-1')
    expect(router.currentRoute.value.name).toBe('chat')
    expect(router.currentRoute.value.query).toEqual({ project: 'project-1' })
    await router.push({ name: 'chat' })
    expect(router.currentRoute.value.query).toEqual({})
  })

  it('routes legacy automation editing and exact-run links to the shared page', async () => {
    const component = { render: () => null }
    const router = createRouter({ history: createMemoryHistory(), routes: settingsRoutes
      .filter((route) => route.path.startsWith('automations'))
      .map((route) => ({ ...route, path: `/settings/${route.path}`, component })) })
    expect(router.resolve('/settings/automations/new').name).toBe('settings-automation-create')
    await router.push('/settings/automations/automation-1/edit?run=run-1')
    expect(router.currentRoute.value.name).toBe('settings-automation-detail')
    expect(router.currentRoute.value.path).toBe('/settings/automations/automation-1')
    expect(router.currentRoute.value.query).toEqual({ run: 'run-1' })
  })

  it('exposes chat and the complete settings navigation', () => {
    const appRoute = routes.find((route) => Array.isArray(route.children))
    const routeNames = appRoute.children.map((route) => route.name)
    const settingsRoute = appRoute.children.find((route) => route.name === 'settings')

    expect(routeNames).toEqual(['chat', undefined, 'personal-variables', 'archive', 'settings', 'admin'])
    expect(settingsRoute.children.map((route) => route.name)).toEqual([
      'settings-general',
      'settings-personalization',
      'settings-providers',
      'settings-model-defaults',
      'settings-execution-agents',
      'settings-integrations',
      'settings-mcp',
      'settings-skills',
      'settings-agents',
      'settings-session-templates',
      'settings-tools',
      'settings-memory',
      'settings-channels',
      'settings-debug-access',
      'settings-persona',
      'settings-notifications',
      'settings-personal-variables',
      'settings-approvals',
      'settings-automations',
      'settings-automation-create',
      'settings-automation-edit',
      'settings-automation-detail'
    ])
    expect(
      settingsRoute.children.find((route) => route.name === 'settings-integrations').children
        .map((route) => route.name)
    ).toEqual(['settings-integration-create', 'settings-integration-edit', 'settings-integration-resources'])
    expect(
      settingsRoute.children.find((route) => route.name === 'settings-mcp').children
        .map((route) => ({ name: route.name, path: route.path }))
    ).toEqual([{ name: 'settings-mcp-tools', path: ':serverId/tools' }])
    expect(
      settingsRoute.children.find((route) => route.name === 'settings-agents').children
        .map((route) => route.name)
    ).toEqual(['settings-agent-create', 'settings-agent-edit'])
    expect(
      settingsRoute.children.filter((route) => route.path.startsWith('automations/'))
        .map(({ name, path }) => ({ name, path }))
    ).toEqual([
      { name: 'settings-automation-create', path: 'automations/new' },
      { name: 'settings-automation-edit', path: 'automations/:automationId/edit' },
      { name: 'settings-automation-detail', path: 'automations/:automationId' }
    ])
    expect(routes.find((route) => route.name === 'markdown-print')?.path).toBe('/print/markdown')
    expect(appRoute.children.find((route) => route.name === 'admin')?.meta.requiresAdmin).toBe(true)
    expect(adminRoutes.map((route) => route.name)).toEqual(['admin-general', 'admin-appearance', 'admin-notifications', 'admin-users', 'admin-providers', 'admin-model-defaults', 'admin-tools', 'admin-integrations', 'admin-mcp', 'admin-skills', 'admin-personas', 'admin-agents', 'admin-session-templates', 'admin-channels', 'admin-instance-limits'])
    expect(settingsRoutes.find((route) => route.name === 'settings-notifications')?.meta.requiresAdmin).toBe(true)
    expect(settingsRoutes.find((route) => route.name === 'settings-debug-access')?.meta.requiresAdmin).toBe(true)
    expect(settingsRoutes.find((route) => route.name === 'settings-tools')?.meta.requiresAdmin).toBeUndefined()
    expect(settingsRoutes.find((route) => route.name === 'settings-channels')?.meta).toEqual({
      settingsLabel: 'Kanäle', settingsIcon: 'forum'
    })
    expect(settingsRoutes.find((route) => route.name === 'settings-mcp')?.meta).toEqual({
      settingsLabel: 'MCP-Server', settingsIcon: 'hub'
    })
    expect(settingsRoutes.find((route) => route.name === 'settings-personal-variables')?.redirect).toEqual({
      name: 'personal-variables'
    })
    expect(settingsRoutes.find((route) => route.name === 'settings-approvals')?.redirect).toEqual({
      name: 'chat', query: { controlCenter: 'notifications' }
    })
    expect(settingsRoutes.find((route) => route.name === 'settings-personalization')?.meta).toEqual({
      settingsLabel: 'Personalisierung', settingsIcon: 'manage_accounts'
    })
    expect(settingsRoutes.find((route) => route.name === 'settings-persona')?.redirect).toEqual({
      name: 'settings-personalization', query: { tab: 'assistant' }
    })
    expect(settingsRoutes.find((route) => route.name === 'settings-persona')?.meta).toBeUndefined()
    expect(settingsRoutes
      .filter((route) => ['settings-providers', 'settings-integrations'].includes(route.name))
      .every((route) => route.meta.requiresAdmin === undefined)).toBe(true)
  })

  it('keeps user management and instance limits in the admin-only section', () => {
    const router = createRouter({ history: createMemoryHistory(), routes })

    expect(router.resolve('/settings/users').matched.at(-1).path).toBe('/:catchAll(.*)*')
    expect(router.resolve('/admin/users').name).toBe('admin-users')
    expect(router.resolve('/admin/users').meta.requiresAdmin).toBe(true)
    expect(router.resolve('/admin/instance-limits').meta.requiresAdmin).toBe(true)
    expect(router.resolve('/admin/appearance').meta.requiresAdmin).toBe(true)
    expect(router.resolve('/admin/providers').meta.requiresAdmin).toBe(true)
    expect(adminRoutes.find((route) => route.name === 'admin-providers').props).toEqual({ admin: true })
    expect(router.resolve('/settings').matched.at(-1).redirect).toEqual({ name: 'settings-personalization' })
  })
})
