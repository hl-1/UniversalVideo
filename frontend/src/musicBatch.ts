import { api, jsonRequest, trackKey, type MusicTrack } from './musicLibrary'

// Shared by search results and scene playlists; the existing batch panel owns progress.
export async function createMusicBatch(tracks: MusicTrack[], audioFormat: 'mp3' | 'm4a', signal?: AbortSignal, destination: { playlist_id?: string; playlist_name?: string } = {}) {
  const unique = [...new Map(tracks.map(track => [trackKey(track), track])).values()]
  if (!unique.length) throw new Error('请先选择歌曲。')
  const requests: Array<{ token: string; webpage_url: string }> = []
  for (const track of unique) {
    if (track.provider === 'local' || track.local_url || track.filename) throw new Error('本地歌曲已有文件，请使用歌曲旁的文件下载入口。')
    const resolved = await api<MusicTrack>('/api/music/parse', { ...jsonRequest('POST', { url: track.webpage_url }), signal })
    if (!resolved.token) throw new Error(`无法取得“${track.title}”的下载凭据。`)
    requests.push({ token: resolved.token, webpage_url: resolved.webpage_url })
  }
  return api<{ batch_id: string }>('/api/music/batches', { ...jsonRequest('POST', { tracks: requests, audio_format: audioFormat, ...destination }), signal })
}
