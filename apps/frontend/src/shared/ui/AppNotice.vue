<template>
  <div class="app-notice-card app-notice" :class="`app-notice--${tone}`">
    <q-icon :name="icon" class="app-notice__icon" aria-hidden="true" />
    <div class="app-notice__body">
      <div role="status">
        <strong>{{ title }}</strong>
        <div v-if="!$slots.actions" class="app-muted"><slot /></div>
      </div>
      <template v-if="$slots.actions">
        <slot />
        <div class="app-notice__actions"><slot name="actions" /></div>
      </template>
    </div>
    <q-btn
      flat round dense icon="close"
      class="app-notice__dismiss"
      aria-label="Meldung schließen"
      :disable="dismissDisabled"
      @click="$emit('dismiss')"
    />
  </div>
</template>

<script setup>
defineProps({
  title: { type: String, required: true },
  icon: { type: String, required: true },
  tone: { type: String, required: true },
  dismissDisabled: Boolean
})
defineEmits(['dismiss'])
</script>
