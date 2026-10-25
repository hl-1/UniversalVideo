import { api, jsonRequest } from '../../shared/api/client'
import { trackPayload, type MusicTrack } from './musicLibrary'
import { isAndroidApp } from '../../shared/native/bridge'
import { account } from '../accounts/auth'

import { ListeningCoverage } from './listeningCoverage'
let pending: Array<Record<string, unknown>> = []
let owner = 0
let sending = false
export type ListeningContext = 'general' | 'focus' | 'commute' | 'exercise' | 'sleep'
let listeningContext: ListeningContext = 'general'
window.addEventListener('videodream-session-cleared', () => { listeningContext = 'general'; session = null; pending = []; owner = 0 })
export function selectListeningContext(value: ListeningContext) { listeningContext = value }
export function discoveryEvent(event: Record<string, unknown>) {
  const uid = account.value?.id || 0
  if (!uid) return
  if (owner !== uid) { pending = []; owner = uid }
  pending.push({ event_id: crypto.randomUUID(), occurred_at: Date.now() / 1000, origin: 'web', ...event })
  if (pending.length > 100) pending.splice(0, pending.length - 100)
  void flush()
}
async function flush() {
  if (sending || !pending.length || owner !== account.value?.id) return
  const batch = pending.slice(0, 10), uid = owner
  sending = true
  try {
    await api('/api/music/discovery/events', jsonRequest('POST', { events: batch }))
    if (owner === uid) pending.splice(0, batch.length)
  } catch { /* Bounded memory only; never infer failed delivery as dislike. */ }
  finally { sending = false }
}
export function expose(track: MusicTrack) {
  if (!track.discovery) return
  discoveryEvent({ event_type: 'exposure', track_key: track.key, batch_id: track.discovery.batch_id, feed_id: track.discovery.feed_id, position: track.discovery.position })
}
let session: { id: string; track: MusicTrack; coverage: ListeningCoverage; seq: number; owner: number; active: boolean; duration: number; sent: number; context: ListeningContext } | null = null
export function beginListening(track: MusicTrack, active: boolean, retry = false) {
  if (isAndroidApp() || retry) return
  endListening(active ? 'user_select' : 'automatic')
  session = { id: crypto.randomUUID(), track, coverage: new ListeningCoverage(), seq: 0, owner: account.value?.id || 0, active, duration: (track.duration || 0) * 1000, sent: performance.now(), context: listeningContext }
  report('playback_start')
}
function report(type: string, reason = '') {
  if (!session || isAndroidApp()) return
  const s = session, d = s.track.discovery
  if (s.owner !== account.value?.id) { session = null; pending = []; return }
  discoveryEvent({ event_type: type, playback_id: s.id, seq: s.seq++, track: trackPayload(s.track),
    ...(d ? { track_key: s.track.key, batch_id: d.batch_id, feed_id: d.feed_id, position: d.position } : {}), context: s.context,
    listened_ms: Math.floor(s.coverage.listened), covered_ms: Math.floor(Math.min(s.coverage.covered, s.duration || Infinity)),
    duration_ms: s.duration > 0 ? Math.floor(s.duration) : null, terminal_reason: reason, active: s.active })
  s.sent = performance.now()
}
export function sampleListening(position: number, duration: number, playing: boolean) {
  if (!session || isAndroidApp()) return
  session.duration = Number.isFinite(duration) && duration > 0 ? duration * 1000 : session.duration
  session.coverage.sample(position * 1000, performance.now(), playing)
  if (performance.now() - session.sent >= 15000) report('playback_progress')
}
export function pauseListening() { session?.coverage.discontinuity(); report('playback_progress') }
export function seekListening() { session?.coverage.discontinuity() }
export function endListening(reason: string) { report('playback_end', reason); session = null }
