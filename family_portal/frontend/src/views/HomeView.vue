<script setup lang="ts">
import { computed } from 'vue'
import { ArrowRight, WifiOff, Clock3, ChevronRight, Radio, MessageSquare } from '@lucide/vue'
import { usePortalStore } from '../stores/portal'
import IncidentRow from '../components/IncidentRow.vue'
import ConnectionPanel from '../components/ConnectionPanel.vue'
const portal = usePortalStore()
const current = computed(() => portal.urgent ?? portal.active.find(item => !portal.reviewFor(item.id).submitted) ?? portal.active[0])
const status = computed(() => {
  if (!portal.lastSync) return '等待首次同步'
  if (!portal.backendConnected) return '连接中断'
  return portal.deviceOnline ? '设备近期有上报' : '设备状态待更新'
})
const progress = computed(() => {
  const device = portal.devices.find(item => portal.deviceFresh(item))
  const names: Record<string,string> = { listening: '设备正在接收语音', asking: '设备正在询问', awaiting_answer: '设备正在等待回答', processing: '设备正在处理语音', alert_latched: '设备告警保持' }
  return device ? names[device.interaction_state] || '来源尚未提供可确认的询问阶段。' : '暂无新鲜的设备进度，保留待核查状态。'
})
</script>

<template>
  <div class="page-heading"><div><h1>家人近况</h1><p>查看最新状态与需要处理的事情。</p></div><span class="small-label">家中设备</span></div>
  <div class="home-grid">
    <section v-if="!portal.paired" class="overview disconnected" aria-labelledby="overview-title">
      <span class="overview-icon"><WifiOff :size="30" /></span><h2 id="overview-title">等待连接家中设备</h2><p>连接后，这里会显示家人的最新消息、告警原因和系统询问进度。</p>
      <div class="status-pair"><span>设备情况<strong>暂不可用</strong></span><span>最近更新<strong>暂无数据</strong></span></div>
      <ConnectionPanel />
    </section>
    <section v-else class="overview" :class="{ 'urgent-overview': current?.redLatched }" aria-labelledby="overview-title">
      <span class="state-tag" :class="current?.redLatched ? 'urgent' : current ? 'assistance' : 'neutral'">{{ current?.redLatched ? '告警保持' : current ? portal.reviewFor(current.id).submitted ? '核查已记录' : '待核查事件' : status }}</span>
      <h2 id="overview-title">{{ current?.title || (portal.backendConnected && portal.lastSync ? '暂无进行中的事件' : '正在等候设备消息') }}</h2>
      <p>{{ current?.reason || '设备上报的事件会在这里显示。' }}</p>
      <p v-if="current" class="footnote">{{ current.time }} · {{ current.source }}</p>
      <RouterLink v-if="current" class="button" :class="current.redLatched ? 'urgent-button' : 'primary'" :to="'/events/'+current.id">查看并处理<ArrowRight :size="18" /></RouterLink>
      <RouterLink v-else class="button primary" to="/events">查看事件<ArrowRight :size="18" /></RouterLink>
      <div class="status-pair"><span>设备连接<strong>{{ status }}</strong></span><span>最近同步<strong>{{ portal.lastSync || '等待更新' }}</strong></span></div>
    </section>
    <section class="recent-section" aria-labelledby="recent-title"><div class="section-heading"><h2 id="recent-title">最近消息</h2><RouterLink to="/events">全部<ChevronRight :size="16" /></RouterLink></div>
      <div v-if="portal.incidents.length" class="event-list home-recent-list"><IncidentRow v-for="event in portal.incidents.slice(0,5)" :key="event.id" :incident="event" :acknowledged="portal.reviewFor(event.id).acknowledged" :review-recorded="portal.reviewFor(event.id).submitted" /></div>
      <div v-else class="quiet-empty"><MessageSquare :size="25" /><h3>暂时没有可显示的消息</h3><p>{{ portal.backendConnected ? '服务当前没有返回事件记录。' : '连接设备后查看消息。' }}</p></div>
      <div class="progress-note"><Radio :size="20" /><div><h3>设备上报进度</h3><p>{{ portal.isService ? progress : '连接后可查看设备询问、等待回答与告警状态。' }}</p></div></div>
    </section>
  </div>
  <section class="home-bottom"><div><Clock3 :size="21" /><div><h3>每一步处理都有记录</h3><p>查看原因、确认收到、核查情况，分别展示处理进度。</p></div></div><RouterLink to="/history">查看历史<ArrowRight :size="17" /></RouterLink></section>
</template>
