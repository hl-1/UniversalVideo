export type PreviewFormat = {
  format_id: string
  ext?: string | null
}

export function selectPreviewFormat<T extends PreviewFormat>(formats: T[], selectedId: string): T | null {
  const mp4Formats = formats.filter((item) => item.ext?.toLowerCase() === 'mp4')
  return mp4Formats.find((item) => item.format_id === selectedId) || mp4Formats[0] || null
}

export function readPreviewUrl(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object' || !('preview_url' in payload)) return null
  const value = payload.preview_url
  if (typeof value !== 'string') return null
  return /^\/api\/previews\/[A-Za-z0-9_-]+\/content$/.test(value) ? value : null
}
