<template>
  <template v-if="session.title != null">
    <SidebarNavItem
      v-if="showPin" icon="push_pin"
      :label="session.pinned ? 'Nicht mehr anheften' : 'Anheften'"
      :disable="pinning" v-close-popup @click="emit('toggle-pin', session)"
    />
    <SidebarNavItem icon="edit" label="Umbenennen" v-close-popup @click="emit('rename', session)" />
    <SidebarNavItem icon="drive_file_move" label="Verschieben" v-close-popup @click="emit('move', session)" />
    <q-separator class="q-my-xs" />
    <SidebarNavItem icon="archive" label="Archivieren" v-close-popup @click="emit('archive', session)" />
    <SidebarNavItem icon="delete_outline" label="Löschen" v-close-popup class="text-negative" @click="emit('delete', session)" />
  </template>
  <q-item v-else><q-item-section>Session wird geladen …</q-item-section></q-item>
</template>

<script setup>
import SidebarNavItem from 'src/shared/ui/SidebarNavItem.vue'

defineProps({
  session: { type: Object, required: true },
  showPin: Boolean,
  pinning: Boolean
})
const emit = defineEmits(['toggle-pin', 'rename', 'move', 'archive', 'delete'])
</script>
