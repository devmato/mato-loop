import { computed, ref } from 'vue'
import { isNavigationFailure, NavigationFailureType } from 'vue-router'
import { sessionSidebarStatus } from 'src/features/chat/session-groups'
import { notifyError } from 'src/utils/notifications'
import { chooseDeletion } from 'src/features/archive/deletion-choice'
import { useSessionTabsStore } from './session-tabs-store'

const emptySidebarList = Object.freeze({
  items: [],
  loaded: false,
  loading: false,
  loadFailed: false,
  nextCursor: null,
  loadingMore: false,
  loadMoreFailed: false
})

export function useSidebarSessions({
  chat, projects, approvals, questions, workspace, controlCenter, automations,
  route, router, $q, confirm, drawerOpen, selectedProjectId, confirmDiscardedEdit
}) {
  const projectOptions = computed(() => [
    { label: 'Ohne Projekt', value: null },
    ...projects.items.map((project) => ({ label: project.name, value: project.id, project }))
  ])
  const standaloneSessionList = computed(() => projectSessionList(null))
  const sidebarLists = new WeakMap()
  const sessionStatuses = computed(() => {
    const counts = new Map()
    for (const request of [...approvals.pending, ...questions.pending]) {
      counts.set(request.sessionId, (counts.get(request.sessionId) || 0) + 1)
    }
    // Each rendered row computes its own run, delivery and unread indicators.
    return new Map([...counts].map(([id, count]) => [id, sessionSidebarStatus(null, count)]))
  })
  const renameDialogOpen = ref(false)
  const renameSessionId = ref(null)
  const renameTitle = ref('')
  const renaming = ref(false)
  const pinningSessionId = ref(null)
  const moveDialogOpen = ref(false)
  const moveSourceSession = ref(null)
  const moveProjectId = ref(null)
  const movingSession = ref(false)
  const draggedSession = ref(null)
  const sessionDropTarget = ref(undefined)

  function openRenameDialog(session) {
    renameSessionId.value = session.id
    renameTitle.value = session.title
    renameDialogOpen.value = true
  }

  function chatQuery(sessionId = null, projectId = selectedProjectId.value, extra = {}) {
    return {
      ...(projectId ? { project: projectId } : {}),
      ...(sessionId ? { session: sessionId } : {}),
      ...extra
    }
  }

  function projectSessionList(projectId) {
    const list = chat.sessionList(projectId)
    if (!list) return emptySidebarList
    if (!sidebarLists.has(list)) {
      const items = computed(() => {
        const excluded = chat.personalChatSessionId
        return excluded && list.items.some(session => session.id === excluded)
          ? list.items.filter(session => session.id !== excluded) : list.items
      })
      sidebarLists.set(list, { get items() { return items.value },
        get loaded() { return list.loaded }, get loading() { return list.loading },
        get loadFailed() { return list.loadFailed }, get loadingMore() { return list.loadingMore },
        get nextCursor() { return list.nextCursor }, get loadMoreFailed() { return list.loadMoreFailed } })
    }
    return sidebarLists.get(list)
  }

  async function loadProjectSessions(projectId, force = false) {
    const list = chat.ensureSessionList(projectId)
    if (!force && ((list.loaded && !list.stale) || list.loading)) return
    try {
      if (list.loaded && list.stale && !force) await chat.reconcileSessionList(projectId)
      else await chat.fetchSessions(null, projectId)
    } catch {
      // The central API interceptor displays the error and the folder exposes a retry action.
    }
  }

  async function loadStandaloneSessions(force = false) {
    const list = chat.ensureSessionList(null)
    if (!force && ((list.loaded && !list.stale) || list.loading)) return
    try {
      if (list.loaded && list.stale && !force) await chat.reconcileSessionList(null)
      else await chat.fetchSessions(null, null)
    } catch {
      // The central API interceptor displays the error and the list exposes a retry action.
    }
  }

  function startSessionDrag(event, session) {
    if (movingSession.value || moveDialogOpen.value || !event.dataTransfer) {
      event.preventDefault()
      return
    }
    draggedSession.value = session
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('application/x-matoloop-session', session.id)
  }

  function endSessionDrag() {
    draggedSession.value = null
    sessionDropTarget.value = undefined
  }

  function canDropSession(projectId) {
    return draggedSession.value && !movingSession.value && !moveDialogOpen.value
      && (draggedSession.value.projectId || null) !== projectId
  }

  function dragSessionOver(event, projectId) {
    if (!canDropSession(projectId)) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
    sessionDropTarget.value = projectId
  }

  function leaveSessionDropTarget(event) {
    if (!event.currentTarget.contains(event.relatedTarget)) sessionDropTarget.value = undefined
  }

  function dropSession(event, projectId) {
    if (canDropSession(projectId)) {
      event.preventDefault()
      openMoveDialog(draggedSession.value, projectId)
    }
    endSessionDrag()
  }

  function openMoveDialog(session, projectId = session.projectId || null) {
    moveSourceSession.value = (chat.current?.id === session.id ? chat.current : chat.findCachedSession(session.id)) || session
    moveProjectId.value = projectId
    moveDialogOpen.value = true
  }

  async function moveSession(options = {}) {
    const { newProjectName, applyProjectDefaults } = options
    const sessionId = moveSourceSession.value?.id
    const projectId = moveProjectId.value || null
    if (movingSession.value || !sessionId || (!newProjectName && projectId === (moveSourceSession.value.projectId || null))) return
    const targetProject = applyProjectDefaults ? projects.items.find(project => project.id === projectId) : null
    const switchesWorkspace = targetProject?.defaultExecutionAgentId || targetProject?.gitWorkspaceMode === 'worktree'
    if (switchesWorkspace && workspace.hasDirtyFiles(moveSourceSession.value.preferredExecutionAgentId, moveSourceSession.value.currentDirectory)) {
      $q.notify({ type: 'warning', message: 'Bitte zuerst die ungespeicherten Editoränderungen im bisherigen Arbeitsbereich speichern oder verwerfen.' })
      return
    }
    const affectedTabs = controlCenter.fileTabs.filter((tab) => (
      tab.fileTarget.scope === 'session'
      && (workspace.sessionId === sessionId
        ? tab.fileTarget.rootIdentity !== sessionId
        : tab.fileTarget.rootIdentity === sessionId)
    ))
    movingSession.value = true
    try {
      if (
        affectedTabs.some((tab) => workspace.fileTabDirty(tab.tabId))
        && !await confirmDiscardedEdit({ discard: false })
      ) return
      const moved = await chat.moveSession(sessionId, newProjectName ? null : projectId, options)
      const targetProjectId = moved.projectId || null
      if (affectedTabs.length) {
        affectedTabs.forEach((tab) => workspace.discardFileTab(tab.tabId))
        void controlCenter.removeFileTabs(affectedTabs.map((tab) => tab.tabId)).catch(() => {})
      }
      moveDialogOpen.value = false
      $q.notify({ type: 'positive', message: newProjectName ? 'Projekt angelegt und Session verschoben.' : 'Session verschoben.' })
      void projects.fetchAll().catch(() => {})
      if (route.query.session === sessionId) {
        await router.push({
          name: 'chat',
          query: {
            ...(targetProjectId ? { project: targetProjectId } : {}),
            session: sessionId
          }
        })
      }
      if (applyProjectDefaults && moved.codingWorkspace?.warningCode) void chat.refreshSession(sessionId).catch(() => {})
    } catch {
      // The central API interceptor displays the error; the dialog remains open.
    } finally {
      movingSession.value = false
    }
  }

  async function renameSession() {
    const title = renameTitle.value.trim()
    if (!renameSessionId.value || !title) return

    renaming.value = true
    try {
      await chat.renameSession(renameSessionId.value, title)
      renameDialogOpen.value = false
    } catch {
      // The central API interceptor displays the error; the dialog remains open for correction.
    } finally {
      renaming.value = false
    }
  }

  async function toggleSessionPinned(session) {
    if (pinningSessionId.value) return
    pinningSessionId.value = session.id
    try {
      await chat.setSessionPinned(session.id, !session.pinned)
    } catch {
      // The central API interceptor displays the error; existing state remains unchanged.
    } finally {
      pinningSessionId.value = null
    }
  }

  async function openNewSession(projectId = null) {
    if (!await confirmDiscardedEdit()) return false
    if ($q.screen.lt.md) drawerOpen.value = false
    try {
      await chat.setSessionScope(projectId)
    } catch {
      // The new-session draft remains usable while the sidebar offers a list retry.
    }
    await chat.loadSession(null)
    await router.push({ name: 'chat', query: chatQuery(null, projectId, { new: '1' }) })
    return true
  }

  async function openSession(session, messageId = null) {
    if ($q.screen.lt.md) drawerOpen.value = false
    const tabs = useSessionTabsStore()
    const alreadyOpen = route.name === 'chat' && route.query.session === session.id && chat.current?.id === session.id
    if (!messageId) delete tabs.readingPositions[session.id]
    const failure = await router.push({
      name: 'chat',
      query: chatQuery(
        session.id,
        session.projectId || null,
        messageId ? { message: messageId } : {}
      )
    })
    if (!messageId && alreadyOpen && (!failure || isNavigationFailure(failure, NavigationFailureType.duplicated))) {
      tabs.followLatestRequest = { sessionId: session.id }
    }
  }

  async function openSearchProject(project) {
    await openNewSession(project.id)
  }

  async function openSearchSession(session) {
    await openSession(session, session.messageId)
  }

  async function loadMoreSessions(projectId) {
    try {
      await chat.fetchMoreSessions(null, projectId)
    } catch {
      // The central API interceptor displays the error and the list exposes a retry action.
    }
  }

  async function retrySessionPagination(projectId) {
    chat.retrySessionPagination(projectId)
    await loadMoreSessions(projectId)
  }

  async function removeSession(session, { permanent = false } = {}) {
    const id = typeof session === 'string' ? session : session.id
    const sourceProjectId = typeof session === 'string'
      ? chat.findCachedSession(id)?.projectId || null
      : session.projectId || null
    const title = typeof session === 'string' ? chat.findCachedSession(id)?.title : session.title
    let impact
    try {
      impact = await chat.sessionDeleteImpact(id, { suppressErrorToast: true })
    } catch (error) {
      notifyError(error)
      return false
    }
    let selection
    try {
      selection = await chooseDeletion($q, 'Session', title || 'Ohne Titel', { permanent, impact })
    } catch { return false }
    if (selection === null) return false
    permanent = selection.permanent
    const { worktreeImpact, keepWorktree } = selection
    const removingWorktree = worktreeImpact || (permanent && impact.worktree?.scheduled && !keepWorktree)
    if (permanent && impact.fileReferenceCount) {
      $q.notify({
        type: 'negative',
        message: `${impact.fileReferenceCount} Dateireferenz${impact.fileReferenceCount === 1 ? '' : 'en'} ${impact.fileReferenceCount === 1 ? 'verhindert' : 'verhindern'} das Löschen dieser Session.`
      })
      return false
    }
    if (removingWorktree && workspace.hasDirtyEditors) {
      $q.notify({ type: 'warning', message: 'Bitte zuerst die ungespeicherten Editoränderungen speichern oder verwerfen.' })
      return false
    }
    let automationMode = null
    if (permanent && impact.requiredAutomationCount) {
      const count = impact.automationCount
      const accepted = await confirm(
        `Beim Fortfahren werden ${count} verknüpfte Automation${count === 1 ? '' : 'en'} samt Historie dauerhaft gelöscht.`,
        'Abhängige Automationen löschen?',
        { okLabel: 'Alles löschen' }
      )
      if (!accepted) return false
      automationMode = 'deleteAutomations'
    }
    const affectedTabs = controlCenter.fileTabs.filter((tab) => (
      tab.fileTarget.scope === 'session' && tab.fileTarget.rootIdentity === id
    ))
    if (
      affectedTabs.some((tab) => workspace.fileTabDirty(tab.tabId))
      && !await confirmDiscardedEdit({ discard: false })
    ) return false
    try {
      await chat.deleteSession(id, {
        permanent, worktreeImpact, keepWorktree,
        ...(permanent ? { automationMode,
          automationImpact: automationMode ? impact.automationImpact : null } : {})
      })
      if (sourceProjectId) void projects.fetchAll().catch(() => {})
    } catch {
      return false
    }
    if (workspace.sessionId !== id && affectedTabs.length) {
      void controlCenter.removeFileTabs(affectedTabs.map((tab) => tab.tabId)).catch(() => {})
    }
    affectedTabs.forEach((tab) => workspace.discardFileTab(tab.tabId))
    if (removingWorktree && impact.worktree?.sessions?.some(item => item.id === chat.current?.id)) {
      void chat.refreshSession(chat.current.id).catch(() => {})
    }
    if (automationMode || !permanent) void automations.fetchAutomations().catch(() => {})
    if (route.query.session === id) {
      await router.push({ name: 'chat', query: chatQuery(null, sourceProjectId) })
    }
    $q.notify({ type: 'positive', message: permanent ? 'Session endgültig gelöscht.' : 'Session in den Papierkorb verschoben.' })
    return true
  }

  async function archiveSession(session) {
    if (!await confirmDiscardedEdit()) return
    try {
      await chat.setArchived(session.id, true)
      await automations.fetchAutomations()
      $q.notify({ type: 'positive', message: 'Session archiviert' })
    } catch {
      // The Axios interceptor displays the error.
    }
  }

  return {
    projectOptions, standaloneSessionList, sessionStatuses,
    renameDialogOpen, renameTitle, renaming, pinningSessionId,
    moveDialogOpen, moveSourceSession, moveProjectId, movingSession,
    sessionDropTarget, openRenameDialog, projectSessionList, loadProjectSessions,
    loadStandaloneSessions, startSessionDrag, endSessionDrag, dragSessionOver,
    leaveSessionDropTarget, dropSession, openMoveDialog, moveSession,
    renameSession, toggleSessionPinned, openNewSession, openSession,
    openSearchProject, openSearchSession, loadMoreSessions,
    retrySessionPagination, removeSession, archiveSession, chatQuery
  }
}
