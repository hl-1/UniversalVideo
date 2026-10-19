import { api, jsonRequest } from '../../shared/api/client'
import { ref } from 'vue'

export type MusicTrack = {
  id?: string
  title: string
  artist?: string | null
  uploader?: string | null
  thumbnail?: string | null
  automatic_thumbnail?: string | null
  custom_thumbnail?: string | null
  file_available?: boolean
  duration?: number | null
  webpage_url: string
  provider?: string
  identifier?: string
  token?: string
  access?: string
  local_url?: string | null
  filename?: string
  liked?: boolean
  played_at?: number
  preview?: boolean
  formats?: Array<{ format_id: string }>
}
export type Playlist = { id: string; name: string; description: string; count: number; thumbnail?: string | null }
export type Profile = { nickname: string; bio: string; avatar: string | null }
export const profile = ref<Profile>({ nickname: '音乐听众', bio: '', avatar: null })
export const playlists = ref<Playlist[]>([])
export const counts = ref({ favorites: 0, history: 0, downloads: 0, trials: 0 })
export const libraryError = ref('')
export const libraryRevision = ref(0)
export const likedKeys = ref(new Set<string>())
export const trackArtworkMap = ref(new Map<string, string | null>())
const favoriteBusy = new Set<string>()
let refreshRevision = 0
export function resetLibrary() {
  refreshRevision++
  profile.value = { nickname: '', bio: '', avatar: null }
  playlists.value = []
  counts.value = { favorites: 0, history: 0, downloads: 0, trials: 0 }
  likedKeys.value = new Set()
  trackArtworkMap.value = new Map()
  libraryError.value = ''
  favoriteBusy.clear()
  libraryRevision.value = 0
}
export const providers: Record<string, string> = { qq: 'QQ 音乐', netease: '网易云音乐', kugou: '酷狗音乐', video: '视频音轨', local: '本地音乐' }

export function trackKey(track: MusicTrack) {
  return `${track.provider || 'video'}:${track.identifier || track.webpage_url}`
}
export function trackArtwork(track?: MusicTrack | null) {
  if (!track) return null
  const key = trackKey(track)
  return trackArtworkMap.value.has(key) ? trackArtworkMap.value.get(key) : track.thumbnail
}
export function duration(value?: number | null) {
  if (!value || !Number.isFinite(value)) return '0:00'
  return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, '0')}`
}
export function trackPayload(track: MusicTrack) {
  return { title: track.title, artist: track.artist || track.uploader || '', provider: track.provider || 'video',
    identifier: track.identifier || '', webpage_url: track.webpage_url,
    thumbnail: track.custom_thumbnail ? track.automatic_thumbnail || null : track.thumbnail || null,
    duration: track.duration || null, access: track.access || '' }
}
export async function refreshLibrary() {
  const revision = ++refreshRevision
  try {
    const [overview, favorites] = await Promise.all([
      api<{ profile: Profile; playlists: Playlist[]; counts: typeof counts.value; artwork: MusicTrack[] }>('/api/library'),
      api<{ tracks: MusicTrack[] }>('/api/library/tracks?view=favorites'),
    ])
    if (revision !== refreshRevision) return
    profile.value = overview.profile
    playlists.value = overview.playlists
    counts.value = overview.counts
    trackArtworkMap.value = new Map(overview.artwork.map(track => [trackKey(track), track.thumbnail || null]))
    likedKeys.value = new Set(favorites.tracks.map(trackKey))
    libraryError.value = ''
    libraryRevision.value++
  } catch (failure) {
    if (revision === refreshRevision) libraryError.value = failure instanceof Error ? failure.message : '音乐库加载失败。'
  }
}
export function isLiked(track: MusicTrack) { return likedKeys.value.has(trackKey(track)) }
export async function toggleFavorite(track: MusicTrack) {
  const key = trackKey(track)
  if (favoriteBusy.has(key)) return
  favoriteBusy.add(key)
  const liked = !isLiked(track)
  try {
    await api('/api/library/favorites', jsonRequest('PUT', { track: trackPayload(track), liked }))
    if (liked) likedKeys.value.add(key)
    else likedKeys.value.delete(key)
    await refreshLibrary()
  } catch (failure) { libraryError.value = failure instanceof Error ? failure.message : '收藏失败。' }
  finally { favoriteBusy.delete(key) }
}
export async function getTracks(view: string, playlistId = '') {
  return (await api<{ tracks: MusicTrack[] }>(`/api/library/tracks?view=${view}&playlist_id=${encodeURIComponent(playlistId)}`)).tracks
}
export async function renewTrack(track: MusicTrack, signal?: AbortSignal): Promise<MusicTrack> {
  if (track.provider === 'local' || track.local_url || !track.provider || track.provider === 'video') return track
  return api<MusicTrack>('/api/music/parse', { ...jsonRequest('POST', { url: track.webpage_url }), signal })
}
