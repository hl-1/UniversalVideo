export type VideoFormat = {
  format_id: string
  label: string
  ext?: string | null
  resolution?: string | null
  filesize?: number | null
  has_video?: boolean | null
}

export type ParseResult = {
  record_id?: string | null
  title: string
  thumbnail?: string | null
  duration?: number | null
  uploader?: string | null
  webpage_url?: string | null
  formats: VideoFormat[]
}

export type TaskStatus = 'pending' | 'running' | 'finished' | 'failed' | 'cancelled'
export type PreviewState = 'idle' | 'loading' | 'ready' | 'error'
export type SummaryTab = 'summary' | 'transcript' | 'mind-map'

export type TaskResult = {
  destination?: 'device' | 'library'
  task_id: string
  status: TaskStatus
  progress: number
  message: string
  filename?: string | null
  download_url?: string | null
  error?: string | null
}

export type SubtitleSegment = {
  start: number
  end?: number | null
  timestamp: string
  text: string
}

export type SummaryResult = {
  title: string
  webpage_url: string
  language: string
  source: string
  summary_markdown: string
  transcript: SubtitleSegment[]
  markdown_url?: string | null
  json_url?: string | null
}

export type SummaryTaskResult = {
  task_id: string
  status: TaskStatus
  progress: number
  message: string
  error?: string | null
  result?: SummaryResult | null
  timing?: import('./executionTiming').ExecutionTiming | null
  model_info?: import('./executionTiming').ModelInfo | null
  mind_map_task_id?: string | null
}

export type FilePickerWritable = {
  write(data: Blob | Uint8Array<ArrayBuffer>): Promise<void>
  close(): Promise<void>
  abort?(): Promise<void>
}

export type FilePickerHandle = {
  createWritable(): Promise<FilePickerWritable>
}

declare global {
  interface Window {
    showSaveFilePicker?: (options?: {
      suggestedName?: string
      startIn?: 'downloads'
      types?: Array<{
        description: string
        accept: Record<string, string[]>
      }>
    }) => Promise<FilePickerHandle>
  }
}
