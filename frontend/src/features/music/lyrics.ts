export type LyricLine = { time: number; text: string }
export type Lyrics = { lines: LyricLine[]; plain: string; source: string; message?: string; manual?: boolean }

export function parseImportedLrc(text: string): Lyrics {
  const offset = Number(text.match(/\[offset:([+-]?\d+)\]/i)?.[1] || 0) / 1000
  const grouped = new Map<number, string[]>()
  for (const row of text.split(/\r?\n/)) {
    const tags = [...row.matchAll(/\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]/g)]
    const content = row.replace(/\[[^\]]*\]/g, '').trim()
    for (const tag of tags) {
      if (Number(tag[2]) >= 60) continue
      const time = Math.max(0, Math.round((Number(tag[1]) * 60 + Number(tag[2]) + Number(`0.${tag[3] || 0}`) + offset) * 1000) / 1000)
      const lines = grouped.get(time) || []
      if (!lines.includes(content)) lines.push(content)
      grouped.set(time, lines)
    }
  }
  const lines = [...grouped].sort(([left], [right]) => left - right).map(([time, texts]) => ({ time, text: texts.join('\n') }))
  if (!lines.some(line => line.text)) throw new Error('没有找到有效的 LRC 时间标签和歌词。')
  return { lines, plain: '', source: '本地 LRC', manual: true }
}

export function activeLyricIndex(lines: LyricLine[], seconds: number): number {
  let low = 0
  let high = lines.length - 1
  let result = -1
  while (low <= high) {
    const middle = (low + high) >>> 1
    if (lines[middle].time <= seconds) { result = middle; low = middle + 1 }
    else high = middle - 1
  }
  return result
}

export function audioFilename(url?: string | null): string {
  if (!url) return ''
  try {
    const path = new URL(url, 'http://localhost').pathname
    return path.startsWith('/api/files/') ? decodeURIComponent(path.slice('/api/files/'.length)) : ''
  } catch { return '' }
}
