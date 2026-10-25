import { ref } from 'vue'
import { api, jsonRequest } from '../../shared/api/client'
import { failure } from './social'
import { desiredPosition } from './timeline'
import { pausePlayback } from '../music/player/musicPlayer'

export function roomAudio() {
  const audio = new Audio()
  audio.preload = 'auto'
  const error = ref(''), ready = ref(false), volume = ref(.7), current = ref(0)
  let identity = '', generation = 0, state: any, offset = 0, allowed = false
  let seekTimer: ReturnType<typeof setTimeout> | undefined
  audio.ontimeupdate = () => { current.value = audio.currentTime }
  audio.onerror = () => { error.value = '此音源暂时无法播放，请换源或跳过。'; ready.value = false }
  async function update(room: any, onReady?: () => Promise<void>) {
    state = room.playback; offset = room.server_time - Date.now() / 1000
    const track = state.track
    if (!track) { audio.pause(); identity = ''; ready.value = false; return }
    const key = [room.id, state.queue_id, track.provider, track.identifier, track.backing_asset_id, track.webpage_url].join(':')
    if (key !== identity) {
      identity = key; audio.pause(); ready.value = false; error.value = ''; const version = ++generation
      try {
        let url = track.backing_asset_id ? `/api/social/assets/${track.backing_asset_id}/content` : ''
        if (!url) {
          const resolved = await api('/api/music/parse', jsonRequest('POST', { url: track.webpage_url }))
          if (resolved.provider !== track.provider || String(resolved.identifier) !== String(track.identifier) || track.duration && resolved.duration && Math.abs(track.duration - resolved.duration) > 3) throw new Error('解析到的歌曲版本与房间不一致，请换源。')
          const source = await api('/api/music/playback', jsonRequest('POST', { token: resolved.token, webpage_url: resolved.webpage_url, progressive: false }))
          url = source.playback_url
        }
        if (version !== generation) return
        audio.src = url; audio.volume = volume.value
        await new Promise<void>((resolve, reject) => {
          const loaded = () => { cleanup(); resolve() }
          const failed = () => { cleanup(); reject(new Error('音源加载失败，请跳过或换源。')) }
          const cleanup = () => { clearTimeout(timeout); audio.removeEventListener('loadedmetadata', loaded); audio.removeEventListener('error', failed) }
          const timeout = setTimeout(failed, 30000)
          audio.addEventListener('loadedmetadata', loaded, { once: true }); audio.addEventListener('error', failed, { once: true }); audio.load()
        })
        if (version !== generation) return
        ready.value = true
        await onReady?.()
      } catch (e) { if (version === generation) { error.value = failure(e); identity = '' }; return }
    }
    sync()
  }
  function sync() {
    clearTimeout(seekTimer)
    if (!ready.value || !state || !allowed) return
    const target = desiredPosition(state, Date.now() / 1000 + offset)
    if (state.state !== 'playing') { audio.pause(); if (Math.abs(audio.currentTime - target) > .5) audio.currentTime = target; return }
    const wait = (state.started_at || 0) - (Date.now() / 1000 + offset)
    if (wait > .03) { audio.pause(); seekTimer = setTimeout(sync, wait * 1000); return }
    const drift = target - audio.currentTime
    if (Math.abs(drift) > .5) audio.currentTime = Math.min(target, Number.isFinite(audio.duration) ? audio.duration : target)
    audio.playbackRate = Math.abs(drift) < .08 ? 1 : drift > 0 ? 1.03 : .97
    if (audio.paused) void audio.play().catch(() => { error.value = '点击“开始听”以允许浏览器播放。' })
  }
  const correction = setInterval(sync, 2000)
  return { audio, error, ready, current, volume, update,
    start() { allowed = true; pausePlayback(); sync() },
    setVolume(value: number) { volume.value = value; audio.volume = value },
    stop() { generation++; clearInterval(correction); clearTimeout(seekTimer); audio.pause(); audio.removeAttribute('src'); audio.load() },
  }
}
