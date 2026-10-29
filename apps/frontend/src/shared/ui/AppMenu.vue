<template>
  <q-menu ref="menu" v-bind="$attrs" :transition-duration="0" class="app-popup-surface app-action-menu" :offset="offset">
    <slot />
  </q-menu>
</template>

<script setup>
import { ref } from 'vue'

defineOptions({ name: 'AppMenu', inheritAttrs: false })
defineProps({ offset: { type: Array, default: () => [0, 8] } })
const menu = ref(null)
defineExpose({ updatePosition: () => menu.value?.updatePosition() })
</script>
