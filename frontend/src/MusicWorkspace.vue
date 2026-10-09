<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowRight, Download, ExternalLink, LoaderCircle, Music2, Pause, Play, RefreshCw, Search, X } from 'lucide-vue-next'
import { extractHttpUrl } from './downloadInput'

type Track = {
  title: string
  artist?: string | null
  uploader?: string | null
  thumbnail?: string | null
  duration?: number | null
  webpage_url: string
  provider?: string
  token?: string
  access?: string
  formats?: Array<{ format_id: string }>
}
type Task = {
  task_id: string
  status: 'pending' | 'running' | 'finished' | 'failed'
  progress: number
  message: string
  error?: string | null
  filename?: string | null
  download_url?: string | null
}
const props = withDefaults(defineProps<{ active?: boolean }>(), { active: true })
const input = ref('')
const source = ref('video')
const audioFormat = ref('m4a')
const busy = ref(false)
const downloading = ref(false)
const saving = ref(false)
const pollingError = ref(false)
const error = ref('')
const saved = ref('')
const selected = ref<Track | null>(null)
const audioResult = ref<HTMLElement | null>(null)
const tracks = ref<Track[]>([])
const task = ref<Task | null>(null)
const sourceMessages = ref<Array<{ provider: string; message: string }>>([])
const failedCover = ref(false)
const playbackTrack = ref<Track | null>(null)
const playbackAudio = ref<HTMLAudioElement | null>(null)
const downloadedAudio = ref<HTMLAudioElement | null>(null)
const playbackUrl = ref('')
const playbackState = ref<'idle' | 'loading' | 'playing' | 'paused' | 'ended' | 'error'>('idle')
const playbackError = ref('')
const playbackWarning = ref('')
const playbackRevision = ref(0)
let playbackController: AbortController | undefined
let playbackGeneration = 0
let playbackTimer: number | undefined
let timer: number | undefined
let controller: AbortController | undefined
let generation = 0
const hasLink = computed(() => Boolean(extractHttpUrl(input.value)))
const activeTask = computed(() => downloading.value || task.value?.status === 'running' || task.value?.status === 'pending')
const providers: Record<string, string> = { qq: 'QQ 音乐', netease: '网易云音乐', kugou: '酷狗音乐', all: '全部音乐平台', video: '视频音轨' }

async function apiError(response: Response) {
  try {
    const body = await response.json()
    return typeof body.detail === 'string' ? body.detail : body.detail?.[0]?.msg || '请求失败。'
  } catch { return `请求失败（${response.status}）。` }
}
function duration(value?: number | null) {
  if (!value) return '—'
  return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, '0')}`
}
function stopPolling() {
  if (timer) window.clearTimeout(timer)
  timer = undefined
}
function isPlaybackTrack(track: Track) {
  return playbackTrack.value?.webpage_url === track.webpage_url
}
function playbackLabel(track: Track) {
  const action = isPlaybackTrack(track)
    ? playbackState.value === 'loading' ? '取消加载' : playbackState.value === 'playing' ? '暂停' : '播放'
    : '播放'
  return `${action} ${track.title}`
}
function clearPlaybackTimer() {
  window.clearTimeout(playbackTimer)
  playbackTimer = undefined
}
function failPlayback(message: string) {
  clearPlaybackTimer()
  playbackState.value = 'error'
  playbackError.value = message
}
function startPlaybackTimer(current: number) {
  clearPlaybackTimer()
  playbackTimer = window.setTimeout(() => {
    if (current !== playbackGeneration) return
    playbackGeneration++
    playbackController?.abort()
    failPlayback('音源连接超时，请重试。')
    playbackAudio.value?.pause()
  }, 30000)
}
function stopPlayback() {
  clearPlaybackTimer()
  playbackGeneration++
  playbackController?.abort()
  playbackController = undefined
  playbackAudio.value?.pause()
  playbackAudio.value?.removeAttribute('src')
  playbackAudio.value?.load()
  playbackUrl.value = ''
  playbackTrack.value = null
  playbackState.value = 'idle'
  playbackError.value = ''
  playbackWarning.value = ''
}
function onPlaybackState(event: Event, state: 'playing' | 'paused' | 'ended') {
  if (event.target !== playbackAudio.value || !playbackUrl.value || playbackState.value === 'error') return
  playbackState.value = state
  clearPlaybackTimer()
  if (state === 'playing') { playbackError.value = ''; downloadedAudio.value?.pause() }
}
function onPlaybackError(event: Event) {
  if (event.target !== playbackAudio.value || !playbackUrl.value) return
  failPlayback('音源连接中断或格式无法播放，请重试或切换音乐平台。')
}
function onPlaybackWaiting(event: Event) {
  if (event.target !== playbackAudio.value || !playbackUrl.value || playbackState.value === 'error') return
  playbackState.value = 'loading'
  startPlaybackTimer(playbackGeneration)
}
function onPlaybackMetadata(event: Event) {
  const player = playbackAudio.value
  if (!player || event.target !== player || !playbackTrack.value) return
  const actual = player.duration
  const expected = playbackTrack.value.duration
  if (expected && Number.isFinite(actual) && actual < expected - Math.max(3, expected * 0.03)) {
    playbackWarning.value = `当前音源为试听片段（${duration(actual)} / ${duration(expected)}）。`
  }
}
async function playAudio(current: number) {
  const player = playbackAudio.value
  if (!player) return
  downloadedAudio.value?.pause()
  try {
    await player.play()
    if (current !== playbackGeneration) player.pause()
  } catch (failure) {
    if (current !== playbackGeneration) return
    if (failure instanceof DOMException && failure.name === 'NotAllowedError') {
      clearPlaybackTimer()
      playbackState.value = 'paused'
      playbackError.value = '浏览器阻止了自动播放。'
    } else {
      failPlayback('当前音源无法播放，请重试或切换音乐平台。')
    }
  }
}
async function togglePlayback(track: Track) {
  if (isPlaybackTrack(track)) {
    if (playbackState.value === 'loading') { stopPlayback(); return }
    if (playbackState.value === 'playing') { playbackAudio.value?.pause(); return }
    if (playbackUrl.value && playbackState.value !== 'error') {
      startPlaybackTimer(playbackGeneration)
      await playAudio(playbackGeneration)
      return
    }
  }
  stopPlayback()
  playbackTrack.value = track
  playbackState.value = 'loading'
  playbackRevision.value++
  playbackController = new AbortController()
  const current = playbackGeneration
  startPlaybackTimer(current)
  try {
    const response = await fetch('/api/music/playback', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: track.token, webpage_url: track.webpage_url }), signal: playbackController.signal })
    if (!response.ok) throw new Error(response.status === 404 ? '当前后端未启用在线播放，请重启后端服务。' : await apiError(response))
    const data = await response.json()
    if (current !== playbackGeneration) return
    playbackUrl.value = data.playback_url
    await nextTick()
    if (current === playbackGeneration) await playAudio(current)
  } catch (failure) {
    if (current !== playbackGeneration || failure instanceof DOMException && failure.name === 'AbortError') return
    failPlayback(failure instanceof Error ? failure.message : '在线播放失败，请重试。')
  }
}
async function pick(track: Track) {
  if (activeTask.value) return
  selected.value = track
  task.value = null
  failedCover.value = false
  error.value = ''
  saved.value = ''
  await nextTick()
  audioResult.value?.scrollIntoView({ block: 'nearest' })
}
async function submit() {
  if (activeTask.value) return
  const text = input.value.trim()
  if (!text) { error.value = '请输入歌曲名称或粘贴链接。'; return }
  const link = extractHttpUrl(text)
  if (!link && source.value === 'video') { error.value = '请粘贴视频链接或完整的抖音分享文字。'; return }
  stopPlayback()
  controller?.abort()
  controller = new AbortController()
  const current = ++generation
  busy.value = true
  error.value = ''
  saved.value = ''
  selected.value = null
  tracks.value = []
  sourceMessages.value = []
  task.value = null
  failedCover.value = false
  stopPolling()
  try {
    const response = link
      ? await fetch('/api/music/parse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: link }), signal: controller.signal })
      : await fetch(`/api/music/search?q=${encodeURIComponent(text)}&source=${source.value}`, { signal: controller.signal })
    if (!response.ok) throw new Error(await apiError(response))
    const data = await response.json()
    if (current !== generation) return
    if (link) selected.value = data
    else { tracks.value = data.tracks; sourceMessages.value = data.sources }
  } catch (failure) {
    if (failure instanceof DOMException && failure.name === 'AbortError') return
    error.value = failure instanceof Error ? failure.message : '音乐解析失败。'
  } finally { if (current === generation) busy.value = false }
}
async function poll(id: string) {
  try {
    const response = await fetch(`/api/tasks/${id}`)
    if (!response.ok) throw new Error(await apiError(response))
    task.value = await response.json()
    if (pollingError.value) error.value = ''
    pollingError.value = false
    if (task.value?.status === 'pending' || task.value?.status === 'running') timer = window.setTimeout(() => poll(id), 1200)
  } catch (failure) {
    pollingError.value = true
    error.value = failure instanceof Error ? failure.message : '任务查询失败。'
  }
}
async function download() {
  if (!selected.value || activeTask.value) return
  downloading.value = true
  pollingError.value = false
  error.value = ''
  saved.value = ''
  stopPolling()
  try {
    const response = await fetch('/api/download', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: selected.value.webpage_url, media_type: 'audio', audio_format: audioFormat.value,
        format_id: selected.value.token ? `music:${selected.value.token}` : selected.value.formats?.[0]?.format_id }),
    })
    if (!response.ok) throw new Error(await apiError(response))
    const data = await response.json()
    task.value = { task_id: data.task_id, status: 'pending', progress: 0, message: '等待下载任务开始' }
    await poll(data.task_id)
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '下载任务创建失败。' }
  finally { downloading.value = false }
}
async function save() {
  if (!task.value?.download_url) return
  const downloadUrl = task.value.download_url
  const filename = task.value.filename
  const title = selected.value?.title || 'audio'
  saving.value = true
  error.value = ''
  try {
    const ext = filename?.split('.').pop() || 'm4a'
    const name = title.replace(/[\\/:*?"<>|]+/g, ' ').slice(0, 80) + `.${ext}`
    const handle = window.showSaveFilePicker ? await window.showSaveFilePicker({ suggestedName: name,
      types: [{ description: 'Audio', accept: ext === 'mp3' ? { 'audio/mpeg': ['.mp3'] } : { 'audio/mp4': ['.m4a'] } }] }) : null
    const response = await fetch(downloadUrl)
    if (!response.ok) throw new Error(await apiError(response))
    const blob = await response.blob()
    if (handle) {
      const writable = await handle.createWritable()
      await writable.write(blob)
      await writable.close()
    } else {
      const objectUrl = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = objectUrl
      anchor.download = name
      anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30000)
    }
    saved.value = '音频已保存。'
  } catch (failure) {
    if (!(failure instanceof DOMException && failure.name === 'AbortError')) error.value = failure instanceof Error ? failure.message : '保存失败。'
  } finally { saving.value = false }
}
watch(() => props.active, active => {
  if (active) return
  if (playbackState.value === 'loading') stopPlayback()
  else playbackAudio.value?.pause()
})
onBeforeUnmount(() => { generation++; controller?.abort(); stopPolling(); stopPlayback() })
</script>

<template>
  <div class="music-workspace" role="tabpanel" id="music-panel" aria-labelledby="music-tab">
    <form class="link-composer" @submit.prevent="submit">
      <textarea v-model="input" aria-label="歌曲名称或音乐视频链接" :disabled="activeTask" :placeholder="source === 'video' ? '粘贴抖音视频链接或分享文字…' : '输入歌名、歌手，或粘贴歌曲链接…'" @keydown.ctrl.enter.prevent="submit" />
      <div class="composer-footer">
        <label><span>来源</span><select v-model="source" :disabled="busy || activeTask" aria-label="音乐来源">
          <option value="video">视频音轨 · 抖音 / B站 / YouTube</option>
          <option value="all">全部音乐平台</option><option value="qq">QQ 音乐</option><option value="netease">网易云音乐</option><option value="kugou">酷狗音乐</option>
        </select></label>
        <label><span>格式</span><select v-model="audioFormat" :disabled="activeTask" aria-label="音频导出格式"><option value="m4a">M4A</option><option value="mp3">MP3</option></select></label>
        <button class="composer-submit" :disabled="busy || activeTask || !input.trim()" type="submit"><LoaderCircle v-if="busy" :size="18" class="spin" /><Search v-else-if="!hasLink && source !== 'video'" :size="18" /><ArrowRight v-else :size="18" />{{ busy ? '处理中' : hasLink || source === 'video' ? '解析音轨' : '搜索歌曲' }}</button>
      </div>
    </form>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <p v-if="saved" class="success-text" role="status">{{ saved }}</p>
    <div v-if="sourceMessages.length" class="source-messages"><p v-for="item in sourceMessages" :key="item.provider"><strong>{{ providers[item.provider] }}</strong>{{ item.message }}</p></div>
    <div v-if="selected" ref="audioResult" class="audio-result">
      <div class="audio-cover"><img v-if="selected.thumbnail && !failedCover" :src="selected.thumbnail" :alt="selected.title" @error="failedCover = true" /><Music2 v-else :size="32" /></div>
      <div class="audio-meta"><span class="status-chip">{{ selected.provider ? providers[selected.provider] : '视频音轨' }}</span><h2>{{ selected.title }}</h2><div class="meta-row"><span>{{ selected.artist || selected.uploader }}</span><time>{{ duration(selected.duration) }}</time><span v-if="selected.access">{{ selected.access }}</span><a :href="selected.webpage_url" target="_blank" rel="noreferrer">原始页面<ExternalLink :size="13" /></a></div></div>
      <button class="composer-submit" :disabled="activeTask || busy" @click="download"><LoaderCircle v-if="activeTask" :size="18" class="spin" /><Download v-else :size="18" />{{ activeTask ? '处理中' : `下载 ${audioFormat.toUpperCase()}` }}</button>
    </div>
    <div v-if="task" class="audio-task" :class="{ failed: task.status === 'failed' }" aria-live="polite">
      <div class="task-head"><span>{{ task.error || task.message }}</span><strong>{{ Math.round(task.progress) }}%</strong></div>
      <progress :value="task.progress" max="100" aria-label="音轨下载进度" />
      <button v-if="pollingError" class="secondary-action" @click="poll(task.task_id)"><RefreshCw :size="16" />刷新任务</button>
      <div v-if="task.status === 'finished' && task.download_url" class="audio-finished"><audio ref="downloadedAudio" :src="task.download_url" controls preload="metadata" aria-label="下载音轨试听" @play="stopPlayback" @error="error = '音频播放失败，请重新下载。'" /><button class="composer-submit" :disabled="saving" @click="save"><Download :size="18" />{{ saving ? '保存中' : '另存为' }}</button></div>
    </div>
    <div v-if="playbackTrack" class="music-playback" :class="{ failed: playbackState === 'error' }">
      <div class="playback-track"><strong>{{ playbackTrack.title }}</strong><span>{{ playbackTrack.artist }} · {{ providers[playbackTrack.provider || ''] }}</span><span v-if="playbackState === 'loading'" role="status">正在连接音源</span></div>
      <audio v-show="playbackUrl && playbackState !== 'error'" :key="playbackRevision" ref="playbackAudio" :src="playbackUrl || undefined" controls preload="metadata" aria-label="在线播放器" @playing="onPlaybackState($event, 'playing')" @waiting="onPlaybackWaiting" @pause="onPlaybackState($event, 'paused')" @ended="onPlaybackState($event, 'ended')" @error="onPlaybackError" @loadedmetadata="onPlaybackMetadata" />
      <button type="button" class="playback-close" title="停止在线播放" aria-label="停止在线播放" @click="stopPlayback"><X :size="18" /></button>
      <div v-if="playbackError" class="playback-feedback"><p class="playback-error" role="alert">{{ playbackError }}</p><button type="button" class="track-play" title="重试播放" aria-label="重试播放" @click="togglePlayback(playbackTrack)"><RefreshCw :size="17" /></button></div><p v-else-if="playbackWarning" class="playback-warning" role="status">{{ playbackWarning }}</p>
    </div>
    <div v-if="tracks.length" class="track-results">
      <div class="results-heading"><h2>搜索结果</h2><span>{{ tracks.length }} 首</span></div>
      <div v-for="(track, index) in tracks" :key="`${track.provider}-${track.webpage_url}`" class="track-row" :class="{ selected: selected?.webpage_url === track.webpage_url, playing: isPlaybackTrack(track) }" @click="pick(track)">
        <button type="button" class="track-select" :disabled="activeTask" :aria-label="`选择 ${track.title} ${track.artist || ''}`" @click.stop="pick(track)"><span class="track-number">{{ String(index + 1).padStart(2, '0') }}</span><span class="track-name"><strong>{{ track.title }}</strong><span>{{ track.artist || '未知歌手' }}</span></span></button>
        <button type="button" class="track-play" :class="{ active: isPlaybackTrack(track) }" :title="playbackLabel(track)" :aria-label="playbackLabel(track)" :aria-pressed="isPlaybackTrack(track) && playbackState === 'playing'" :aria-busy="isPlaybackTrack(track) && playbackState === 'loading'" @click.stop="togglePlayback(track)"><LoaderCircle v-if="isPlaybackTrack(track) && playbackState === 'loading'" :size="17" class="spin" /><Pause v-else-if="isPlaybackTrack(track) && playbackState === 'playing'" :size="17" /><Play v-else :size="17" /></button>
        <span class="track-provider">{{ providers[track.provider || ''] }}</span><time>{{ duration(track.duration) }}</time><ArrowRight :size="16" aria-hidden="true" />
      </div>
    </div>
  </div>
</template>
