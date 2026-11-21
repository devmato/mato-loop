<template>
  <q-card flat class="app-surface">
    <q-card-section>
      <div class="text-subtitle1 text-weight-bold">Aktive Integrationen nach Agent</div>
      <div class="text-body2 app-muted">
        Die Tabelle zeigt die tatsächlich wirksame Verfügbarkeit. Wähle einen Eintrag für die Berechtigungen dieses Agenten.
      </div>
    </q-card-section>
    <q-separator />
    <q-card-section v-if="!profiles.length" class="q-pa-xl">
      <AppStateMessage
        icon="smart_toy"
        title="Keine aktiven Agentenprofile"
        description="Aktiviere ein Profil, um seine Integrationen anzuzeigen."
      />
    </q-card-section>
    <q-card-section v-else-if="!rows.length" class="q-pa-xl">
      <AppStateMessage
        icon="extension"
        title="Noch keine Integrationen"
        description="Füge zuerst eine Integration hinzu."
      />
    </q-card-section>
    <div v-else class="agent-integration-overview__scroll">
      <table class="q-table q-table--bordered q-table--dense full-width bg-transparent">
        <thead>
          <tr>
            <th class="text-left">Integration</th>
            <th v-for="profile in profiles" :key="profile.id" class="text-left">
              <div class="text-weight-medium">{{ profile.name }}</div>
              <div class="text-caption app-muted">{{ activeCount(profile.id) }} aktiv</div>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="integration in rows" :key="integration.id">
            <td>
              <div class="row items-center no-wrap q-gutter-sm">
                <q-avatar color="primary" text-color="white" size="32px" :icon="typeIcon(integration.integrationType)" />
                <div>
                  <div class="text-weight-medium">{{ integration.name }}</div>
                  <div class="text-caption app-muted">
                    {{ integration.scope === 'instance' ? 'Global' : 'Persönlich' }}
                  </div>
                </div>
              </div>
            </td>
            <td v-for="profile in profiles" :key="profile.id">
              <div class="row items-center no-wrap q-gutter-xs">
                <q-btn
                  flat
                  no-caps
                  dense
                  :aria-label="cellLabel(integration, profile)"
                  :to="cellTarget(integration, profile)"
                >
                  <q-avatar
                    size="24px"
                    :color="cellColor(integration, profile)"
                    text-color="white"
                    :icon="cellIcon(integration, profile)"
                  />
                </q-btn>
                <q-btn
                  flat
                  round
                  dense
                  icon="info_outline"
                  size="sm"
                  :aria-label="`Details zu ${integration.name} für ${profile.name}`"
                >
                  <AppMenu anchor="bottom middle" self="top middle" max-width="300px">
                    <q-card flat class="agent-integration-details app-surface">
                      <q-card-section class="q-pb-sm">
                        <div class="text-subtitle2 text-weight-bold">{{ integration.name }} · {{ profile.name }}</div>
                      </q-card-section>
                      <q-separator />
                      <q-list dense>
                        <q-item>
                          <q-item-section>
                            <q-item-label caption>Verfügbarkeit</q-item-label>
                            <q-item-label>{{ cellSource(integration, profile) }}</q-item-label>
                          </q-item-section>
                        </q-item>
                        <q-item>
                          <q-item-section>
                            <q-item-label caption>Tools</q-item-label>
                            <q-item-label>{{ cellLoading(integration, profile) }}</q-item-label>
                          </q-item-section>
                        </q-item>
                      </q-list>
                    </q-card>
                  </AppMenu>
                </q-btn>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </q-card>
</template>

<script setup>
import { computed } from 'vue'
import AppStateMessage from 'src/shared/ui/AppStateMessage.vue'
import AppMenu from 'src/shared/ui/AppMenu.vue'
import { useAgentProfileStore } from 'stores/agent-profiles'

const props = defineProps({ admin: { type: Boolean, default: false } })
const agents = useAgentProfileStore()
const profiles = computed(() => agents.agents.filter((profile) => profile.enabled !== false))
const rows = computed(() => agents.integrations)
const sourceLabels = {
  native: 'Nativ verfügbar',
  profile: 'Für diesen Agenten festgelegt',
  base: 'Vom Basisprofil geerbt',
  selected: 'Eigene Agentenintegration',
  default: 'Profilvorgabe'
}

function access(integration, profileId) {
  return integration.profiles?.find((item) => item.id === profileId) || null
}

function typeIcon(integrationType) {
  return {
    search: 'travel_explore', calendar: 'calendar_month', mail: 'mail', microsoft365: 'mail',
    support: 'confirmation_number', knowledge: 'menu_book', recipes: 'restaurant_menu',
    documents: 'description', infrastructure: 'dns', music: 'library_music',
    openscad: 'view_in_ar', coding: 'code', browser: 'web'
  }[integrationType] || 'extension'
}

function activeCount(profileId) {
  return rows.value.filter((integration) => access(integration, profileId)?.effective).length
}

function cellStatus(integration, profile) {
  const item = access(integration, profile.id)
  if (!integration.available) return 'Integration nicht verfügbar'
  if (!item?.available) return 'Nicht aktiv'
  return item.effective ? 'Aktiv' : 'Nicht verfügbar'
}

function cellColor(integration, profile) {
  const item = access(integration, profile.id)
  if (!integration.available) return 'grey-7'
  return item?.effective ? 'positive' : 'grey-7'
}

function cellIcon(integration, profile) {
  return access(integration, profile.id)?.effective && integration.available ? 'check' : 'close'
}

function cellSource(integration, profile) {
  const item = access(integration, profile.id)
  if (!item) return 'Keine Angaben'
  return sourceLabels[item.source] || 'Profilvorgabe'
}

function cellLoading(integration, profile) {
  const mode = access(integration, profile.id)?.toolLoading
  return mode === 'all' ? 'Alle Tools sofort laden' : mode === 'native' ? 'Native Tools sofort laden' : 'Bei Bedarf laden'
}

function cellLabel(integration, profile) {
  return `${integration.name} für ${profile.name}: ${cellStatus(integration, profile)}. Berechtigungen öffnen`
}

function cellTarget(integration, profile) {
  return {
    name: props.admin ? 'admin-integration-edit' : 'settings-integration-edit',
    params: { integrationId: integration.id },
    query: { tab: 'permissions', profileId: profile.id }
  }
}
</script>

<style scoped>
.agent-integration-overview__scroll {
  overflow-x: auto;
}

.agent-integration-overview__scroll th,
.agent-integration-overview__scroll td {
  min-width: 150px;
}

.agent-integration-overview__scroll th:first-child,
.agent-integration-overview__scroll td:first-child {
  position: sticky;
  left: 0;
  z-index: 1;
  min-width: 220px;
  background: var(--app-surface);
}

.agent-integration-overview__scroll thead th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--app-surface);
}

.agent-integration-overview__scroll thead th:first-child {
  z-index: 3;
}

.agent-integration-details {
  min-width: 250px;
}
</style>
