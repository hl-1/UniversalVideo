import type { ParseResult, VideoFormat } from './types'

export function matchRefreshedFormat(formats: VideoFormat[], selected?: VideoFormat): VideoFormat | null {
  if (!selected) return null
  const exact = formats.find(item => item.format_id === selected.format_id && !item.format_id.startsWith('saved:'))
  if (exact) return exact
  const candidates = formats.filter(item => {
    if (selected.ext && item.ext?.toLowerCase() !== selected.ext.toLowerCase()) return false
    if (selected.has_video != null && item.has_video != null && selected.has_video !== item.has_video) return false
    if (selected.resolution && item.resolution && selected.resolution !== item.resolution) return false
    if (selected.format_id.startsWith('sniffpair:') && !item.format_id.startsWith('sniffpair:')) return false
    if (selected.format_id.startsWith('sniff:') && !item.format_id.startsWith('sniff:')) return false
    return true
  })
  const labelled = candidates.filter(item => item.label === selected.label)
  if (labelled.length === 1) return labelled[0]!
  const resolved = selected.resolution ? candidates.filter(item => item.resolution === selected.resolution) : []
  return resolved.length === 1 ? resolved[0]! : null
}

export function refreshDownloadFormats(current: ParseResult, fresh: ParseResult, selectedId: string) {
  const selected = current.formats.find(item => item.format_id === selectedId)
  const matched = matchRefreshedFormat(fresh.formats, selected)
  if (selectedId && !matched) throw new Error('原先选择的下载格式已失效，请重新解析并选择格式。')
  return {
    result: { ...current, formats: fresh.formats },
    selectedId: selectedId ? matched!.format_id : '',
  }
}
