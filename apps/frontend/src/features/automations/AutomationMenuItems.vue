<template>
  <SidebarNavItem icon="edit" label="Automation bearbeiten" v-close-popup @click="emit('edit', automation)" />
  <SidebarNavItem icon="delete_outline" label="Automation löschen" v-close-popup class="text-negative" @click="remove" />
</template>

<script setup>
import { useQuasar } from 'quasar'
import { useRoute, useRouter } from 'vue-router'
import SidebarNavItem from 'src/shared/ui/SidebarNavItem.vue'
import { useConfirm } from 'src/composables/confirm'
import { useAutomationStore } from 'stores/automations'

const props = defineProps({ automation: { type: Object, required: true } })
const emit = defineEmits(['edit'])
const $q = useQuasar()
const route = useRoute()
const router = useRouter()
const automations = useAutomationStore()
const { confirm } = useConfirm()

async function remove() {
  const automation = props.automation
  if (!await confirm(`„${automation.name}“ mit allen Zeitplänen löschen? Die Session und ihre Nachrichten bleiben erhalten.`, 'Automation löschen?', { okLabel: 'Löschen' })) return
  try {
    if (route.query.session === automation.executionSessionId && route.query.automation === automation.id) {
      const failure = await router.replace({ query: { ...route.query, automation: undefined, run: undefined } })
      if (failure) return
    }
    await automations.deleteAutomation(automation.id, automation.revision)
    $q.notify({ type: 'positive', message: 'Automation gelöscht' })
  } catch {
    // The central Axios interceptor displays the error.
  }
}
</script>
