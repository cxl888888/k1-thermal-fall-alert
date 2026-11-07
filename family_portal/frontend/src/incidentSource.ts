import { MessageSquare, Mic, Radio, Thermometer } from '@lucide/vue'
import type { IncidentSource } from './types'

export const incidentSources = {
  voice: { label: '语音', icon: Mic },
  thermal: { label: '热像', icon: Thermometer },
  radar: { label: '毫米波雷达', icon: Radio },
  other: { label: '其他', icon: MessageSquare },
} satisfies Record<IncidentSource, { label: string; icon: typeof Mic }>

export function describeSource(source: string): { kind: IncidentSource; label: string } {
  switch (source) {
    case 'kws_live': return { kind: 'voice', label: '关键词快速预警' }
    case 'llm': return { kind: 'voice', label: '语音语义识别' }
    case 'voice_runtime': return { kind: 'voice', label: '语音系统' }
    case 'thermal': return { kind: 'thermal', label: '热像传感器' }
    case 'radar': return { kind: 'radar', label: '毫米波雷达' }
    default: return { kind: 'other', label: source || '来源未记录' }
  }
}
