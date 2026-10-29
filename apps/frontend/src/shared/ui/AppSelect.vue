<template>
  <q-select
    dense
    hide-bottom-space
    v-bind="$attrs"
    :transition-duration="0"
    :behavior="behavior || ($q.screen.lt.sm ? 'dialog' : 'menu')"
    :popup-content-class="['app-popup-surface app-select-popup q-pa-xs', popupContentClass].filter(Boolean).join(' ')"
    options-selected-class="app-menu-item--active"
    :menu-offset="[0, 8]"
  >
    <template v-for="name in Object.keys($slots).filter(name => name !== 'option')" :key="name" #[name]="scope">
      <slot :name="name" v-bind="scope || {}" />
    </template>
    <template #option="scope">
      <slot name="option" v-bind="scope">
        <q-item v-bind="scope.itemProps" class="rounded-borders q-py-sm">
          <q-item-section v-if="scope.opt?.icon || scope.opt?.trustIcon" side>
            <q-icon :name="scope.opt.icon || scope.opt.trustIcon" size="20px" />
          </q-item-section>
          <q-item-section>
            <q-item-label>{{ scope.opt?.primaryLabel || scope.label }}</q-item-label>
            <q-item-label v-if="scope.opt?.caption" caption>{{ scope.opt.caption }}</q-item-label>
            <q-item-label v-if="scope.opt?.description" caption>{{ scope.opt.description }}</q-item-label>
          </q-item-section>
          <q-item-section side>
            <q-icon name="check" size="18px" color="primary" :class="{ invisible: !scope.selected }" aria-hidden="true" />
          </q-item-section>
        </q-item>
      </slot>
    </template>
  </q-select>
</template>

<script setup>
import { useQuasar } from 'quasar'

defineOptions({ name: 'AppSelect', inheritAttrs: false })
defineProps({
  behavior: { type: String, default: undefined },
  popupContentClass: { type: String, default: undefined }
})
const $q = useQuasar()
</script>
