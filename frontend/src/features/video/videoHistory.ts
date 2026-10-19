export type VideoHistoryRow = {
  record_id: string
  url: string
  title: string
  thumbnail?: string | null
  duration?: number | null
  platform: string
  status: string
  error?: string | null
  created_at: number
  has_summary: boolean
  has_mind_map: boolean
}

export type VideoTaskReference = {
  kind: 'summary' | 'mind_map' | 'download'
  task_id: string
  status: string
  version: number
  summary_task_id?: string
  filename?: string | null
  download_url?: string | null
  created_at: number
}

export type MapView = { scale: number; x: number; y: number; collapsed: string[] }
export type VideoView = {
  tab: 'summary' | 'transcript' | 'mind-map'
  summary_task_id: string | null
  mind_map_id: string | null
  selected_format: string
  playback_time: number
  map_view: MapView | null
}

export function recordLocation(recordId: string, tab = 'summary') {
  return `#video/${encodeURIComponent(recordId)}/${tab}`
}

export function readRecordLocation(hash: string): { recordId: string; tab: VideoView['tab'] } | null {
  const match = /^#video\/([a-f0-9]{12})\/(summary|transcript|mind-map)$/.exec(hash)
  return match ? { recordId: match[1]!, tab: match[2] as VideoView['tab'] } : null
}
