import { nextTick, ref } from 'vue'
import { api, duration, jsonRequest, libraryError, refreshLibrary, trackKey, trackPayload, type MusicTrack } from './musicLibrary'

export const currentTrack = ref<MusicTrack | null>(null)
export const queue = ref<MusicTrack[]>([])
export const audio = ref<HTMLAudioElement | null>(null)
export const playbackUrl = ref('')
export const playbackState = ref<'idle' | 'loading' | 'playing' | 'paused' | 'ended' | 'error'>('idle')
export const playbackError = ref('')
export const playbackWarning = ref('')
export const elapsed = ref(0)
export const total = ref(0)
export const volume = ref(0.7)
export const muted = ref(false)
export const playMode = ref<'list' | 'loop' | 'one' | 'shuffle'>('list')
let controller: AbortController | undefined
let timeout: number | undefined
let generation = 0
let recordedGeneration = -1
let expiresAt = 0
let attachedSource = ''

export function isPlaybackTrack(track: MusicTrack) { return Boolean(currentTrack.value && trackKey(currentTrack.value) === trackKey(track)) }
function clearTimer() { window.clearTimeout(timeout); timeout = undefined }
function fail(message: string) {
  clearTimer()
  playbackState.value = 'error'
  playbackError.value = message
}
function startTimer(revision: number) {
  clearTimer()
  timeout = window.setTimeout(() => {
    if (revision !== generation) return
    controller?.abort()
    audio.value?.pause()
    generation++
    fail('音源连接超时，请重试。')
  }, 30000)
}
export function pausePlayback() {
  audio.value?.pause()
  if (playbackState.value === 'loading') {
    clearTimer()
    controller?.abort()
    generation++
    playbackState.value = 'paused'
  }
}
export function stopPlayback() {
  pausePlayback()
  generation++
  controller?.abort()
  clearTimer()
  playbackUrl.value = ''
  attachedSource = ''
  audio.value?.removeAttribute('src')
  audio.value?.load()
  currentTrack.value = null
  elapsed.value = 0
  total.value = 0
  playbackState.value = 'idle'
  playbackError.value = ''
  playbackWarning.value = ''
}
async function resume(revision: number) {
  try {
    await audio.value?.play()
    if (revision !== generation) return
  } catch (failure) {
    if (revision !== generation) return
    clearTimer()
    if (failure instanceof DOMException && failure.name === 'NotAllowedError') {
      playbackState.value = 'paused'
      playbackError.value = '点击播放继续聆听。'
    } else fail('当前音源无法播放，请重试或切换音乐平台。')
  }
}
export function addToQueue(track: MusicTrack) {
  if (!queue.value.some(item => trackKey(item) === trackKey(track))) queue.value.push(track)
}
export async function playTrack(track: MusicTrack, list?: MusicTrack[]) {
  if (list?.length) queue.value = [...new Map(list.map(item => [trackKey(item), item])).values()]
  addToQueue(track)
  controller?.abort()
  audio.value?.pause()
  const revision = ++generation
  controller = new AbortController()
  currentTrack.value = track
  playbackState.value = 'loading'
  playbackUrl.value = ''
  playbackError.value = ''
  playbackWarning.value = ''
  elapsed.value = 0
  total.value = track.duration || 0
  startTimer(revision)
  try {
    let url = track.local_url || (track.provider === 'local' ? track.webpage_url : '')
    expiresAt = Number.POSITIVE_INFINITY
    if (!url) {
      let resolved = track
      if (!track.token) resolved = await api<MusicTrack>('/api/music/parse', { ...jsonRequest('POST', { url: track.webpage_url }), signal: controller.signal })
      let response = await fetch('/api/music/playback', { ...jsonRequest('POST', { token: resolved.token, webpage_url: resolved.webpage_url }), signal: controller.signal })
      if (response.status === 410) {
        resolved = await api<MusicTrack>('/api/music/parse', { ...jsonRequest('POST', { url: track.webpage_url }), signal: controller.signal })
        response = await fetch('/api/music/playback', { ...jsonRequest('POST', { token: resolved.token, webpage_url: resolved.webpage_url }), signal: controller.signal })
      }
      const data = await response.json()
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '无法获取音源。')
      url = data.playback_url
      expiresAt = Date.now() + data.expires_in * 1000
      if (revision === generation) currentTrack.value = { ...track, ...resolved }
    }
    if (revision !== generation) return
    playbackUrl.value = url
    attachedSource = new URL(url, window.location.href).href
    await nextTick()
    if (audio.value) {
      audio.value.volume = volume.value
      audio.value.muted = muted.value
      audio.value.load()
    }
    if (revision === generation) await resume(revision)
  } catch (failure) {
    if (revision !== generation || failure instanceof DOMException && failure.name === 'AbortError') return
    fail(failure instanceof Error ? failure.message : '在线播放失败。')
  }
}
export async function togglePlayback(track = currentTrack.value, list?: MusicTrack[]) {
  if (!track) return
  if (!isPlaybackTrack(track)) { await playTrack(track, list); return }
  if (playbackState.value === 'loading') { pausePlayback(); return }
  if (playbackState.value === 'playing') { pausePlayback(); return }
  if (playbackUrl.value && playbackState.value !== 'error' && Date.now() < expiresAt) {
    if (playbackState.value === 'ended' && audio.value) audio.value.currentTime = 0
    startTimer(generation)
    await resume(generation)
  } else await playTrack(track, list)
}
function currentEvent(event: Event) {
  return event.target === audio.value && Boolean(playbackUrl.value) && audio.value?.currentSrc === attachedSource
}
export function onMetadata(event: Event) {
  if (!currentEvent(event) || !audio.value) return
  total.value = Number.isFinite(audio.value.duration) ? audio.value.duration : currentTrack.value?.duration || 0
  const expected = currentTrack.value?.duration
  if (expected && total.value < expected - Math.max(3, expected * 0.03)) {
    playbackWarning.value = `试听片段 ${duration(total.value)} / ${duration(expected)}`
  }
}
export function onTime(event: Event) {
  if (currentEvent(event) && audio.value) elapsed.value = audio.value.currentTime
}
export function onPlaying(event: Event) {
  if (!currentEvent(event) || !currentTrack.value) return
  clearTimer()
  playbackState.value = 'playing'
  playbackError.value = ''
  if (recordedGeneration === generation) return
  recordedGeneration = generation
  void api('/api/library/history', jsonRequest('POST', { track: trackPayload(currentTrack.value), preview: Boolean(playbackWarning.value) }))
    .then(() => refreshLibrary())
    .catch(failure => { libraryError.value = failure instanceof Error ? failure.message : '播放记录保存失败。' })
}
export function onPause(event: Event) {
  if (currentEvent(event) && playbackState.value !== 'error' && playbackState.value !== 'loading') playbackState.value = 'paused'
}
export function onWaiting(event: Event) {
  if (!currentEvent(event) || playbackState.value === 'error' || audio.value?.paused) return
  playbackState.value = 'loading'
  startTimer(generation)
}
export function onError(event: Event) {
  if (currentEvent(event)) fail('音源连接中断或格式无法播放，请重试。')
}
export async function nextTrack(direction = 1, automatic = false) {
  if (!queue.value.length) return
  let index = queue.value.findIndex(item => currentTrack.value && trackKey(item) === trackKey(currentTrack.value))
  if (playMode.value === 'shuffle' && queue.value.length > 1) {
    const candidates = queue.value.filter((_, candidate) => candidate !== index)
    await playTrack(candidates[Math.floor(Math.random() * candidates.length)]!)
    return
  }
  index += direction
  if (automatic && playMode.value === 'list' && index >= queue.value.length) { playbackState.value = 'ended'; return }
  index = (index + queue.value.length) % queue.value.length
  await playTrack(queue.value[index]!)
}
export function onEnded(event: Event) {
  if (!currentEvent(event)) return
  playbackState.value = 'ended'
  if (playMode.value === 'one' && currentTrack.value) void playTrack(currentTrack.value)
  else void nextTrack(1, true)
}
export function seek(value: number) {
  if (audio.value && Number.isFinite(total.value) && total.value > 0) {
    audio.value.currentTime = Math.max(0, Math.min(value, total.value))
    elapsed.value = audio.value.currentTime
  }
}
export function setVolume(value: number) {
  volume.value = Math.max(0, Math.min(1, value))
  if (volume.value > 0) muted.value = false
  if (audio.value) { audio.value.volume = volume.value; audio.value.muted = muted.value }
}
export function toggleMute() {
  muted.value = !muted.value
  if (audio.value) audio.value.muted = muted.value
}
