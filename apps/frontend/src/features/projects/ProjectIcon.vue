<template>
  <span class="project-icon" :class="{ 'project-icon--folder': expanded !== null }" :style="{ '--project-icon-light': tone?.light, '--project-icon-dark': tone?.dark }">
    <svg v-if="expanded !== null" class="project-icon__folder" viewBox="0 1 22 20" fill="none" stroke="var(--project-outline)" stroke-linejoin="round" aria-hidden="true" focusable="false">
      <path v-if="expanded" d="M1.5 17.5V4.5h6l2 2h7.5a1.5 1.5 0 0 1 1.5 1.5v.5 M1.5 17.5l2.5-9h17l-2.5 9z" />
      <path v-else d="M1.5 17.5v-13h6l2 2H19a1.5 1.5 0 0 1 1.5 1.5v9.5z" />
    </svg>
    <span aria-hidden="true">{{ initials }}</span>
    <slot />
  </span>
</template>

<script setup>
import { computed } from 'vue'
import { useProjectStore } from 'stores/projects'
import { projectIconTone } from './project-icon-palette'

const props = defineProps({
  projectId: { type: String, required: true },
  name: { type: String, required: true },
  iconInitials: { type: String, default: null },
  iconColor: { type: String, default: null },
  expanded: { type: Boolean, default: null }
})

const initials = computed(() => {
  if (props.iconInitials) return props.iconInitials
  const words = props.name.replace(/([\p{Ll}\p{N}])(\p{Lu})/gu, '$1 $2').match(/[\p{L}\p{N}]+/gu) || []
  const letters = words.length > 1 ? words.slice(0, 2).map(word => [...word][0]) : [...(words[0] || '')].slice(0, 2)
  return [...letters.join('').toUpperCase()].slice(0, 2).join('') || '·'
})
const projects = useProjectStore()
const tone = computed(() => projectIconTone(
  props.projectId ? projects.iconOrder.get(props.projectId) : projects.items.length,
  props.iconColor
))
</script>

<style scoped>
.project-icon {
  --project-outline: var(--project-icon-light, var(--app-text));
  position: relative;
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 20px;
  flex: none;
  border-radius: 4px;
  background: transparent;
  box-shadow: inset 0 0 0 1px var(--project-outline);
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
}

:global(body.body--dark .project-icon) {
  --project-outline: var(--project-icon-dark, var(--app-text));
}

.project-icon > span[aria-hidden="true"] { color: var(--project-outline); }

.project-icon--folder {
  box-shadow: none;
  padding-top: 4px;
  font-size: 8px;
}

.project-icon__folder {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
</style>
