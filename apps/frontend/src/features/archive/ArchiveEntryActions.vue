<template>
  <div class="row no-wrap items-center" @click.stop>
    <SidebarIconButton v-if="restorable" :icon="trash ? 'restore_from_trash' : 'unarchive'" :aria-label="`${label} wiederherstellen`" :disable="disabled || expired" :loading="restoring" @click="emit('restore')" />
    <SidebarActionMenu v-model="menuOpen" :aria-label="`Aktionen für ${title}`">
      <q-item v-if="restorable" clickable v-close-popup :disable="disabled || expired" @click="emit('restore')">
        <q-item-section avatar><q-icon name="restore" /></q-item-section>
        <q-item-section>Wiederherstellen</q-item-section>
      </q-item>
      <q-item clickable v-close-popup :disable="disabled" class="text-negative" @click="emit('delete')">
        <q-item-section avatar><q-icon :name="trash ? 'delete_forever' : 'delete_outline'" /></q-item-section>
        <q-item-section>{{ trash ? 'Sofort löschen' : 'Löschen' }}</q-item-section>
      </q-item>
    </SidebarActionMenu>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import SidebarIconButton from 'src/shared/ui/SidebarIconButton.vue'
import SidebarActionMenu from 'src/features/chat/components/SidebarActionMenu.vue'

defineProps({ title: { type: String, required: true }, label: { type: String, required: true }, trash: Boolean, disabled: Boolean, expired: Boolean, restoring: Boolean, restorable: { type: Boolean, default: true } })
const emit = defineEmits(['restore', 'delete'])
const menuOpen = ref(false)
</script>
