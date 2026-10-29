import { computed, ref } from 'vue'
import { sessionSidebarStatus } from 'src/features/chat/session-groups'
import { notifyError } from 'src/utils/notifications'

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
    ...projects.items.map((project) => ({ label: project.name, value: project.id }))
  ])
  const standaloneSessionList = computed(() => chat.sessionList(null) || emptySidebarList)
  const sessionStatuses = computed(() => new Map(chat.cachedSessions.map((session) => [
    session.id,
    sessionSidebarStatus(
      session,
      approvals.forSession(session.id).length,
      questions.forSession(session.id).length
    )
  ])))
  const renameDialogOpen = ref(false)
  const renameSessionId = ref(null)
  const renameTitle = ref('')
  const renaming = ref(false)
  const pinningSessionId = ref(null)
  const moveDialogOpen = ref(false)
  const moveSessionId = ref(null)
  const moveSourceProjectId = ref(null)
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
    return chat.sessionList(projectId) || emptySidebarList
  }

  async function loadProjectSessions(projectId, force = false) {
    const list = chat.ensureSessionList(projectId)
    if (!force && (list.loaded || list.loading)) return
    try {
      await chat.fetchSessions(null, projectId)
    } catch {
      // The central API interceptor displays the error and the folder exposes a retry action.
    }
  }

  async function loadStandaloneSessions(force = false) {
    const list = chat.ensureSessionList(null)
    if (!force && (list.loaded || list.loading)) return
    try {
      await chat.fetchSessions(null, null)
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
    moveSessionId.value = session.id
    moveSourceProjectId.value = session.projectId || null
    moveProjectId.value = projectId
    moveDialogOpen.value = true
  }

  async function moveSession() {
    if (!moveSessionId.value || moveProjectId.value === moveSourceProjectId.value) return
    const sessionId = moveSessionId.value
    const targetProjectId = moveProjectId.value || null
    const affectedTabs = controlCenter.fileTabs.filter((tab) => (
      tab.fileTarget.scope === 'session'
      && (workspace.sessionId === sessionId
        ? tab.fileTarget.rootIdentity !== sessionId
        : tab.fileTarget.rootIdentity === sessionId)
    ))
    if (
      affectedTabs.some((tab) => workspace.fileTabDirty(tab.tabId))
      && !await confirmDiscardedEdit({ discard: false })
    ) return
    movingSession.value = true
    try {
      await chat.moveSession(sessionId, targetProjectId)
      if (affectedTabs.length) {
        affectedTabs.forEach((tab) => workspace.discardFileTab(tab.tabId))
        void controlCenter.removeFileTabs(affectedTabs.map((tab) => tab.tabId)).catch(() => {})
      }
      moveDialogOpen.value = false
      $q.notify({ type: 'positive', message: 'Session verschoben.' })
      if (route.query.session === sessionId) {
        await router.push({
          name: 'chat',
          query: {
            ...(targetProjectId ? { project: targetProjectId } : {}),
            session: sessionId
          }
        })
      }
      void projects.fetchAll().catch(() => {})
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
    await router.push({
      name: 'chat',
      query: chatQuery(
        session.id,
        session.projectId || null,
        messageId ? { message: messageId } : {}
      )
    })
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

  async function removeSession(session) {
    const id = typeof session === 'string' ? session : session.id
    const sourceProjectId = typeof session === 'string'
      ? chat.findCachedSession(id)?.projectId || null
      : session.projectId || null
    let impact
    try {
      impact = await chat.sessionDeleteImpact(id, { suppressErrorToast: true })
    } catch (error) {
      notifyError(error)
      return
    }
    if (impact.fileReferenceCount) {
      $q.notify({
        type: 'negative',
        message: `${impact.fileReferenceCount} Dateireferenz${impact.fileReferenceCount === 1 ? '' : 'en'} ${impact.fileReferenceCount === 1 ? 'verhindert' : 'verhindern'} das Löschen dieser Session.`
      })
      return
    }
    let removeWorktree = false
    const title = typeof session === 'string' ? chat.findCachedSession(id)?.title : session.title
    if (impact.hasWorktree) {
      const result = await new Promise(resolve => {
        $q.dialog({
          transitionDuration: 0,
          title: 'Session löschen',
          message: `Session „${title || 'Ohne Titel'}“ löschen? Ohne Auswahl bleibt der Worktree erhalten. Beim Entfernen werden nicht committete Änderungen sowie zusätzliche und ignorierte Dateien unwiderruflich gelöscht. Branch und Commits bleiben erhalten.`,
          options: { type: 'checkbox', model: [], items: [{ label: 'Worktree ebenfalls entfernen', value: 'remove' }] },
          ok: { label: 'Löschen', color: 'negative', unelevated: true, noCaps: true },
          cancel: { label: 'Abbrechen', flat: true, noCaps: true }
        }).onOk(values => resolve(values.includes('remove'))).onCancel(() => resolve(null)).onDismiss(() => resolve(null))
      })
      if (result === null) return
      removeWorktree = result
    } else if (!await confirm(`Session „${title || 'Ohne Titel'}“ löschen?`, 'Session löschen', { okLabel: 'Löschen' })) return
    let automationMode = null
    if (impact.requiredAutomationCount) {
      const count = impact.automationCount
      const accepted = await confirm(
        `Beim Fortfahren werden ${count} verknüpfte Automation${count === 1 ? '' : 'en'} samt Historie dauerhaft gelöscht.`,
        'Abhängige Automationen löschen?',
        { okLabel: 'Alles löschen' }
      )
      if (!accepted) return
      automationMode = 'deleteAutomations'
    }
    const affectedTabs = controlCenter.fileTabs.filter((tab) => (
      tab.fileTarget.scope === 'session' && tab.fileTarget.rootIdentity === id
    ))
    if (
      affectedTabs.some((tab) => workspace.fileTabDirty(tab.tabId))
      && !await confirmDiscardedEdit({ discard: false })
    ) return
    try {
      await chat.deleteSession(id, {
        removeWorktree,
        automationMode,
        automationImpact: automationMode ? impact.automationImpact : null
      })
      if (sourceProjectId) void projects.fetchAll().catch(() => {})
    } catch {
      return
    }
    if (workspace.sessionId !== id && affectedTabs.length) {
      void controlCenter.removeFileTabs(affectedTabs.map((tab) => tab.tabId)).catch(() => {})
    }
    affectedTabs.forEach((tab) => workspace.discardFileTab(tab.tabId))
    if (automationMode) void automations.fetchAutomations().catch(() => {})
    if (route.query.session === id) {
      await router.push({ name: 'chat', query: chatQuery(null, sourceProjectId) })
    }
  }

  return {
    projectOptions, standaloneSessionList, sessionStatuses,
    renameDialogOpen, renameTitle, renaming, pinningSessionId,
    moveDialogOpen, moveSourceProjectId, moveProjectId, movingSession,
    sessionDropTarget, openRenameDialog, projectSessionList, loadProjectSessions,
    loadStandaloneSessions, startSessionDrag, endSessionDrag, dragSessionOver,
    leaveSessionDropTarget, dropSession, openMoveDialog, moveSession,
    renameSession, toggleSessionPinned, openNewSession, openSession,
    openSearchProject, openSearchSession, loadMoreSessions,
    retrySessionPagination, removeSession, chatQuery
  }
}
