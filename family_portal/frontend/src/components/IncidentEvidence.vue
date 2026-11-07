<script setup lang="ts">
import { computed, onUnmounted, ref, shallowRef, watch } from 'vue'
import { ChevronDown } from '@lucide/vue'
import { api, type ServerVoicePage } from '../api'
import type { IncidentEvidence, VoiceRecord } from '../types'
import LiveThermalPreview from './LiveThermalPreview.vue'

const props = defineProps<{ incidentId: string; evidence?: IncidentEvidence; sectionSelected: boolean }>()
const voiceLabels: Record<VoiceRecord['kind'], string> = {
  utterance: '现场语音', question: '设备询问', answer: '现场回答',
  no_answer: '未收到回答', playback_failed: '询问播放失败', thermal_candidate: '热像',
}
const riskLabels: Record<NonNullable<VoiceRecord['risk']>, string> = {
  thermal_candidate: '疑似跌倒', self_reported_fall: '自述跌倒', emergency_help: '紧急求救',
}
const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})
function displayTime(value: string) {
  const time = new Date(value)
  return Number.isNaN(time.getTime()) ? value : timeFormat.format(time)
}

const records = shallowRef<VoiceRecord[]>([])
const before = ref<number | null>(null)
const historyOpen = ref(false), loading = ref(false), error = ref('')
let generation = 0
watch(() => props.evidence, page => {
  if (!page) return
  const previous = new Set(records.value.map(record => record.id))
  if (!page.voiceRecords.some(record => previous.has(record.id))) {
    // A missed live page may leave a gap. Restart the cursor from the fresh page.
    generation++
    records.value = page.voiceRecords
    before.value = page.voiceBefore
    loading.value = false
    error.value = ''
    return
  }
  records.value = [...records.value, ...page.voiceRecords.filter(record => !previous.has(record.id))]
}, { immediate: true })
onUnmounted(() => { generation++ })

async function loadOlder() {
  if (loading.value || before.value === null) return
  const run = generation
  loading.value = true
  error.value = ''
  try {
    const page = await api<ServerVoicePage>('/incidents/' + encodeURIComponent(props.incidentId)
      + '/voice?live_only=true&before=' + before.value)
    if (run !== generation) return
    records.value = [...page.voice_records, ...records.value]
    before.value = page.voice_before
  } catch {
    if (run === generation) error.value = '暂时无法读取更早记录，请重试。'
  } finally {
    if (run === generation) loading.value = false
  }
}

type VoiceGroup = { dialogue: boolean; entries: VoiceRecord[] }
const sections = computed(() => {
  const groups: VoiceGroup[] = []
  for (const record of records.value) {
    const group = groups.at(-1)
    const last = group?.entries.at(-1)
    const prompt = record.kind === 'question' || record.kind === 'playback_failed'
    const reply = record.kind === 'answer' || record.kind === 'no_answer'
    if (group?.dialogue && (reply || (prompt && (record.follow_up || last?.continues_round)))) {
      group.entries.push(record)
    } else {
      groups.push({ dialogue: prompt || reply, entries: [record] })
    }
  }
  groups.reverse()
  return [{ key: 'latest', groups: groups.slice(0, 1) }, { key: 'history', groups: groups.slice(1) }]
})
</script>

<template>
  <LiveThermalPreview :section-selected="sectionSelected" />
  <section class="content-section" aria-labelledby="incident-voice-title">
    <h2 id="incident-voice-title">语音记录</h2>
    <template v-if="records.length">
      <template v-for="part in sections" :key="part.key">
        <button
          v-if="part.key === 'history' && (part.groups.length || before !== null)"
          class="button secondary voice-history-toggle" type="button"
          :aria-expanded="historyOpen" aria-controls="incident-voice-history" @click="historyOpen = !historyOpen"
        >
          {{ historyOpen ? '收起较早记录' : '查看全部记录' }}<ChevronDown :size="18" aria-hidden="true" />
        </button>
        <div v-show="part.key === 'latest' || historyOpen" :id="'incident-voice-' + part.key" class="voice-groups">
          <component v-for="group in (part.key === 'latest' || historyOpen ? part.groups : [])"
            :is="group.dialogue || group.entries[0].text ? 'details' : 'div'" :key="group.entries[0].id"
            class="voice-group" :open="part.key === 'latest' && (group.dialogue || !!group.entries[0].text)">
            <component :is="group.dialogue || group.entries[0].text ? 'summary' : 'div'" class="voice-group-heading">
              <span>
                <strong v-if="group.dialogue">{{ part.key === 'latest' ? '最近一轮对话' : '核查对话' }}</strong>
                <strong v-else>{{ voiceLabels[group.entries[0].kind] }}</strong>
                <span v-if="!group.dialogue && group.entries[0].risk" class="voice-risk" :class="group.entries[0].risk">{{ riskLabels[group.entries[0].risk] }}</span>
                <span v-if="group.dialogue && part.key === 'history'" class="voice-count">{{ group.entries.length }} 条</span>
              </span>
              <time :datetime="group.entries[0].time">{{ displayTime(group.entries[0].time) }}</time>
              <ChevronDown v-if="group.dialogue || group.entries[0].text" :size="17" aria-hidden="true" />
            </component>
            <ol v-if="group.dialogue || group.entries[0].text" class="voice-records">
              <li v-for="record in group.entries" :key="record.id">
                <div v-if="group.dialogue">
                  <strong>{{ voiceLabels[record.kind] }}</strong>
                  <span v-if="group.dialogue && record.risk" class="voice-risk" :class="record.risk">{{ riskLabels[record.risk] }}</span>
                  <time :datetime="record.time">{{ displayTime(record.time) }}</time>
                </div>
                <p v-if="record.text">{{ record.text }}</p>
              </li>
            </ol>
          </component>
          <template v-if="part.key === 'history' && historyOpen">
            <button v-if="before !== null" class="button secondary voice-load" type="button" :disabled="loading" @click="loadOlder">
              {{ loading ? '正在读取…' : error ? '重试读取' : '加载更早记录' }}
            </button>
            <p v-if="error" class="form-error" role="status">{{ error }}</p>
          </template>
        </div>
      </template>
    </template>
    <p v-else>{{ evidence ? '此事件没有保存语音记录。' : '正在读取现场记录。' }}</p>
  </section>
</template>
