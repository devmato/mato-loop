<template>
  <q-expansion-item
    :duration="0"
    v-model="expanded" dense expand-separator
    :icon="schedule.kind === 'once' ? 'event' : 'update'"
    :label="scheduleSummary(schedule)" :caption="caption"
    :header-class="error ? 'text-negative' : ''"
    :style="{ colorScheme: $q.dark.isActive ? 'dark' : 'light' }"
  >
    <div class="q-pa-md">
      <div class="row q-col-gutter-sm">
        <div class="col-12">
          <AppSelect v-model="schedule.kind" :readonly="readonly" outlined dense emit-value map-options label="Wann?" :options="[{ label: 'Einmalig', value: 'once' }, { label: 'Regelmäßig', value: 'cron' }]" />
        </div>
        <template v-if="schedule.kind === 'cron'">
          <div class="col-12 col-sm-6">
            <AppSelect v-model="schedule.preset" :readonly="readonly" outlined dense emit-value map-options label="Wiederholung" :options="schedulePresets" @update:model-value="applySchedulePreset(schedule)" />
          </div>
          <div v-if="schedule.preset !== 'custom'" class="col-12 col-sm-6">
            <q-input v-model="schedule.time" :readonly="readonly" outlined dense type="time" stack-label label="Uhrzeit" @update:model-value="applySchedulePreset(schedule)" />
          </div>
          <div v-if="schedule.preset === 'weekly'" class="col-12">
            <AppSelect v-model="schedule.weekday" :readonly="readonly" outlined dense emit-value map-options label="Wochentag" :options="weekdays" @update:model-value="applySchedulePreset(schedule)" />
          </div>
          <div v-if="schedule.preset === 'monthly'" class="col-12">
            <q-input v-model.number="schedule.monthDay" :readonly="readonly" outlined dense type="number" min="1" max="31" label="Tag im Monat" hint="In Monaten ohne diesen Tag entfällt der Termin." @update:model-value="applySchedulePreset(schedule)" />
          </div>
          <div v-if="schedule.preset === 'custom'" class="col-12">
            <q-expansion-item :duration="0" dense label="Zeitregel bearbeiten" caption="Für besondere Wiederholungen">
              <q-input v-model="schedule.cron" :readonly="readonly" outlined dense label="Zeitregel" maxlength="255" class="q-mt-sm" hint="Fünf Angaben: Minute, Stunde, Tag, Monat, Wochentag." />
            </q-expansion-item>
          </div>
        </template>
        <div v-else class="col-12">
          <q-input v-model="schedule.runAt" :readonly="readonly" outlined dense stack-label type="datetime-local" step="any" label="Datum und Uhrzeit" />
        </div>
      </div>
      <q-expansion-item v-model="schedule.advanced" :duration="0" dense label="Weitere Einstellungen" class="q-mt-sm">
        <div class="q-pt-sm q-gutter-sm">
          <template v-if="schedule.kind === 'cron'">
            <q-input v-model="schedule.timezone" :readonly="readonly" outlined dense label="Zeitzone" placeholder="Europe/Berlin" />
            <q-input v-model="schedule.validUntil" :readonly="readonly" outlined dense stack-label type="datetime-local" step="any" label="Endet am (optional)" :hint="`Lokale Uhrzeit (${browserTimezone()})`" />
          </template>
          <q-toggle v-model="schedule.enabled" :disable="readonly" dense label="Zeitplan aktiv" />
        </div>
      </q-expansion-item>
      <div v-if="error" role="alert" class="text-negative text-caption q-mt-sm">{{ error }}</div>
      <q-btn v-if="!readonly" flat dense no-caps icon="delete_outline" color="negative" label="Zeitplan entfernen" class="q-mt-sm" @click="$emit('remove')" />
    </div>
  </q-expansion-item>
</template>

<script setup>
import AppSelect from 'src/shared/ui/AppSelect.vue'
import { computed } from 'vue'
import { applySchedulePreset, browserTimezone, schedulePresets, scheduleSummary, weekdays } from 'src/utils/automation-schedules'
import { formatAbsoluteDateTime } from 'src/utils/date-time'

const schedule = defineModel({ type: Object, required: true })
const expanded = defineModel('expanded', { type: Boolean, default: false })
defineProps({ readonly: Boolean, error: { type: String, default: '' } })
defineEmits(['remove'])
const caption = computed(() => [
  !schedule.value.enabled ? 'Deaktiviert' : '',
  schedule.value.kind === 'cron' && schedule.value.validUntil ? `Bis ${formatAbsoluteDateTime(schedule.value.validUntil)}` : '',
  schedule.value.kind === 'cron' && schedule.value.timezone !== browserTimezone() ? schedule.value.timezone : ''
].filter(Boolean).join(' · '))
</script>
