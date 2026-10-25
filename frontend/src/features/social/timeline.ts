export type PlaybackTimeline = { state: string; position_ms: number; started_at?: number | null }

export function desiredPosition(playback: PlaybackTimeline, serverNow: number) {
  const since = playback.state === 'playing' && playback.started_at != null ? Math.max(0, serverNow - playback.started_at) : 0
  return Math.max(0, playback.position_ms / 1000 + since)
}
