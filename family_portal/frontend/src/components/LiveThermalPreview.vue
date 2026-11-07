<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
import ThermalFrameView from './ThermalFrameView.vue'

const props = defineProps<{ sectionSelected: boolean }>()
const open = ref(false)
const pixels = ref<number[] | null>(null)
const low = ref(180), high = ref(380)
const narrow = window.matchMedia('(max-width: 760px)')
const status = ref<'loading' | 'live' | 'unavailable' | 'offline'>('loading')
const labels = { loading: '正在连接', live: '实时', unavailable: '暂无画面', offline: '连接中断' }
let timer: number | undefined
let generation = 0

async function poll(run: number) {
  try {
    const { frame } = await api<{ frame: { pixels_decic: number[] } | null }>('/thermal/live', undefined, 1500)
    if (run !== generation) return
    pixels.value = frame?.pixels_decic ?? null
    if (pixels.value) {
      low.value = Math.min(low.value, ...pixels.value)
      high.value = Math.max(high.value, ...pixels.value)
    }
    status.value = frame ? 'live' : 'unavailable'
  } catch {
    if (run !== generation) return
    pixels.value = null
    status.value = 'offline'
  }
  timer = window.setTimeout(() => { void poll(run) }, status.value === 'live' ? 500 : 1000)
}

function stop() {
  generation++
  window.clearTimeout(timer)
  pixels.value = null
}
function update() {
  stop()
  if (!open.value || document.hidden || (narrow.matches && !props.sectionSelected)) return
  status.value = 'loading'
  void poll(generation)
}
watch(open, value => {
  if (value) { low.value = 180; high.value = 380 }
  update()
})
watch(() => props.sectionSelected, selected => {
  if (!selected && narrow.matches) open.value = false
})
onMounted(() => {
  document.addEventListener('visibilitychange', update)
  narrow.addEventListener('change', update)
})
onUnmounted(() => {
  document.removeEventListener('visibilitychange', update)
  narrow.removeEventListener('change', update)
  stop()
})
</script>

<template>
  <section class="content-section live-thermal" aria-labelledby="thermal-live-title">
    <div class="section-heading">
      <h2 id="thermal-live-title">实时热像</h2>
      <button class="button secondary" type="button" :aria-expanded="open" @click="open = !open">{{ open ? '关闭预览' : '查看画面' }}</button>
    </div>
    <div v-if="open" class="live-thermal-content">
      <ThermalFrameView v-if="pixels" :pixels="pixels" :low="low" :high="high" />
      <p class="thermal-status" role="status">
        <span :class="{ 'thermal-active': status === 'live' }">{{ labels[status] }}</span>
        <span v-if="pixels">{{ (low / 10).toFixed(0) }}–{{ (high / 10).toFixed(0) }} ℃</span>
      </p>
    </div>
  </section>
</template>
