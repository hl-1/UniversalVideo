export type LyricLine = { time: number; text: string }
export type Lyrics = { lines: LyricLine[]; plain: string; source: string; message?: string }

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
