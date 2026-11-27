<template>
  <div ref="shell" class="app-closable-tab row items-stretch no-wrap col-auto" :class="{ 'app-closable-tab--active': active }" @keydown.capture="navigateTabs">
    <q-tab :id="tabId" :name="name" :aria-label="accessibleLabel || label" :alert-icon="alertIcon" :disable="disable" class="app-closable-tab__tab overflow-hidden">
      <slot name="leading">
        <q-icon v-if="icon" :name="icon" class="app-closable-tab__icon col-auto" />
      </slot>
      <span class="app-closable-tab__label ellipsis">{{ text ?? label }}</span>
      <q-tooltip>{{ label }}</q-tooltip>
      <slot name="menu" />
    </q-tab>
    <q-btn flat square dense icon="close" class="app-closable-tab__close" :aria-label="closeLabel || `${label} schließen`" :disable="disable" @click.stop="$emit('close')" @keydown.stop @dblclick.stop>
      <q-tooltip>{{ closeTooltip }}</q-tooltip>
    </q-btn>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({
  name: { type: [String, Number], required: true },
  label: { type: String, required: true },
  text: { type: String, default: undefined },
  accessibleLabel: { type: String, default: undefined },
  icon: { type: String, default: undefined },
  tabId: { type: String, default: undefined },
  alertIcon: { type: String, default: undefined },
  closeLabel: { type: String, default: undefined },
  closeTooltip: { type: String, default: 'Tab schließen' },
  active: Boolean,
  disable: Boolean
})
defineEmits(['close'])
const shell = ref(null)

onBeforeUnmount(() => {
  if (!shell.value.contains(document.activeElement)) return
  const strip = shell.value.closest('[role="tablist"]')
  void nextTick(() => {
    if (strip?.isConnected && document.activeElement === document.body) {
      strip.querySelector('[role="tab"][aria-selected="true"]')?.focus({ preventScroll: true })
    }
  })
})

watch(() => props.active, async (active) => {
  if (!active) return
  await nextTick()
  shell.value?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
}, { immediate: true })

function navigateTabs(event) {
  if (props.disable || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey
    || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)
    || !event.target.matches('.app-closable-tab__tab')) return
  // QTabs cannot traverse tab shells with sibling close buttons itself.
  const tabs = [...shell.value.closest('[role="tablist"]').querySelectorAll('.app-closable-tab__tab:not([aria-disabled="true"])')]
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
    : tabs.indexOf(event.target) + (event.key === 'ArrowLeft' ? -1 : 1)
  event.preventDefault()
  event.stopPropagation()
  tabs[index]?.focus({ preventScroll: true })
  tabs[index]?.parentElement.scrollIntoView({ block: 'nearest', inline: 'nearest' })
}
</script>

<style scoped>
.app-closable-tab {
  min-width: 0;
  max-width: min(240px, 100%);
  border: 1px solid transparent;
  border-right-color: var(--app-surface-border);
}

.app-closable-tab:hover { background: var(--app-hover); }

.app-closable-tab--active {
  border-color: color-mix(in srgb, var(--q-primary) 45%, var(--app-surface-border));
  background: var(--app-active);
  box-shadow: inset 0 2px var(--q-primary);
  color: var(--q-primary);
}

.app-closable-tab__tab {
  min-width: 0;
  min-height: 38px;
  flex: 1 1 auto;
  padding: 0 4px 0 12px;
}

.app-closable-tab__icon {
  font-size: 20px;
  margin-right: 8px;
}

.app-closable-tab__label { min-width: 0; }

.app-closable-tab__close {
  min-width: 32px;
  flex: 0 0 32px;
  color: var(--app-muted);
}

.app-closable-tab__close:hover { color: var(--app-text); }
</style>
