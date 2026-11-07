import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { describeSource } from '../incidentSource'
import type { IncidentReview, Incident } from '../types'
import { api, ApiError, type ServerDevice, type ServerIncident } from '../api'

const emptyReview = (): IncidentReview => ({ acknowledged: false, acknowledgedAt: '', submitted: false, submittedAt: '', note: '' })
// getRandomValues is also available in ordinary LAN HTTP contexts, where
// crypto.randomUUID may be restricted to HTTPS; never fall back to Math.random.
const newRequestId = () => {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return 'r-' + Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
}
const displayTime = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value)) : '未保存时间'
const phoneKey = 'k1-family-contact-phone'
const validPhone = (value?: string | null) => typeof value === 'string' && /^\+?[0-9]{3,20}$/.test(value) ? value : ''

function readLocalPhone() {
  try { return validPhone(localStorage.getItem(phoneKey)) }
  catch { return '' }
}

export const usePortalStore = defineStore('portal', () => {
  const backendAvailable = !!document.querySelector('meta[name="family-api"][content="same-origin"]')
    && !document.querySelector('meta[name="family-demo"]')
  const directAccess = !!document.querySelector('meta[name="family-access"][content="direct"]')
  const backendConnected = ref(false), paired = ref(false), busy = ref(false), error = ref('')
  const clientLabel = ref(''), servicePhone = ref(''), localPhone = ref(directAccess ? readLocalPhone() : '')
  const contactPhone = computed(() => directAccess ? localPhone.value || servicePhone.value : servicePhone.value)
  const lastSync = ref(''), total = ref(0), nextOffset = ref<number | null>(null)
  const serviceReviews = ref<Record<string, IncidentReview>>({})
  const serviceIncidents = ref<Incident[]>([]), devices = ref<ServerDevice[]>([])
  const isService = computed(() => paired.value)
  const incidents = computed(() => paired.value ? serviceIncidents.value : [])
  const active = computed(() => incidents.value.filter(event => !event.archived))
  const history = computed(() => incidents.value.filter(event => event.archived))
  const urgent = computed(() => active.value.find(event => event.redLatched))
  const tick = ref(performance.now()), devicesReadAt = ref(performance.now())
  function deviceFresh(device?: ServerDevice) { return !!device && backendConnected.value && device.origin === 'live' && device.online && device.age_s+(tick.value-devicesReadAt.value)/1000<=15 }
  const deviceOnline = computed(() => devices.value.some(deviceFresh))
  let stream: EventSource | null = null, refreshTimer: number | null = null, reconnectTimer: number | null = null
  let cursor = 0, observedCursor = 0, syncingEpoch: number | null = null, repeatSync = false, epoch = 0, pageEnd = 0
  const pending = new Map<string, string>(), received = new Set<string>()
  const dirtyIncidents = new Map<string, number>()
  let tickTimer: number | null = null

  function stopUpdates() {
    stream?.close(); stream = null
    if (refreshTimer !== null) window.clearInterval(refreshTimer)
    if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
    if (tickTimer !== null) window.clearInterval(tickTimer)
    refreshTimer = reconnectTimer = null
    tickTimer = null
  }
  function clearServiceState() {
    serviceIncidents.value = []; serviceReviews.value = {}; devices.value = []
    received.clear(); pending.clear(); cursor = observedCursor = pageEnd = 0
    dirtyIncidents.clear()
    syncingEpoch = null; repeatSync = false
    total.value = 0; nextOffset.value = null; clientLabel.value = ''; lastSync.value = ''
    servicePhone.value = ''; backendConnected.value = false
  }
  function failed(reason: unknown, run = epoch) {
    if (run !== epoch) return
    backendConnected.value = false; error.value = reason instanceof Error ? reason.message : '连接中断'
    if (reason instanceof ApiError && reason.status === 401) {
      epoch++; stopUpdates(); paired.value = false; busy.value = false
      clearServiceState(); syncingEpoch = null; repeatSync = false
      if (directAccess) {
        error.value = ''
        reconnectTimer = window.setTimeout(() => { void restoreSession() }, 250)
      }
    }
  }
  function mapIncident(item: ServerIncident): Incident {
    const source = describeSource(item.trigger_source ?? item.source)
    return { id: item.id, title: item.title, severity: item.red_latched ? 'urgent' : 'assistance',
      time: displayTime(item.received_at), receivedAt: item.received_at, location: item.device_id, reason: item.reason || '来源未保存原因',
      transcript: item.transcript || '', source: source.label, sourceKind: source.kind,
      redLatched: item.red_latched, archived: item.archived, origin: item.origin, version: item.version,
      updatedSequence: item.updated_sequence, timelineTruncated: item.timeline_truncated,
      route: item.route, resolveRequest: item.resolve_request || undefined,
      timeline: item.timeline.map(row => ({ ...row, time: displayTime(row.time) })) }
  }
  function mergeItems(items: ServerIncident[], withEvidence = false) {
    const values = new Map(serviceIncidents.value.map(item => [item.id, item]))
    for (const item of items) {
      if (item.origin !== 'live') continue
      const previous = values.get(item.id)
      if (previous && ((previous.version || 0) > item.version || (previous.version === item.version && (previous.updatedSequence || 0) > item.updated_sequence))) continue
      const incident = mapIncident(item)
      incident.evidence = withEvidence ? {
        version: item.version,
        voiceRecords: item.voice_records
          ?? (incident.transcript ? [{ id: item.id, kind: 'utterance', text: incident.transcript, time: item.received_at }] : []),
        voiceBefore: item.voice_before ?? null,
      } : previous?.evidence
      values.set(item.id, incident)
      serviceReviews.value[item.id] = {
        acknowledged: item.my_acknowledged, acknowledgedAt: item.my_acknowledged_at ? displayTime(item.my_acknowledged_at) : '',
        submitted: !!item.my_review, submittedAt: item.my_reviewed_at ? displayTime(item.my_reviewed_at) : '', note: item.my_review?.note || '',
      }
    }
    serviceIncidents.value = [...values.values()].sort((a,b) => (b.receivedAt || '').localeCompare(a.receivedAt || ''))
  }
  async function refresh() {
    if (!paired.value || !isService.value) return
    const run = epoch
    if (syncingEpoch === run) { repeatSync = true; return }
    syncingEpoch = run
    const target = observedCursor
    const requestStarted = performance.now()
    try {
      const [list, status, client] = await Promise.all([
        api<{ items: ServerIncident[]; total: number; next_offset: number | null }>('/incidents?limit=200&live_only=true'),
        api<{ items: ServerDevice[] }>('/devices'),
        api<{ contact_phone?: string | null }>('/me'),
      ])
      if (run !== epoch || !isService.value || !paired.value) return
      mergeItems(list.items); devices.value = status.items; devicesReadAt.value = requestStarted
      updateContactPhone(client.contact_phone)
      total.value = list.total
      pageEnd = Math.max(pageEnd, list.items.length)
      nextOffset.value = pageEnd < list.total ? pageEnd : null
      backendConnected.value = true; error.value = ''; lastSync.value = displayTime(new Date().toISOString()); cursor = target
      for (const item of list.items) {
        if (run !== epoch) return
        if (item.origin !== 'live') continue
        const key = `${item.id}:${item.version}`
        if (!received.has(key)) {
          await api(`/incidents/${item.id}/received`, { request_id: newRequestId(), expected_version: item.version })
          if (run !== epoch) return
          received.add(key)
        }
      }
      for (const id of [...dirtyIncidents.keys()].slice(0,20)) {
        if (run !== epoch) return
        await loadIncident(id, true)
      }
    } catch (reason) { failed(reason, run) }
    finally {
      if (run === epoch && syncingEpoch === run) {
        syncingEpoch = null
        if (repeatSync) { repeatSync = false; void refresh() }
      }
    }
  }
  function connectStream() {
    if (!paired.value || !isService.value) return
    const run = epoch
    stream?.close(); const source = new EventSource(`/api/stream?after=${cursor}`, { withCredentials: true }); stream = source
    source.onopen = () => { if (run === epoch) void refresh() }
    source.onmessage = event => {
      if (run !== epoch) return
      const value = Number(event.lastEventId)
      if (Number.isSafeInteger(value) && value > cursor) {
        observedCursor = Math.max(observedCursor, value)
        try {
          const change = JSON.parse(event.data)
          if (typeof change.incident_id === 'string' && serviceIncidents.value.some(item => item.id === change.incident_id)) dirtyIncidents.set(change.incident_id, Math.max(dirtyIncidents.get(change.incident_id) || 0, value))
        } catch { failed(new Error('事件更新格式异常，正在重新读取服务记录。'), run) }
        void refresh()
      }
    }
    source.addEventListener('session_expired', () => failed(new ApiError('配对会话已过期或被撤销，请重新配对。', 401), run))
    source.onerror = () => {
      source.close(); if (run !== epoch) return
      backendConnected.value = false; error.value = '连接中断，正在重连；显示的是最后保存记录。'
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      reconnectTimer = window.setTimeout(() => { if (run === epoch) connectStream() }, 2500)
    }
  }
  function beginUpdates() {
    stopUpdates(); connectStream()
    refreshTimer = window.setInterval(() => { void refresh() }, 5000)
    tickTimer = window.setInterval(() => { tick.value = performance.now() }, 1000)
  }
  async function loadMore() {
    if (!paired.value || nextOffset.value === null || busy.value) return
    const run = epoch; busy.value = true
    try {
      const list = await api<{ items: ServerIncident[]; total: number; next_offset: number | null }>(`/incidents?limit=200&live_only=true&offset=${nextOffset.value}`)
      if (run !== epoch || !paired.value || !isService.value) return
      mergeItems(list.items); total.value = list.total; nextOffset.value = list.next_offset
      pageEnd = list.next_offset ?? list.total
    } catch (reason) { failed(reason, run) } finally { if (run === epoch) busy.value = false }
  }
  async function loadIncident(id: string, force = false) {
    if (!paired.value) return
    const current = serviceIncidents.value.find(item => item.id === id)
    if (!force && current?.evidence && current.evidence.version === current.version) return
    const run = epoch
    if (force && !dirtyIncidents.has(id)) dirtyIncidents.set(id, 0)
    try {
      const item = await api<ServerIncident>('/incidents/'+encodeURIComponent(id)+'?live_only=true')
      if (run === epoch && paired.value && isService.value) {
        mergeItems([item], true)
        if ((dirtyIncidents.get(id) || 0) <= item.updated_sequence) dirtyIncidents.delete(id)
      }
    } catch (reason) {
      if (run === epoch && reason instanceof ApiError && reason.status === 404) {
        dirtyIncidents.delete(id)
        return
      }
      failed(reason, run)
    }
  }
  async function restoreSession() {
    if (!backendAvailable) return
    const run = epoch
    try {
      if (directAccess) {
        error.value = ''
        await api('/direct-session', {})
      }
      const client = await api<{ label: string; contact_phone?: string | null }>('/me')
      if (run !== epoch) return
      updateContactPhone(client.contact_phone)
      clientLabel.value = client.label; paired.value = true
      await refresh(); if (run === epoch && paired.value) beginUpdates()
    } catch (reason) {
      if (directAccess && !paired.value && run === epoch) {
        error.value = '无法连接家属端，请重试。'
      } else if (!(reason instanceof ApiError && reason.status === 401) || paired.value) {
        failed(reason, run)
      }
    }
  }
  function updateContactPhone(phone?: string | null) {
    servicePhone.value = validPhone(phone)
  }
  function saveLocalPhone(phone: string) {
    const value = validPhone(phone)
    if (!value) return '请输入有效的联系电话。'
    try {
      localStorage.setItem(phoneKey, value)
      localPhone.value = value
      return ''
    } catch {
      return '浏览器无法保存号码，请检查网站存储权限。'
    }
  }
  async function pair(code: string) {
    if (busy.value) return '正在处理，请稍候。'
    const run = ++epoch; stopUpdates(); clearServiceState(); paired.value = false
    busy.value = true; error.value = ''
    try { await api('/pair', { code }); if (run !== epoch) return '配对已取消。'; await restoreSession(); return '' }
    catch (reason) { failed(reason, run); return reason instanceof Error ? reason.message : '配对未完成。' }
    finally { if (run === epoch) busy.value = false }
  }
  async function logout() {
    if (busy.value) return
    const run = ++epoch; stopUpdates()
    busy.value = true
    try {
      await api('/logout', {}); if (run !== epoch) return
      paired.value = false; clearServiceState()
    } catch (reason) { failed(reason, run); if (run === epoch && paired.value) beginUpdates() } finally { if (run === epoch) busy.value = false }
  }
  function reviewFor(id: string): IncidentReview { return serviceReviews.value[id] || emptyReview() }
  function canOperate(id: string) { return paired.value && backendConnected.value && active.value.some(event => event.id === id) }
  async function operate(id: string, kind: string, extra: Record<string, unknown> = {}) {
    const event = incidents.value.find(item => item.id === id)
    if (!event || !canOperate(id)) return '服务尚未连接或事件已归档，操作未提交。'
    const key = JSON.stringify([id, kind, event.version, extra]), requestId = pending.get(key) || newRequestId()
    const run = epoch; pending.set(key, requestId); busy.value = true
    try {
      await api(`/incidents/${id}/${kind}`, { request_id: requestId, expected_version: event.version, ...extra })
      if (run !== epoch) return '会话已切换，操作结果未写入当前页面。'
      pending.delete(key); await loadIncident(id, true); await refresh(); return ''
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '操作未完成'
      if (reason instanceof ApiError && reason.status === 409) {
        pending.delete(key)
        if (run === epoch) {
          await loadIncident(id, true)
          await refresh()
        }
        return message === '事件已更新，请刷新后核查' ? '本次操作未提交，请核对当前事件后重试。' : message
      }
      failed(reason, run)
      return message
    } finally { if (run === epoch) busy.value = false }
  }
  async function acknowledge(id: string) {
    if (!canOperate(id)) return false
    return !(await operate(id, 'acknowledge'))
  }
  async function submitReview(id: string, note: string, humanChecked: boolean) {
    if (!canOperate(id) || !reviewFor(id).acknowledged) return '请先确认收到。'
    if (!note.trim() || note.length > 1000) return '请填写1至1000字核查说明。'
    if (!humanChecked) return '请勾选已完成人工核查。'
    return operate(id, 'review', { note: note.trim(), human_checked: true })
  }
  async function requestResolve(id: string) {
    const event = incidents.value.find(item => item.id === id)
    if (!isService.value || event?.origin !== 'live' || !event.redLatched) return '仅真实锁存告警可提交解除申请。'
    if (!reviewFor(id).submitted) return '请先保存当前事件版本的核查说明。'
    return operate(id, 'resolve_request', { note: reviewFor(id).note, human_checked: true })
  }
  return { isService, backendAvailable, directAccess, backendConnected, paired, busy, error, clientLabel, contactPhone, lastSync,
    incidents, active, history, urgent, devices, deviceOnline, deviceFresh, total, nextOffset,
    reviewFor, acknowledge, submitReview, requestResolve, restoreSession, pair, logout, refresh, loadMore, loadIncident, updateContactPhone, saveLocalPhone }
})
