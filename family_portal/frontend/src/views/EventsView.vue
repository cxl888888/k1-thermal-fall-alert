<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { BellOff, Archive } from '@lucide/vue'
import { usePortalStore } from '../stores/portal'
import { incidentSources } from '../incidentSource'
import type { IncidentSource } from '../types'
import IncidentRow from '../components/IncidentRow.vue'
import ConnectionPanel from '../components/ConnectionPanel.vue'
const portal = usePortalStore(), route = useRoute(), filter = ref<'active' | 'all'>('active')
const isHistory = computed(() => route.name === 'history')
const source = ref<IncidentSource | 'all'>('all')
const rows = computed(() => {
  const records = isHistory.value ? portal.history : filter.value === 'active' ? portal.active : portal.incidents
  return source.value === 'all' ? records : records.filter(event => event.sourceKind === source.value)
})
</script>

<template>
  <div class="page-heading"><div><h1>{{ isHistory ? '历史记录' : '事件' }}</h1><p>{{ isHistory ? '回看消息来源与已经记录的处置过程。' : '先了解原因，再确认和核查情况。' }}</p></div></div>
  <div v-if="portal.paired" class="event-filters">
    <div v-if="!isHistory" class="segmented" aria-label="筛选事件"><button :aria-pressed="filter === 'active'" @click="filter = 'active'">进行中 {{ portal.active.length }}</button><button :aria-pressed="filter === 'all'" @click="filter = 'all'">已载入 {{ portal.incidents.length }}</button></div>
    <label class="source-filter">事件来源<select v-model="source"><option value="all">全部来源</option><option v-for="(entry, key) in incidentSources" :key="key" :value="key">{{ entry.label }}</option></select></label>
  </div>
  <section v-if="rows.length" class="list-panel"><div class="list-caption">服务接收时间 · 每条记录标注实际来源</div><IncidentRow v-for="event in rows" :key="event.id" :incident="event" :acknowledged="portal.reviewFor(event.id).acknowledged" :review-recorded="portal.reviewFor(event.id).submitted" /></section>
  <section v-else class="page-empty"><Archive v-if="isHistory" :size="34" /><BellOff v-else :size="34" /><h2>{{ portal.lastSync ? '已载入记录中没有符合条件的事件' : portal.paired ? '等待同步事件' : '尚未接入设备事件' }}</h2><ConnectionPanel v-if="!portal.paired" /></section>
  <button v-if="portal.isService && portal.nextOffset !== null" class="button secondary" :disabled="portal.busy" @click="portal.loadMore()">加载更多记录（已载入 {{ portal.incidents.length }}／{{ portal.total }}）</button>
</template>
