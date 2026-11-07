export type Severity = 'urgent' | 'assistance'
export type IncidentSource = 'voice' | 'thermal' | 'radar' | 'other'
export interface TimelineItem { title: string; detail: string; time: string }
export interface VoiceRecord {
  id: string
  kind: 'utterance' | 'question' | 'answer' | 'no_answer' | 'playback_failed' | 'thermal_candidate'
  text?: string
  time: string
  risk?: 'thermal_candidate' | 'self_reported_fall' | 'emergency_help'
  follow_up?: boolean
  continues_round?: boolean
}
export interface IncidentEvidence {
  version: number
  voiceRecords: VoiceRecord[]
  voiceBefore: number | null
}
export interface Incident {
  id: string
  title: string
  severity: Severity
  time: string
  location: string
  reason: string
  transcript: string
  source: string
  sourceKind: IncidentSource
  redLatched: boolean
  archived: boolean
  timeline: TimelineItem[]
  origin?: 'live' | 'controlled_simulation' | 'historical_replay' | 'synthetic'
  version?: number
  route?: string
  receivedAt?: string
  updatedSequence?: number
  timelineTruncated?: boolean
  evidence?: IncidentEvidence
  resolveRequest?: { status: string; version: number; through_journal_id?: number }
}
export interface IncidentReview {
  acknowledged: boolean
  acknowledgedAt: string
  submitted: boolean
  submittedAt: string
  note: string
}
