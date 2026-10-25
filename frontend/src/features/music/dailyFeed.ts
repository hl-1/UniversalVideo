import type { MusicTrack } from './musicLibrary'
export type DailyFeed = {
  feed_id: string | null; tracks: MusicTrack[]; profile_revision: number; latest_profile_revision: number
  agent_revision: number; refreshing: boolean; can_change: boolean; retry_after_seconds: number
  validation_stale?: boolean
  refill?: { state: 'running' | 'budget_exhausted' | 'cooldown' | 'disabled' | 'idle'; retry_at: number | null }
  reason: 'cold_start' | 'refreshing' | 'exhausted' | 'insufficient_candidates' | 'ready' | 'error'; saved_playlist_id?: string | null
}
export function feedNotice(feed: DailyFeed | null) {
  if (!feed) return ''
  if (feed.reason === 'cold_start') return '收藏喜欢的歌曲，或设置听歌偏好，开始发现新声音。'
  if (feed.reason === 'error') return '推荐服务暂时不可用，请稍后重试。'
  if (!feed.tracks.length) return '新推荐准备中，准备好后会自动显示。'
  if (feed.tracks.length < 6) return '新推荐准备中，准备好后会自动补齐。'
  if (feed.profile_revision !== feed.latest_profile_revision) return '偏好已更新，下批生效。'
  return ''
}

export function changeNotice(previous: DailyFeed | null, next: DailyFeed) {
  return next.feed_id === previous?.feed_id || next.tracks.length < 6 ? '新推荐准备中，稍后再试。' : ''
}
