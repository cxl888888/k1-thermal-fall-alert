<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowLeft, Check, ClipboardCheck, LockKeyhole, Siren, MessageSquare, CircleCheck, ArrowRight, Phone } from '@lucide/vue'
import { usePortalStore } from '../stores/portal'
import IncidentEvidence from '../components/IncidentEvidence.vue'
const portal = usePortalStore(), route = useRoute()
const id = computed(() => typeof route.params.id === 'string' ? route.params.id : '')
const event = computed(() => portal.incidents.find(item => item.id === id.value))
const review = computed(() => portal.reviewFor(id.value))
const note = ref(''), checked = ref(false), error = ref('')
const sections = [{ key: 'overview', label: '概况' }, { key: 'evidence', label: '现场记录' }, { key: 'handling', label: '处置' }] as const
const section = ref<typeof sections[number]['key']>('overview')
const detailTabs = ref<HTMLElement | null>(null)
const evidencePanel = ref<HTMLElement | null>(null)
let aligning = false
let alignObserver: ResizeObserver | null = null
let alignTimer: ReturnType<typeof setTimeout> | null = null
const quickNotes = ['已电话联系', '已到场查看', '已安排家人查看']
const noteInput = ref<HTMLTextAreaElement | null>(null)
const withQuickNote = (text: string) => [note.value.trimEnd(), text].filter(Boolean).join('\n')
function addQuickNote(text: string) {
  if (!note.value.split('\n').includes(text)) note.value = withQuickNote(text)
  noteInput.value?.focus()
}
const canResolveOnDevice = !!document.querySelector('meta[name="family-resolution"][content="enabled"]')
const pending = computed(() => {
  const request = event.value?.resolveRequest
  return request?.version === event.value?.version && request?.status === 'pending_device_validation'
    && (!canResolveOnDevice || !!request.through_journal_id)
})
watch(id, () => { stopAligning(); note.value = portal.reviewFor(id.value).note; checked.value = false; error.value = ''; section.value = 'overview' }, { immediate: true })
watch([id, () => portal.paired, () => event.value?.version], () => { if(id.value) void portal.loadIncident(id.value) }, { immediate: true })
watch(() => event.value?.version, (next, previous) => {
  if (next && previous && next !== previous) {
    checked.value = false
    error.value = ''
  }
})
async function submit() { error.value = await portal.submitReview(id.value, note.value, checked.value) }
async function resolve() { error.value = await portal.requestResolve(id.value) }
function stopAligning() {
  aligning = false
  alignObserver?.disconnect()
  alignObserver = null
  if (alignTimer) { clearTimeout(alignTimer); alignTimer = null }
}
function alignToTabs() {
  if (!aligning || section.value !== 'evidence') return
  const tabs = detailTabs.value
  if (!tabs) return
  // 语音记录在切换后才到达，页面会先被撑高再变高；每次内容变化都重新对齐。
  if (tabs.getBoundingClientRect().top > 14) tabs.scrollIntoView({ block: 'start' })
}
async function selectSection(key: typeof section.value) {
  stopAligning()
  section.value = key
  if (key !== 'evidence') {
    if (key === 'overview' && scrollY > 0 && matchMedia('(max-width: 760px)').matches) window.scrollTo({ top: 0 })
    return
  }
  aligning = true
  await nextTick()
  alignToTabs()
  requestAnimationFrame(alignToTabs)
  if (evidencePanel.value) {
    alignObserver = new ResizeObserver(alignToTabs)
    alignObserver.observe(evidencePanel.value)
  }
  window.addEventListener('wheel', stopAligning, { passive: true, once: true })
  window.addEventListener('touchstart', stopAligning, { passive: true, once: true })
  alignTimer = setTimeout(stopAligning, 3000)
}
onUnmounted(stopAligning)
async function continueReview() {
  stopAligning()
  section.value = 'handling'
  await nextTick()
  const heading = document.getElementById('handling-title')
  heading?.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
  heading?.focus({ preventScroll: true })
}
</script>

<template>
  <RouterLink class="back-link" to="/events"><ArrowLeft :size="18" />返回事件</RouterLink>
  <template v-if="event">
    <div class="detail-heading" :class="{ compact: section !== 'overview' }">
      <span class="state-tag" :class="event.redLatched ? 'urgent' : 'assistance'"><Siren v-if="event.redLatched" :size="15" /><MessageSquare v-else :size="15" />{{ event.redLatched ? '告警保持' : event.archived ? (event.resolveRequest?.status === 'resolved' ? '告警已解除' : '历史记录') : review.submitted ? '核查已记录' : '消息待核查' }}</span>
      <h1>{{ event.title }}</h1><p>{{ event.time }} · {{ event.location }} · 服务接收时间</p>
      <div v-if="!event.archived" class="detail-contact">
        <template v-if="portal.contactPhone">
          <a class="button secondary" :href="'tel:' + portal.contactPhone"><Phone :size="17" />打开手机拨号</a>
          <RouterLink class="text-action" to="/device/contact">修改号码</RouterLink>
        </template>
        <RouterLink v-else class="button secondary" to="/device/contact">设置联系电话</RouterLink>
      </div>
      <div v-if="!event.archived" class="detail-quick-action">
        <button v-if="!review.acknowledged" class="button primary full-width" :disabled="portal.busy || !portal.backendConnected" @click="portal.acknowledge(id)">确认收到<Check :size="18" /></button>
        <button v-else-if="!review.submitted" class="button secondary full-width" @click="continueReview">继续核查情况<ArrowRight :size="18" /></button>
        <p class="footnote" role="status">{{ review.submitted ? '核查说明已记录，设备告警状态未改变。' : review.acknowledged ? '已确认收到，具体情况仍待核查。' : '确认仅记录你已看到，不自动解除告警。' }}</p>
      </div>
    </div>
    <div ref="detailTabs" class="detail-tabs segmented" role="group" aria-label="事件详情">
      <button v-for="item in sections" :key="item.key" type="button" :aria-pressed="section === item.key" @click="selectSection(item.key)">{{ item.label }}</button>
    </div>
    <div class="detail-grid">
      <div class="detail-main">
        <div class="detail-section" :class="{ 'mobile-visible': section === 'overview' }">
          <section class="content-section"><h2>发生了什么</h2><p>{{ event.reason }}</p>
            <dl class="fact-list"><div><dt>消息来源</dt><dd>{{ event.source }}</dd></div><div><dt>设备告警</dt><dd :class="{ 'danger-text': event.redLatched }">{{ event.redLatched ? '保持，不随确认自动解除' : '本事件未设置红色锁存' }}</dd></div><div><dt>当前手机确认</dt><dd>{{ review.acknowledged ? '已确认收到' : '尚未确认' }}</dd></div></dl>
          </section>
          <section class="content-section"><h2>处理进展</h2><ol class="timeline">
            <li v-for="(item, index) in event.timeline" :key="index"><span class="timeline-mark"><Check :size="13" /></span><div><div class="timeline-title"><h3>{{ item.title }}</h3><time>{{ item.time }}</time></div><p>{{ item.detail }}</p></div></li>
          </ol><p v-if="event.timelineTruncated" class="footnote">本页显示近期进展。</p></section>
        </div>
        <div ref="evidencePanel" class="detail-section" :class="{ 'mobile-visible': section === 'evidence' }"><IncidentEvidence :key="event.id" :incident-id="event.id" :evidence="event.evidence" :section-selected="section === 'evidence'" /></div>
      </div>
      <aside class="handling-panel detail-section" :class="{ 'mobile-visible': section === 'handling' }" aria-labelledby="handling-title">
        <div class="section-heading"><h2 id="handling-title" tabindex="-1">{{ event.archived ? '处置记录' : '确认与处置' }}</h2><ClipboardCheck :size="21" /></div>
        <template v-if="!event.archived">
          <p>先确认收到，再核查当事人的实际情况。</p>
          <button v-if="!review.acknowledged" class="button primary full-width desktop-ack" :disabled="portal.busy || !portal.backendConnected" @click="portal.acknowledge(id)">确认收到<Check :size="18" /></button>
          <div v-else class="inline-success" role="status"><CircleCheck :size="18" /><span>{{ review.submitted ? '核查说明已记录' : '已确认收到' }}<span>{{ event.redLatched ? '告警仍保持' : '以实际核查情况为准' }}</span></span></div>
          <form v-if="review.acknowledged && !review.submitted" class="handling-form" @submit.prevent="submit">
            <label for="review-note">核查情况</label>
            <div class="quick-notes" role="group" aria-label="快捷核查说明">
              <button v-for="text in quickNotes" :key="text" type="button" class="button secondary" :disabled="portal.busy || withQuickNote(text).length > 1000" @click="addQuickNote(text)">{{ text }}</button>
            </div>
            <textarea id="review-note" ref="noteInput" v-model="note" maxlength="1000" rows="4" placeholder="记录联系、现场核查及安排帮助的实际情况。" :disabled="portal.busy" :aria-invalid="!!error" :aria-describedby="error ? 'review-error' : 'review-note-hint'"></textarea>
            <p id="review-note-hint" class="footnote">可编辑快捷说明，核对后再保存。</p>
            <label class="check-label"><input v-model="checked" type="checkbox" :disabled="portal.busy" />已完成本次人工核查</label>
            <p v-if="error" id="review-error" class="form-error" role="alert">{{ error }}</p>
            <button class="button primary full-width" type="submit" :disabled="portal.busy || !portal.backendConnected">{{ portal.busy ? '正在保存…' : '保存核查说明' }}<ArrowRight :size="18" /></button>
          </form>
          <div v-if="review.submitted" class="saved-review" role="status"><h3>核查说明已记录</h3><p>{{ review.note }}</p><p class="footnote">保存说明不改变设备告警。</p></div>
          <div class="resolve-info">
            <button class="button secondary full-width" :disabled="event.origin !== 'live' || !event.redLatched || !review.submitted || portal.busy || !portal.backendConnected || pending" @click="resolve"><LockKeyhole :size="17" />{{ canResolveOnDevice ? (pending ? '等待设备确认' : '解除告警') : (pending ? '申请待板端处理' : '记录解除申请') }}</button>
            <p class="footnote">{{ event.resolveRequest?.status === 'stale' ? '核查后出现新风险，请重新确认并核查。' : canResolveOnDevice ? (pending ? '设备确认后，告警将归档并恢复监测。' : '确认险情已处理后，可解除这条告警。') : '申请保存为待处理事项，解除状态以板端处理结果为准。' }}</p>
            <p v-if="error && review.submitted" class="form-error" role="alert">{{ error }}</p>
          </div>
        </template>
        <p v-else>此事件由来源标记为已归档，处置与消息来源保留在时间线中。</p>
      </aside>
    </div>
  </template>
  <section v-else class="page-empty"><MessageSquare :size="34" /><h1>当前无法查看这条事件</h1><p>请检查设备连接，或返回事件列表重新打开。</p><RouterLink class="button primary" to="/events">返回事件列表</RouterLink></section>
</template>
