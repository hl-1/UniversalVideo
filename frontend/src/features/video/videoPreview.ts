export type PreviewFormat = {
  format_id: string
  ext?: string | null
  has_video?: boolean | null
}

export function downloadedPreviewUrl(file: { status: string; filename?: string | null; download_url?: string | null } | null): string | null {
  if (file?.status !== 'finished' || !/\.(mp4|webm)$/i.test(file.filename || '')) return null
  const url = file.download_url
  return typeof url === 'string' && /^\/api\/files\/[^/?#]+$/.test(url) ? url : null
}

export function selectPreviewFormat<T extends PreviewFormat>(formats: T[], selectedId: string): T | null {
  const candidates = formats.filter((item) => item.has_video !== false
    && (item.has_video === true || !['m4a', 'mp3', 'aac', 'wav', 'flac', 'opus', 'ogg'].includes(item.ext?.toLowerCase() || ''))
    && !item.format_id.startsWith('music:'))
  const selected = candidates.find((item) => item.format_id === selectedId)
  if (selected?.format_id.startsWith('sniffpair:')) return selected
  if (selected && !selected.format_id.startsWith('sniff:')) return selected
  return candidates.find((item) => item.format_id.startsWith('sniffpair:')) || selected || candidates[0] || null
}

export type PreviewStatus = {
  status?: 'preparing' | 'ready' | 'failed'
  status_url?: string | null
  preview_url?: string | null
  has_audio?: boolean | null
  message?: string
  alternate?: boolean
  delivery?: 'file' | 'proxy' | 'fragmented'
  complete?: boolean
  reused?: boolean
  timings_ms?: Record<string, number>
}

export function readPreviewStatusUrl(payload: PreviewStatus): string | null {
  const value = payload.status_url
  return typeof value === 'string' && /^\/api\/previews\/[A-Za-z0-9_-]+$/.test(value) ? value : null
}

export async function waitForPreview(
  initial: PreviewStatus,
  fetchStatus: (url: string, signal: AbortSignal) => Promise<PreviewStatus>,
  signal: AbortSignal,
  onUpdate: (status: PreviewStatus) => void,
  interval = 300,
): Promise<PreviewStatus> {
  let status = initial
  const statusUrl = readPreviewStatusUrl(initial)
  while (true) {
    signal.throwIfAborted()
    onUpdate(status)
    if (status.status === 'failed') throw new Error(status.message || '音视频准备失败，请重新解析。')
    if (!status.status || status.status === 'ready') {
      const url = readPreviewUrl(status)
      if (!url || (statusUrl && url !== `${statusUrl}/content`)) throw new Error('预览接口返回了无效地址。')
      return status
    }
    if (status.status !== 'preparing' || !statusUrl) throw new Error('预览接口返回了无效状态。')
    await new Promise<void>((resolve, reject) => {
      const abort = () => { clearTimeout(timer); reject(signal.reason) }
      const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, interval)
      signal.addEventListener('abort', abort, { once: true })
    })
    signal.throwIfAborted()
    status = await fetchStatus(statusUrl, signal)
  }
}

export async function monitorPreview(
  initial: PreviewStatus,
  fetchStatus: (url: string, signal: AbortSignal) => Promise<PreviewStatus>,
  signal: AbortSignal,
  interval = 2000,
): Promise<void> {
  const target = readPreviewStatusUrl(initial)
  if (!target) throw new Error('预览接口返回了无效地址。')
  let status = initial
  while (status.delivery === 'fragmented' && status.complete === false) {
    await new Promise<void>((resolve, reject) => {
      signal.throwIfAborted()
      const abort = () => { clearTimeout(timer); reject(signal.reason) }
      const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, interval)
      signal.addEventListener('abort', abort, { once: true })
    })
    signal.throwIfAborted()
    status = await fetchStatus(target, signal)
    if (status.status === 'failed') throw new Error(status.message || '视频准备中断，请重试播放。')
    if (status.status !== 'ready' || readPreviewUrl(status) !== `${target}/content`) throw new Error('视频预览状态无效。')
  }
}

export function readPreviewUrl(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object' || !('preview_url' in payload)) return null
  const value = payload.preview_url
  if (typeof value !== 'string') return null
  return /^\/api\/previews\/[A-Za-z0-9_-]+\/content$/.test(value) ? value : null
}
