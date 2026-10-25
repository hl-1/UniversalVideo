export type MusicListFilters = {
  artist: string
  query: string
  field: 'all' | 'title' | 'artist'
  match: 'fuzzy' | 'exact'
  sort: 'default' | 'newest' | 'oldest'
}

export type MusicListSong = { title: string; artist?: string | null; uploader?: string | null; list_time?: number | null }
export type MusicListAdapter<T> = {
  title?: (item: T) => string
  artist?: (item: T) => string
  time?: (item: T) => number | null | undefined
}

export function defaultMusicListFilters(): MusicListFilters {
  return { artist: '', query: '', field: 'all', match: 'fuzzy', sort: 'default' }
}

export function normalizeMusicText(value: string) {
  return value.normalize('NFKC').trim().toLocaleLowerCase()
}

// Split only explicit separators; names such as AC/DC remain intact.
export function musicArtists(value: string) {
  return value.split(/\s*(?:、|，|；|;|\s+\/\s+|\s+&\s+|\s+feat\.?\s+)\s*/i).map(name => name.trim()).filter(Boolean)
}

export function musicListAccessors<T extends MusicListSong>(adapter: MusicListAdapter<T> = {}) {
  return {
    title: adapter.title || ((item: T) => item.title),
    artist: adapter.artist || ((item: T) => item.artist || item.uploader || ''),
    time: adapter.time || ((item: T) => item.list_time),
  }
}

export function musicListTimestamp(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= 8.64e12 ? value : null
}

export function musicArtistOptions<T extends MusicListSong>(items: readonly T[], adapter: MusicListAdapter<T> = {}) {
  const { artist } = musicListAccessors(adapter)
  const names = new Map<string, string>()
  for (const item of items) {
    const artists = musicArtists(artist(item))
    for (const name of artists.length ? artists : ['未知歌手']) {
      const key = normalizeMusicText(name)
      if (!names.has(key)) names.set(key, name)
    }
  }
  return [...names.values()].sort((a, b) => a.localeCompare(b, 'zh-CN'))
}

export function filterMusicList<T extends MusicListSong>(items: readonly T[], filters: MusicListFilters, adapter: MusicListAdapter<T> = {}): T[] {
  const { title, artist, time } = musicListAccessors(adapter)
  const query = normalizeMusicText(filters.query)
  const selectedArtist = normalizeMusicText(filters.artist)
  const matches = (value: string) => filters.match === 'exact' ? normalizeMusicText(value) === query : normalizeMusicText(value).includes(query)
  const result = items.filter(item => {
    const artistText = artist(item)
    const artists = musicArtists(artistText)
    if (selectedArtist && !(artists.length ? artists : ['未知歌手']).some(name => normalizeMusicText(name) === selectedArtist)) return false
    if (!query) return true
    const titleMatch = filters.field !== 'artist' && matches(title(item))
    const artistMatch = filters.field !== 'title' && (matches(artistText) || artists.some(matches))
    return titleMatch || artistMatch
  })
  if (filters.sort === 'default') return result
  return result.map((item, index) => ({ item, index, time: musicListTimestamp(time(item)) }))
    .sort((a, b) => {
      if (a.time === null) return b.time === null ? a.index - b.index : 1
      if (b.time === null) return -1
      return (filters.sort === 'newest' ? b.time - a.time : a.time - b.time) || a.index - b.index
    }).map(entry => entry.item)
}

export function formatMusicListTime(value: number | null | undefined) {
  const time = musicListTimestamp(value)
  return time === null ? '—' : new Date(time * 1000).toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}
