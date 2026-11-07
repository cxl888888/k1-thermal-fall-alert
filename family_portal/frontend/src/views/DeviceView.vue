<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { Cpu, Mic, Volume2, Thermometer, Radio, Wifi, WifiOff, Info } from '@lucide/vue'
import { usePortalStore } from '../stores/portal'
import { api } from '../api'
import ConnectionPanel from '../components/ConnectionPanel.vue'

type StatusKey = 'microphone' | 'speaker' | 'thermal' | 'radar'
interface HardwareStatus {
  checked_at: string
  source: 'k1_local_observation'
  items: Record<StatusKey, { state: string; detail: string }>
}

const portal = usePortalStore()
const hardware = ref<HardwareStatus | null>(null)
const readAt = ref(0), clock = ref(performance.now()), statusError = ref('')
const loading = ref(false)
let mounted = false, refreshTimer: number | undefined, clockTimer: number | undefined, session = 0
const fresh = computed(() => !!hardware.value && !statusError.value && clock.value - readAt.value < 15000)
const checkedAt = computed(() => {
  if (!hardware.value) return '尚未获取'
  const time = new Date(hardware.value.checked_at)
  return Number.isNaN(time.getTime()) ? '时间未核验' : time.toLocaleTimeString('zh-CN', { hour12: false })
})
const labels: Record<string,string> = {
  active: '正在出数', connected: '已识别', not_detected: '未识别',
  muted: '已静音', waiting: '等待数据', stale: '数据已过期', error: '读取异常', unknown: '待核验',
}
const devices = computed(() => [
  { name: '家中主设备', icon: Cpu, state: fresh.value ? '已连接' : '状态待更新',
    detail: fresh.value ? '已连接到K1设备状态服务。' : statusError.value || '尚未取得K1实时状态。', tone: fresh.value ? 'neutral' : 'assistance' },
  ...([{ name: '麦克风', icon: Mic, key: 'microphone' }, { name: '语音播报', icon: Volume2, key: 'speaker' },
       { name: '热像传感器', icon: Thermometer, key: 'thermal' }, { name: '雷达', icon: Radio, key: 'radar' }] as const).map(item => {
    const observed = fresh.value ? hardware.value?.items[item.key] : undefined
    const state = observed?.state || (hardware.value ? 'stale' : 'unknown')
    const name = state === 'active' && (item.key === 'microphone' || item.key === 'speaker') ? '接口使用中' : labels[state] || '待核验'
    const detail = observed?.detail || (hardware.value ? '上次检测结果已过期，请刷新设备状态。' : '等待取得设备状态。')
    return { ...item, state: name, detail,
      tone: ['error', 'muted', 'not_detected', 'stale'].includes(state) ? 'assistance' : 'neutral' }
  }),
])

async function refreshHardware() {
  if (!portal.paired || loading.value) return
  const run = session
  const started = performance.now()
  loading.value = true
  try {
    const result = await api<HardwareStatus>('/hardware-status')
    if (!mounted || run !== session) return
    if (result.source !== 'k1_local_observation' || !result.items) throw new Error('设备状态格式异常。')
    hardware.value = result; readAt.value = started; clock.value = performance.now(); statusError.value = ''
  } catch {
    if (mounted && run === session) statusError.value = '无法获取实时状态，请检查板子连接或稍后重试。'
  } finally { if (run === session) loading.value = false }
}
watch(() => portal.paired, paired => {
  session++; hardware.value = null; loading.value = false; statusError.value = ''
  if (mounted && paired) void refreshHardware()
}, { flush: 'sync' })
onMounted(() => {
  mounted = true
  void refreshHardware()
  refreshTimer = window.setInterval(() => { void refreshHardware() }, 5000)
  clockTimer = window.setInterval(() => { clock.value = performance.now() }, 1000)
})
onUnmounted(() => {
  mounted = false
  window.clearInterval(refreshTimer); window.clearInterval(clockTimer)
})
</script>

<template>
  <div class="page-heading"><div><h1>设备状态</h1><p>查看家中设备和外设状态。</p></div><RouterLink v-if="portal.paired" class="button secondary" to="/device/contact">联系电话</RouterLink></div>
  <section class="device-connection" aria-live="polite"><component :is="fresh ? Wifi : WifiOff" :size="24" /><div><h2>{{ fresh ? 'K1设备状态已更新' : 'K1设备状态待更新' }}</h2><p>最近检测：{{ checkedAt }} · 页面每5秒刷新</p></div></section>
  <ConnectionPanel />
  <section class="device-list" aria-label="设备与外设"><article v-for="device in devices" :key="device.name" class="device-row"><span class="device-icon"><component :is="device.icon" :size="23" /></span><div><div class="device-title"><h3>{{ device.name }}</h3><span class="state-tag" :class="device.tone">{{ device.state }}</span></div><p>{{ device.detail }}</p></div></article></section>
  <div v-if="portal.paired" class="service-controls"><button class="button secondary" :disabled="loading" @click="refreshHardware">{{ loading ? '正在刷新' : '刷新设备状态' }}</button><template v-if="!portal.directAccess"><p>当前手机：{{ portal.clientLabel }}</p><button class="button secondary" :disabled="portal.busy" @click="portal.logout()">退出此手机会话</button></template></div>
  <div class="context-note with-icon"><Info :size="19" /><p>音频接口状态来自 K1 检测。热像和雷达的出数状态根据近期有效采集帧更新。</p></div>
</template>
