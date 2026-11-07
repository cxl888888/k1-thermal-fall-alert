import type { VoiceRecord } from './types'

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}

export async function api<T>(path: string, body?: unknown, timeoutMs = 10000): Promise<T> {
  const controller = new AbortController(), timer = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch('/api' + path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal,
    })
    let data: any
    try { data = await response.json() } catch { throw new ApiError('当前地址没有可用的家属端服务，请检查设备提供的访问地址。', response.status) }
    if (!response.ok) throw new ApiError(typeof data.detail === 'string' ? data.detail : '请求未完成，请刷新后重试。', response.status)
    return data as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new ApiError('连接中断或请求超时，保留最后记录；请恢复连接后重试。', 0)
  } finally { window.clearTimeout(timer) }
}

export interface ServerVoicePage {
  voice_records: VoiceRecord[]
  voice_before: number | null
}

export interface ServerIncident extends Partial<ServerVoicePage> {
  id: string; device_id: string; version: number; updated_sequence: number; timeline_truncated: boolean; origin: 'live' | 'controlled_simulation' | 'historical_replay' | 'synthetic'
  title: string; route: string; reason: string; transcript: string | null; source: string; trigger_source?: string
  red_latched: boolean; archived: boolean; received_at: string; occurred_at: string | null; time_quality: string
  my_acknowledged: boolean; my_acknowledged_at: string | null
  my_review: { note: string } | null; my_reviewed_at: string | null
  resolve_request: { status: string; version: number; through_journal_id?: number } | null
  timeline: { title: string; detail: string; time: string }[]
}

export interface ContactState {
  phone: string | null
  can_propose: boolean
  pending: {
    id: number
    phone: string
    requester: string
    mine: boolean
    required: number
    approved: number
    i_approved: boolean
    can_approve: boolean
    can_apply: boolean
  } | null
}

export interface ServerDevice {
  device_id: string; origin: string; received_at: string; online: boolean; age_s: number
  sensor_status: Record<string, string>; interaction_state: string; source_gap: boolean
}
