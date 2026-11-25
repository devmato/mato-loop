<template>
  <q-list :id="id" role="listbox" aria-label="Befehle" class="q-pa-xs">
    <q-item v-if="commands.loading && !commands.loaded"><q-item-section>Wird geladen …</q-item-section></q-item>
    <q-item v-else-if="commands.failed" clickable @click="refresh"><q-item-section>Befehle erneut laden</q-item-section></q-item>
    <q-item v-else-if="!filtered.length"><q-item-section class="app-muted">Keine passenden Befehle verfügbar.</q-item-section></q-item>
    <q-item
      v-for="(command, index) in filtered" :id="`${id}-${index}`" :key="command.name"
      dense clickable role="option" :aria-selected="index === activeIndex" :active="index === activeIndex"
      :disable="disabled || Boolean(command.disabledReason)" class="rounded-borders"
      @mouseenter="activeIndex = index" @click="select(command)"
    >
      <q-item-section>
        <div class="row items-center no-wrap full-width">
          <span class="col-shrink ellipsis q-mr-sm">/{{ command.name }}</span>
          <span class="col ellipsis app-muted">{{ command.disabledReason || command.description }}</span>
        </div>
      </q-item-section>
    </q-item>
  </q-list>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { useCommandCatalog } from './catalog'

const props = defineProps({ id: { type: String, required: true }, filter: { type: String, default: '' }, disabled: Boolean })
const emit = defineEmits(['selected', 'resize'])
const { commands, refresh } = useCommandCatalog()
const filtered = computed(() => commands.items.filter(command => command.name.includes(props.filter.replace(/^\//u, '').toLowerCase())))
const activeIndex = ref(0)
const activeId = computed(() => filtered.value.length ? `${props.id}-${activeIndex.value}` : undefined)
watch(filtered, (items, previous) => {
  const selected = previous[activeIndex.value]?.name
  const retained = items.findIndex(command => command.name === selected && !command.disabledReason)
  activeIndex.value = retained >= 0 ? retained : Math.max(0, items.findIndex(command => !command.disabledReason))
  void nextTick(() => emit('resize'))
})
function select(command = filtered.value[activeIndex.value]) {
  if (!command) return false
  if (!props.disabled && !command.disabledReason) emit('selected', command)
  return true
}
function selectSuggestion() {
  const name = props.filter.slice(1).toLowerCase()
  if (filtered.value.some(command => command.name === name && !command.disabledReason)) return false
  return select()
}
function move(delta) {
  for (let step = 1; step <= filtered.value.length; step++) {
    const index = (activeIndex.value + delta * step + filtered.value.length) % filtered.value.length
    if (!filtered.value[index].disabledReason) {
      activeIndex.value = index
      void nextTick(() => document.getElementById(activeId.value)?.scrollIntoView({ block: 'nearest' }))
      break
    }
  }
}
defineExpose({ move, select, selectSuggestion, activeId, pending: computed(() => commands.loading) })
</script>
