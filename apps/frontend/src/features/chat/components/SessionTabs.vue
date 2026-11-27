<template>
  <div v-if="enabled && tabs.items.length" class="session-tabs row no-wrap items-center col-auto" :class="{ 'session-tabs--top': position === 'top' }" aria-label="Offene Sessions">
    <q-resize-observer :debounce="0" @resize="barWidth = $event.width" />
    <q-tabs :model-value="activeKey" align="left" :breakpoint="0" no-caps dense inline-label class="col session-tabs__list" aria-label="Session-Tabs" @update:model-value="select">
      <AppClosableTab
        v-for="(tab, index) in visibleTabs"
        :key="tab.key"
        :name="tab.key"
        :label="tab.label"
        :text="tab.title"
        :accessible-label="`${tab.label}${tab.preview ? ', Vorschau' : ''}`"
        :close-label="`Tab ${tab.label} schließen`"
        :active="tab.key === activeKey"
        class="session-tabs__tab"
        :class="{
          'session-tabs__preview': tab.preview,
          'session-tabs__dragging': draggedId === tab.key,
          'session-tabs__drop-before': dropTarget?.id === tab.key && !dropTarget.after,
          'session-tabs__drop-after': dropTarget?.id === tab.key && dropTarget.after
        }"
        :draggable="projectOrder"
        @dragstart="projectOrder && startDrag($event, tab.key)"
        @dragover="dragOver($event, tab)"
        @drop="drop"
        @dragend="endDrag"
        @keydown.alt.left.prevent.stop="move(index, -1)"
        @keydown.alt.right.prevent.stop="move(index, 1)"
        @dblclick="tabs.keep(tab.key)"
        @close="close(tab)"
      >
        <template #leading>
          <ProjectIcon v-if="tab.projectName" class="session-tabs__project-badge" :project-id="tab.projectId" :name="tab.projectName" :icon-initials="tab.project.iconInitials" :icon-color="tab.project.iconColor">
            <SessionStatusIndicator v-if="tab.status" :status="tab.status" class="session-tabs__status-badge" />
          </ProjectIcon>
          <span v-else-if="tab.status" class="session-tabs__status">
            <SessionStatusIndicator :status="tab.status" />
          </span>
        </template>
        <template #menu>
          <q-menu v-if="projectOrder" touch-position context-menu>
            <q-list dense>
              <q-item clickable v-close-popup :disable="!canMove(index, -1)" @click="move(index, -1)"><q-item-section>Nach links verschieben</q-item-section></q-item>
              <q-item clickable v-close-popup :disable="!canMove(index, 1)" @click="move(index, 1)"><q-item-section>Nach rechts verschieben</q-item-section></q-item>
            </q-list>
          </q-menu>
        </template>
      </AppClosableTab>
    </q-tabs>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ClosePopup, QTabs, QMenu, QList, QItem, QItemSection, QResizeObserver } from 'quasar'
import AppClosableTab from 'src/shared/ui/AppClosableTab.vue'
import SessionStatusIndicator from './SessionStatusIndicator.vue'
import ProjectIcon from 'src/features/projects/ProjectIcon.vue'
import { useUserPreferencesStore } from 'src/features/settings/user-preferences-store'
import { useTabReorder } from 'src/shared/ui/useTabReorder'
import { useRouter } from 'vue-router'
import { useApprovalStore } from 'stores/approvals'
import { useQuestionStore } from 'stores/questions'
import { useProjectStore } from 'stores/projects'
import { useChatStore } from '../store'
import { sessionSidebarStatus } from '../session-groups'
import { sessionTabTarget } from '../session-tabs-store'

const props = defineProps({ enabled: Boolean, tabs: { type: Object, required: true }, activeKey: { type: String, required: true }, position: { type: String, default: 'bottom' } })
const preferences = useUserPreferencesStore()
const projectOrder = computed(() => preferences.sessionTabsOrder === 'project')
const vClosePopup = ClosePopup
const { draggedId, dropTarget, startDrag, updateDropTarget, drop, endDrag } = useTabReorder(
  (source, target, after) => projectOrder.value && props.tabs.reorder(source, target, after)
)
const router = useRouter()
const chat = useChatStore()
const approvals = useApprovalStore()
const questions = useQuestionStore()
const projects = useProjectStore()
const barWidth = ref(Infinity)
const minTabWidth = 75
const projectDisplay = computed(() => new Map(projects.items.map((project, index) => [project.id, { project, index }])))
const displayTabs = computed(() => props.tabs.items.map(tab => {
  const current = chat.current?.id === tab.sessionId ? chat.current : null
  const summary = chat.findCachedSession(tab.sessionId) || props.tabs.summaries?.[tab.sessionId]
  const session = current || summary
  const title = session?.title || tab.title
  const projectId = session && Object.hasOwn(session, 'projectId') ? session.projectId : tab.projectId
  const entry = projectDisplay.value.get(projectId)
  const project = entry?.project
  const projectName = project?.name || ''
  const projectRank = projectId ? (entry?.index ?? projectDisplay.value.size) : -1
  const status = sessionSidebarStatus(
    summary || { activeRunStatus: current?.activeRun?.status },
    approvals.forSession(tab.sessionId).length, questions.forSession(tab.sessionId).length,
    chat.sessionHasPendingSend(tab.sessionId, tab.projectId)
  ) || ((summary || current)?.hasUnreadMessages ? { kind: 'unread', label: 'Ungelesene Antwort' } : null)
  return { ...tab, projectId, projectRank, project, title, projectName, label: projectName ? `${title} · ${projectName}` : title, status }
}).sort((a, b) => projectOrder.value
  ? a.projectRank - b.projectRank || (a.projectId || '').localeCompare(b.projectId || '')
  : (a.addedAt ?? 0) - (b.addedAt ?? 0)))
const visibleTabs = computed(() => {
  const list = displayTabs.value
  const count = Math.max(1, Math.floor(barWidth.value / minTabWidth))
  if (list.length <= count) return list
  const active = list.find(tab => tab.key === props.activeKey)
  const recent = list.filter(tab => tab !== active)
    .sort((a, b) => (b.lastUsedAt ?? 0) - (a.lastUsedAt ?? 0))
  const visible = new Set(recent.slice(0, count - Number(Boolean(active))))
  if (active) visible.add(active)
  return list.filter(tab => visible.has(tab))
})
function canMove(index, offset) {
  const list = visibleTabs.value
  return projectOrder.value && list[index + offset] && list[index + offset].projectId === list[index].projectId
}
function move(index, offset) {
  if (!canMove(index, offset)) return
  const list = visibleTabs.value
  props.tabs.reorder(list[index].key, list[index + offset].key, offset > 0)
}
function dragOver(event, tab) {
  if (projectOrder.value && displayTabs.value.find(item => item.key === draggedId.value)?.projectId === tab.projectId) {
    updateDropTarget(event, tab.key)
  } else {
    dropTarget.value = null
  }
}
async function select(key) {
  const tab = props.tabs.items.find(tab => tab.key === key)
  if (tab) await router.push(sessionTabTarget(tab))
}
async function close(tab) {
  if (tab.key === props.activeKey) {
    const list = visibleTabs.value.length > 1 ? visibleTabs.value : displayTabs.value
    const index = list.findIndex(item => item.key === tab.key)
    const neighbor = list[index + 1] || list[index - 1]
    const next = neighbor && props.tabs.items.find(item => item.key === neighbor.key)
    const failure = await router.push(next ? sessionTabTarget(next) : { name: 'chat' })
    if (failure) return
  }
  props.tabs.close(tab.key)
}
</script>

<style scoped>
.session-tabs {
  min-width: 0;
  box-shadow: inset 0 1px var(--app-surface-border);
  background: var(--app-shell-bg);
  color: var(--app-text);
}

.session-tabs--top {
  order: -1;
  box-shadow: inset 0 -1px var(--app-surface-border);
}

.session-tabs .session-tabs__tab {
  min-width: min(100%, 56px);
  max-width: 180px;
  flex: 0 1 auto;
  min-height: 32px;
  color: var(--app-text);
}

.session-tabs .session-tabs__tab:has(.session-tabs__project-badge, .session-tabs__status) {
  min-width: min(100%, 75px);
}

.session-tabs__dragging { opacity: .45 !important; }

.session-tabs .session-tabs__tab.app-closable-tab--active {
  background: var(--app-page-bg);
  border-color: var(--app-surface-border);
  border-top-color: transparent;
  border-radius: 0 0 9px 9px;
  box-shadow: none;
}

.session-tabs--top.session-tabs .session-tabs__tab.app-closable-tab--active {
  border-top-color: var(--app-surface-border);
  border-bottom-color: transparent;
  border-radius: 9px 9px 0 0;
}

:global(body.body--dark .session-tabs .session-tabs__tab.app-closable-tab--active) {
  background: color-mix(in srgb, var(--app-page-bg) 93%, white);
  border-color: rgba(255, 255, 255, 0.38);
}

.session-tabs .session-tabs__tab.session-tabs__drop-before { box-shadow: inset 2px 0 var(--q-primary); }
.session-tabs .session-tabs__tab.session-tabs__drop-after { box-shadow: inset -2px 0 var(--q-primary); }

.session-tabs__status { flex: 0 0 16px; margin-right: 4px; }
.session-tabs__project-badge { margin-right: 5px; }
.session-tabs__project-badge > .session-tabs__status-badge {
  position: absolute;
  top: -5px;
  right: -4px;
  transform: scale(.625);
  transform-origin: top right;
  border-radius: 4px;
  background: var(--app-page-bg);
}
.session-tabs__list { min-width: 0; }
.session-tabs :deep(.q-tabs__content) {
  flex-wrap: nowrap;
  overflow: hidden;
}
.session-tabs :deep(.app-closable-tab__tab) { min-height: 30px; padding: 0 2px 0 5px; font-size: 13px; opacity: 1; }
.session-tabs :deep(.q-tab__content) { min-width: 0; width: 100%; justify-content: flex-start; padding: 0; }
.session-tabs :deep(.q-tab__indicator) { display: none; }
.session-tabs :deep(.q-tabs__arrow) { display: none; }
.session-tabs__preview :deep(.app-closable-tab__label) { font-style: italic; }
.session-tabs :deep(.app-closable-tab__close) { min-width: 24px; flex-basis: 24px; }
.session-tabs :deep(.app-closable-tab__close .q-icon) { font-size: 14px; }
</style>
