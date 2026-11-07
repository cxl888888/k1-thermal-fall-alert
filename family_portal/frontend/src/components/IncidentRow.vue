<script setup lang="ts">
import { ChevronRight } from '@lucide/vue'
import { incidentSources } from '../incidentSource'
import type { Incident } from '../types'
defineProps<{ incident: Incident; acknowledged?: boolean; reviewRecorded?: boolean }>()
</script>

<template>
  <RouterLink :to="`/events/${incident.id}`" class="incident-row">
    <span class="event-icon" :class="incident.severity"><component :is="incidentSources[incident.sourceKind].icon" :size="21" aria-hidden="true" /></span>
    <span class="event-copy"><span class="row-title">{{ incident.title }}</span><span class="row-meta">{{ incident.time }} · {{ incident.source }}</span>
      <span v-if="incident.redLatched" class="row-state danger-text">{{ reviewRecorded ? '核查已记录，告警仍保持' : acknowledged ? '已确认收到，告警仍保持' : '告警保持 · 待核查' }}</span>
      <span v-else class="row-state">{{ incident.archived ? '已归档记录' : reviewRecorded ? '核查说明已记录' : acknowledged ? '已确认收到，待核查' : '消息待核查' }}</span>
    </span>
    <ChevronRight :size="19" class="row-chevron" aria-hidden="true" />
  </RouterLink>
</template>
