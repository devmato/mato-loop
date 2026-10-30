<template>
  <q-item
    clickable dense v-ripple
    class="sidebar-row session-list-item items-center rounded-borders q-px-sm q-py-xs"
    :class="{ 'session-list-item--menu-open': menuOpen }"
    :active="active"
    active-class="sidebar-row--active"
    @mouseenter="prepareTitleScroll"
    @focusin="prepareTitleScroll"
    @click="emit('open', session)"
  >
    <q-item-section side class="q-pr-sm">
      <span class="session-list-item__status-slot row items-center justify-center">
        <span v-if="indicator" role="status" :aria-label="indicator.label">
          <template v-if="indicator.kind === 'approval'">
            <q-icon name="pending_actions" color="warning" size="16px" />
            <q-badge v-if="indicator.count > 1" floating rounded color="warning" text-color="dark" :label="indicator.count" />
          </template>
          <q-spinner-grid v-else-if="indicator.kind === 'active'" color="primary" size="16px" />
          <q-icon v-else name="fiber_manual_record" color="primary" size="12px" />
          <q-tooltip v-if="$q.platform.is.mobile !== true">{{ indicator.label }}</q-tooltip>
        </span>
      </span>
    </q-item-section>
    <q-item-section
      class="session-list-item__content"
      :draggable="draggable"
      @dragstart="emit('drag-start', $event, session)"
      @dragend="emit('drag-end')"
    >
      <q-item-label
        class="session-list-item__title text-body2"
        :class="{ 'text-weight-medium': active }"
        :title="caption || undefined"
      >
        <span class="session-list-item__title-text">{{ title || session.title }}</span>
        <span v-if="session.hasUnreadMessages" class="app-sr-only">, ungelesene Nachrichten</span>
      </q-item-label>
    </q-item-section>
    <q-item-section side class="session-list-item__trailing self-center">
      <div class="session-list-item__utility row no-wrap items-center">
        <div class="session-list-item__actions row no-wrap items-center">
          <SidebarIconButton
            v-if="showPin && !$q.screen.lt.md" icon="push_pin" class="session-list-item__pin-button"
            :class="{ 'session-list-item__pin-button--pinned': session.pinned }"
            :color="session.pinned ? 'primary' : ($q.dark.isActive ? 'grey-5' : 'grey-7')"
            :loading="pinning"
            :aria-label="session.pinned ? `${session.title} nicht mehr anheften` : `${session.title} anheften`"
            @click="emit('toggle-pin', session)"
          />
          <SidebarActionMenu
            v-model="menuOpen"
            class="session-list-item__menu-button"
            :color="$q.dark.isActive ? 'grey-5' : 'grey-7'"
            :aria-label="`Aktionen für ${title || session.title}`"
            list-class="session-actions-menu"
          >
            <slot name="menu">
              <SessionMenuItems
                :session="session" :show-pin="showPin && $q.screen.lt.md" :pinning="pinning"
                @toggle-pin="emit('toggle-pin', $event)" @rename="emit('rename', $event)"
                @move="emit('move', $event)" @archive="emit('archive', $event)" @delete="emit('delete', $event)"
              />
            </slot>
          </SidebarActionMenu>
        </div>
        <time
          v-if="timestamp && !$q.screen.lt.md" class="session-list-item__time q-ml-xs text-caption app-muted text-no-wrap text-right"
          :datetime="timestamp" :title="`Letzte Aktivität: ${formatAbsoluteDateTime(timestamp)}`"
          :aria-label="`Letzte Aktivität: ${formatAbsoluteDateTime(timestamp)}`"
        >{{ formatCompactAge(timestamp, now) }}</time>
      </div>
    </q-item-section>
  </q-item>
</template>

<script setup>
import { computed } from 'vue'
import { formatAbsoluteDateTime, formatCompactAge } from 'src/utils/date-time'
import { sessionSidebarStatus } from '../session-groups'
import SidebarIconButton from 'src/shared/ui/SidebarIconButton.vue'
import SessionMenuItems from './SessionMenuItems.vue'
import SidebarActionMenu from './SidebarActionMenu.vue'

const props = defineProps({
  session: { type: Object, required: true },
  now: { type: Number, default: Date.now },
  active: Boolean,
  title: { type: String, default: '' },
  caption: { type: String, default: '' },
  status: { type: Object, default: null },
  showPin: { type: Boolean, default: true },
  pinning: Boolean,
  draggable: { type: Boolean, default: true }
})
const menuOpen = defineModel('menuOpen', { type: Boolean, default: false })
const emit = defineEmits(['open', 'rename', 'move', 'archive', 'delete', 'toggle-pin', 'drag-start', 'drag-end'])
const timestamp = computed(() => props.session.lastMessageAt || props.session.createdAt)
const indicator = computed(() => props.status || sessionSidebarStatus(props.session)
  || (props.session.hasUnreadMessages ? { kind: 'unread', label: 'Ungelesene Antwort' } : null))

function prepareTitleScroll(event) {
  const title = event.currentTarget.querySelector('.session-list-item__title')
  const text = title.querySelector('.session-list-item__title-text')
  const style = window.getComputedStyle(title)
  const fade = (parseFloat(style.getPropertyValue('--session-title-fade-width')) || 0)
    + (parseFloat(style.getPropertyValue('--session-title-fade-inset')) || 0)
  const distance = Math.max(0, text.getBoundingClientRect().width - title.clientWidth + fade)
  title.style.setProperty('--session-title-scroll-distance', `${-distance}px`)
  title.style.setProperty('--session-title-scroll-duration', `${distance / 35}s`)
}
</script>
