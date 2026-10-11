<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { account } from './auth'
import { ArrowRight, Check, ChevronDown, Download, ExternalLink, FileArchive, Heart, LoaderCircle, MoreHorizontal, Music2, Pause, Play, RefreshCw, Search, Square, X } from 'lucide-vue-next'
import { extractHttpUrl } from './downloadInput'
import { getTracks, isLiked, libraryRevision, refreshLibrary, renewTrack, toggleFavorite, trackArtwork, trackKey, type MusicTrack } from './musicLibrary'
import { currentTrack, isPlaybackTrack, playbackState, playTrack, togglePlayback } from './musicPlayer'

type Track = MusicTrack
type Task = {
  task_id: string
  status: 'pending' | 'running' | 'finished' | 'failed'
  progress: number
  message: string
  error?: string | null
  filename?: string | null
  download_url?: string | null
}
type BatchItem = Omit<Task, 'status'> & { status: Task['status'] | 'cancelled'; title: string; artist: string; provider: string }
type Batch = {
  batch_id: string
  status: 'pending' | 'running' | 'cancelling' | 'finished' | 'failed' | 'cancelled'
  audio_format: string
  progress: number
  total: number
  finished: number
  failed: number
  cancelled: number
  items: BatchItem[]
  download_url?: string | null
}
const props = withDefaults(defineProps<{ active?: boolean; embedded?: boolean }>(), { active: true, embedded: false })
const emit = defineEmits<{ 'track-menu': [track: MusicTrack, event: MouseEvent] }>()
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
const checked = ref(new Set<string>())
const searchQuery = ref('')
const searchSource = ref('')
const searchPage = ref(1)
const hasMore = ref(false)
const loadingMore = ref(false)
const batch = ref<Batch | null>(null)
const batchBusy = ref(false)
const batchSaving = ref(false)
const batchError = ref('')
const batchSaved = ref('')
const batchPollingError = ref(false)
const batchStorageKey = `videodream.music-batch.${account.value?.id}`
let batchTimer: number | undefined
let batchController: AbortController | undefined
let batchGeneration = 0
let disposed = false
const task = ref<Task | null>(null)
const sourceMessages = ref<Array<{ provider: string; message: string }>>([])
const failedCover = ref(false)
const localTracks = ref<Track[]>([])
const announcedDownloads = new Set<string>()
let localRevision = 0
let timer: number | undefined
let controller: AbortController | undefined
let generation = 0
const hasLink = computed(() => Boolean(extractHttpUrl(input.value)))
const singleActive = computed(() => downloading.value || task.value?.status === 'running' || task.value?.status === 'pending')
const batchActive = computed(() => batchBusy.value || ['pending', 'running', 'cancelling'].includes(batch.value?.status || ''))
const batchComplete = computed(() => batch.value?.status === 'finished' && batch.value.finished === batch.value.total && !batch.value.failed && !batch.value.cancelled)
const activeTask = computed(() => singleActive.value || batchActive.value)
const checkedTracks = computed(() => tracks.value.filter(track => checked.value.has(trackKey(track))))
const allChecked = computed(() => tracks.value.length > 0 && tracks.value.slice(0, 100).every(track => checked.value.has(trackKey(track))))
const batchHeading = computed(() => ({ pending: '等待下载', running: '批量下载中', cancelling: '正在停止后续歌曲', finished: '批量下载完成', failed: '批量下载失败', cancelled: '批量下载已停止' }[batch.value?.status || 'pending']))
const providers: Record<string, string> = { qq: 'QQ 音乐', netease: '网易云音乐', kugou: '酷狗音乐', all: '全部音乐平台', video: '视频音轨' }
const downloadedTrack = computed<Track | null>(() => selected.value && task.value?.status === 'finished' && task.value.download_url
  ? { ...selected.value, filename: task.value.filename || undefined, local_url: task.value.download_url }
  : null)

async function loadLocalTracks() {
  const revision = ++localRevision
  try {
    const data = await getTracks('downloads')
    if (!disposed && revision === localRevision) localTracks.value = data
  } catch { /* The download result remains playable if the library is temporarily offline. */ }
}
function localFor(track: Track) { return localTracks.value.find(item => trackKey(item) === trackKey(track) && item.local_url) }
function playableTrack(track: Track) {
  const local = localFor(track)
  return local ? { ...track, filename: local.filename, local_url: local.local_url } : track
}
function announceDownloads(items: Array<{ status: string; filename?: string | null }>) {
  let changed = false
  for (const item of items) {
    if (item.status === 'finished' && item.filename && !announcedDownloads.has(item.filename)) {
      announcedDownloads.add(item.filename)
      changed = true
    }
  }
  if (changed) void refreshLibrary()
}
async function playSearchTrack(track: Track) {
  await toggleTrack(playableTrack(track), tracks.value.map(playableTrack))
}
async function toggleTrack(track: Track, list?: Track[]) {
  if (track.local_url && currentTrack.value?.local_url !== track.local_url) await playTrack(track, list)
  else await togglePlayback(track, list)
}
async function playBatchItem(item: BatchItem) {
  if (!item.download_url || item.status !== 'finished') return
  const list: Track[] = (batch.value?.items || []).filter(entry => entry.status === 'finished' && entry.download_url).map(entry => {
    const local = localTracks.value.find(track => track.filename === entry.filename)
    return local || { title: entry.title, artist: entry.artist, provider: 'local', webpage_url: entry.download_url!,
      filename: entry.filename || undefined, local_url: entry.download_url }
  })
  const track = list.find(track => track.filename === item.filename)
  if (track) await toggleTrack(track, list)
}
function toggleChecked(track: Track, event: Event) {
  const key = trackKey(track)
  if (checked.value.has(key)) checked.value.delete(key)
  else if (checked.value.size < 100) checked.value.add(key)
  else error.value = '每批最多选择 100 首歌曲。'
  const checkbox = event.target as HTMLInputElement
  checkbox.checked = checked.value.has(key)
}
function toggleAll() {
  selectFirst(allChecked.value ? 0 : 100)
}
function selectFirst(count: number) {
  checked.value = new Set(tracks.value.slice(0, Math.min(count, 100)).map(trackKey))
}
function quickSelect(event: Event) {
  const select = event.target as HTMLSelectElement
  selectFirst(select.value === 'all' ? 100 : Number(select.value))
  select.value = ''
}
function rememberBatch(id?: string) {
  try {
    if (id) localStorage.setItem(batchStorageKey, id)
    else localStorage.removeItem(batchStorageKey)
  } catch { /* Storage can be unavailable in private browser sessions. */ }
}
function stopBatchPolling() {
  window.clearTimeout(batchTimer)
  batchTimer = undefined
  batchController?.abort()
}
function dismissBatch() {
  if (batchActive.value || batchSaving.value) return
  stopBatchPolling()
  batchGeneration++
  batch.value = null
  batchError.value = ''
  batchSaved.value = ''
  batchPollingError.value = false
  rememberBatch()
}
async function pollBatch(id: string, revision = batchGeneration, restore = false) {
  if (disposed || revision !== batchGeneration) return
  batchController = new AbortController()
  try {
    const response = await fetch(`/api/music/batches/${id}`, { signal: batchController.signal })
    if (disposed || revision !== batchGeneration) return
    if (!response.ok) {
      if (response.status === 404) {
        batch.value = null
        rememberBatch()
        batchBusy.value = false
        if (restore) return
      }
      throw new Error(await apiError(response))
    }
    const data: Batch = await response.json()
    if (disposed || revision !== batchGeneration) return
    batch.value = data
    announceDownloads(data.items)
    batchBusy.value = false
    if (batchPollingError.value) batchError.value = ''
    batchPollingError.value = false
    if (batchActive.value) {
      rememberBatch(id)
      batchTimer = window.setTimeout(() => pollBatch(id, revision), 1200)
    } else if (batchComplete.value) {
      rememberBatch()
      if (restore || !props.active) dismissBatch()
    }
  } catch (failure) {
    if (disposed || revision !== batchGeneration || failure instanceof DOMException && failure.name === 'AbortError') return
    batchPollingError.value = true
    batchError.value = failure instanceof Error ? failure.message : '批量任务查询失败。'
    if (batch.value || batchBusy.value) batchTimer = window.setTimeout(() => pollBatch(id, revision), 3000)
  }
}
async function startBatch() {
  if (!checkedTracks.value.length || activeTask.value || batchSaving.value) return
  batchBusy.value = true
  batchError.value = ''
  batchSaved.value = ''
  stopBatchPolling()
  const revision = ++batchGeneration
  try {
    const response = await fetch('/api/music/batches', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tracks: checkedTracks.value.map(track => ({ token: track.token, webpage_url: track.webpage_url })), audio_format: audioFormat.value }) })
    if (!response.ok) throw new Error(await apiError(response))
    const data: Batch = await response.json()
    if (disposed) return
    batch.value = data
    rememberBatch(data.batch_id)
    checked.value = new Set()
    await pollBatch(data.batch_id, revision)
  } catch (failure) { batchError.value = failure instanceof Error ? failure.message : '批量任务创建失败。' }
  finally { batchBusy.value = false }
}
async function updateBatch(action: 'cancel' | 'retry') {
  if (!batch.value || batchBusy.value || batchSaving.value) return
  batchBusy.value = true
  batchError.value = ''
  batchSaved.value = ''
  stopBatchPolling()
  const revision = ++batchGeneration
  const id = batch.value.batch_id
  try {
    const response = await fetch(`/api/music/batches/${id}/${action}`, { method: 'POST' })
    if (!response.ok) throw new Error(await apiError(response))
    const data: Batch = await response.json()
    if (disposed) return
    batch.value = data
  } catch (failure) { batchError.value = failure instanceof Error ? failure.message : '批量任务操作失败。' }
  finally {
    batchBusy.value = false
    if (!disposed) await pollBatch(id, revision)
  }
}
async function saveBatch() {
  if (!batch.value?.download_url || batchSaving.value) return
  const url = batch.value.download_url
  const name = `music-${batch.value.batch_id}.zip`
  batchSaving.value = true
  batchError.value = ''
  batchSaved.value = ''
  try {
    const handle = window.showSaveFilePicker ? await window.showSaveFilePicker({ suggestedName: name,
      types: [{ description: 'ZIP', accept: { 'application/zip': ['.zip'] } }] }) : null
    const response = await fetch(url)
    if (!response.ok) throw new Error(await apiError(response))
    if (handle && response.body) {
      const writable = await handle.createWritable() as FileSystemWritableFileStream
      await response.body.pipeTo(writable)
    } else {
      const objectUrl = URL.createObjectURL(await response.blob())
      const anchor = document.createElement('a')
      anchor.href = objectUrl
      anchor.download = name
      anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30000)
    }
    batchSaved.value = 'ZIP 已保存。'
  } catch (failure) {
    if (!(failure instanceof DOMException && failure.name === 'AbortError')) batchError.value = failure instanceof Error ? failure.message : '打包保存失败。'
  } finally { batchSaving.value = false }
}
function itemStatus(item: BatchItem) {
  return { pending: '等待中', running: `${Math.round(item.progress)}%`, finished: '已完成', failed: '失败', cancelled: '已取消' }[item.status]
}
async function loadMore() {
  if (!hasMore.value || loadingMore.value || busy.value) return
  loadingMore.value = true
  error.value = ''
  const current = generation
  try {
    const response = await fetch(`/api/music/search?q=${encodeURIComponent(searchQuery.value)}&source=${searchSource.value}&page=${searchPage.value + 1}`, { signal: controller?.signal })
    if (!response.ok) throw new Error(await apiError(response))
    const data = await response.json()
    if (current !== generation) return
    const known = new Set(tracks.value.map(trackKey))
    for (const track of data.tracks as Track[]) {
      if (!known.has(trackKey(track))) { tracks.value.push(track); known.add(trackKey(track)) }
    }
    searchPage.value = data.page
    hasMore.value = data.has_more
    sourceMessages.value = data.sources
  } catch (failure) {
    if (current === generation && !(failure instanceof DOMException && failure.name === 'AbortError')) error.value = failure instanceof Error ? failure.message : '加载更多失败。'
  } finally { if (current === generation) loadingMore.value = false }
}

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
function playbackLabel(track: Track) {
  const action = isPlaybackTrack(track)
    ? playbackState.value === 'loading' ? '取消加载' : playbackState.value === 'playing' ? '暂停' : '播放'
    : '播放'
  return `${action} ${track.title}`
}
async function pick(track: Track) {
  if (singleActive.value) return
  selected.value = track
  task.value = null
  failedCover.value = false
  error.value = ''
  saved.value = ''
  await nextTick()
  audioResult.value?.scrollIntoView({ block: 'nearest' })
}
function isDownloadingTrack(track: Track) {
  return singleActive.value && selected.value?.webpage_url === track.webpage_url
}
async function downloadTrack(track: Track) {
  if (activeTask.value || busy.value || saving.value) return
  await pick(track)
  await download()
}
async function submit() {
  if (singleActive.value) return
  const text = input.value.trim()
  if (!text) { error.value = '请输入歌曲名称或粘贴链接。'; return }
  const link = extractHttpUrl(text)
  if (!link && source.value === 'video') { error.value = '请粘贴视频链接或完整的抖音分享文字。'; return }
  if (batchComplete.value) dismissBatch()
  controller?.abort()
  controller = new AbortController()
  const current = ++generation
  busy.value = true
  error.value = ''
  saved.value = ''
  selected.value = null
  tracks.value = []
  checked.value = new Set()
  searchQuery.value = text
  searchSource.value = source.value
  searchPage.value = 1
  hasMore.value = false
  loadingMore.value = false
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
    else { tracks.value = data.tracks; sourceMessages.value = data.sources; hasMore.value = data.has_more }
  } catch (failure) {
    if (failure instanceof DOMException && failure.name === 'AbortError') return
    error.value = failure instanceof Error ? failure.message : '音乐解析失败。'
  } finally { if (current === generation) busy.value = false }
}
async function poll(id: string) {
  try {
    const response = await fetch(`/api/tasks/${id}`)
    if (!response.ok) throw new Error(await apiError(response))
    const data: Task = await response.json()
    if (disposed || task.value?.task_id !== id) return
    task.value = data
    announceDownloads([data])
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
    const track = await renewTrack(selected.value)
    if (disposed) return
    selected.value = track
    const response = await fetch('/api/download', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: track.webpage_url, media_type: 'audio', audio_format: audioFormat.value,
        format_id: track.token ? `music:${track.token}` : track.formats?.[0]?.format_id }),
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
async function search(query: string, provider = 'all') {
  input.value = query
  source.value = provider
  await submit()
}
defineExpose({ search, downloadTrack })
watch(libraryRevision, () => { void loadLocalTracks() })
watch(() => trackArtwork(selected.value), () => { failedCover.value = false })
watch(() => props.active, active => { if (!active && batchComplete.value) dismissBatch() })
onMounted(() => {
  void loadLocalTracks()
  let id: string | null = null
  try { id = localStorage.getItem(batchStorageKey) } catch { return }
  if (id && /^[a-f0-9]{12}$/.test(id)) { batchBusy.value = true; void pollBatch(id, batchGeneration, true) }
})
onBeforeUnmount(() => { disposed = true; localRevision++; batchGeneration++; stopBatchPolling(); generation++; controller?.abort(); stopPolling() })
</script>

<template>
  <div class="music-workspace" :role="embedded ? undefined : 'tabpanel'" :id="embedded ? undefined : 'music-panel'" :aria-labelledby="embedded ? undefined : 'music-tab'">
    <form v-if="!embedded" class="link-composer" @submit.prevent="submit">
      <textarea v-model="input" aria-label="歌曲名称或音乐视频链接" :disabled="singleActive" :placeholder="source === 'video' ? '粘贴抖音视频链接或分享文字…' : '输入歌名、歌手，或粘贴歌曲链接…'" @keydown.ctrl.enter.prevent="submit" />
      <div class="composer-footer">
        <label><span>来源</span><select v-model="source" :disabled="busy || singleActive" aria-label="音乐来源">
          <option value="video">视频音轨 · 抖音 / B站 / YouTube</option>
          <option value="all">全部音乐平台</option><option value="qq">QQ 音乐</option><option value="netease">网易云音乐</option><option value="kugou">酷狗音乐</option>
        </select></label>
        <label><span>格式</span><select v-model="audioFormat" :disabled="singleActive" aria-label="音频导出格式"><option value="m4a">M4A</option><option value="mp3">MP3</option></select></label>
        <button class="composer-submit" :disabled="busy || singleActive || !input.trim()" type="submit"><LoaderCircle v-if="busy" :size="18" class="spin" /><Search v-else-if="!hasLink && source !== 'video'" :size="18" /><ArrowRight v-else :size="18" />{{ busy ? '处理中' : hasLink || source === 'video' ? '解析音轨' : '搜索歌曲' }}</button>
      </div>
    </form>
    <div v-if="embedded" class="music-format-toolbar"><label><span>下载格式</span><select v-model="audioFormat" :disabled="activeTask" aria-label="音频导出格式"><option value="m4a">M4A</option><option value="mp3">MP3</option></select></label></div>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <p v-if="saved" class="success-text" role="status">{{ saved }}</p>
    <p v-if="batchError" class="error-text" role="alert">{{ batchError }}</p>
    <p v-if="batchSaved" class="success-text" role="status">{{ batchSaved }}</p>
    <section v-if="batch" class="music-batch" :class="{ 'batch-complete': batchComplete }" aria-label="批量下载队列">
      <div class="batch-heading"><div><h2>{{ batchHeading }}</h2><p>成功 {{ batch.finished }} / {{ batch.total }}<span v-if="batch.failed"> · 失败 {{ batch.failed }}</span><span v-if="batch.cancelled"> · 已取消 {{ batch.cancelled }}</span> · {{ batch.audio_format.toUpperCase() }}</p></div><strong v-if="!batchComplete">{{ Math.round(batch.progress) }}%</strong></div>
      <progress v-if="!batchComplete" :value="batch.progress" max="100" aria-label="批量音频下载进度" />
      <div class="batch-actions">
        <button v-if="batchActive" type="button" class="secondary-action" :disabled="batchBusy || batch.status === 'cancelling'" @click="updateBatch('cancel')"><Square :size="15" />{{ batch.status === 'cancelling' ? '等待当前歌曲结束' : '停止后续' }}</button>
        <button v-if="batch.failed && !batchActive" type="button" class="secondary-action" :disabled="batchSaving" @click="updateBatch('retry')"><RefreshCw :size="16" />重试失败歌曲</button>
        <button v-if="batch.download_url" type="button" class="composer-submit" :disabled="batchSaving" @click="saveBatch"><LoaderCircle v-if="batchSaving" :size="18" class="spin" /><FileArchive v-else :size="18" />{{ batchSaving ? '打包保存中' : `保存 ZIP · ${batch.finished} 首` }}</button>
        <button v-if="batchPollingError" type="button" class="track-play" title="刷新队列" aria-label="刷新队列" @click="stopBatchPolling(); pollBatch(batch.batch_id)"><RefreshCw :size="17" /></button>
        <button v-if="!batchActive" type="button" class="track-play" :disabled="batchSaving" title="关闭下载记录" aria-label="关闭下载记录" @click="dismissBatch"><X :size="18" /></button>
      </div>
      <ol v-if="!batchComplete" class="batch-items">
        <li v-for="item in batch.items" :key="item.task_id" :class="item.status">
          <Check v-if="item.status === 'finished'" :size="16" /><LoaderCircle v-else-if="item.status === 'running'" :size="16" class="spin" /><X v-else-if="item.status === 'failed' || item.status === 'cancelled'" :size="16" /><Music2 v-else :size="16" />
          <div class="batch-item-name"><strong>{{ item.title }}</strong><span>{{ item.artist }} · {{ providers[item.provider] }}</span><p v-if="item.error">{{ item.error }}</p></div>
          <span class="batch-item-status">{{ itemStatus(item) }}</span>
          <div v-if="item.status === 'finished' && item.download_url" class="batch-item-actions"><button type="button" class="track-play" :title="`播放已下载的 ${item.title}`" :aria-label="`播放已下载的 ${item.title}`" @click="playBatchItem(item)"><Play :size="16" /></button><a class="track-play" :href="item.download_url" :download="`${item.artist} - ${item.title}.${batch.audio_format}`" :title="`保存 ${item.title}`" :aria-label="`保存 ${item.title}`"><Download :size="16" /></a></div><span v-else class="batch-item-placeholder" />
        </li>
      </ol>
    </section>
    <div v-if="sourceMessages.length" class="source-messages"><p v-for="item in sourceMessages" :key="item.provider"><strong>{{ providers[item.provider] }}</strong>{{ item.message }}</p></div>
    <div v-if="selected" ref="audioResult" class="audio-result">
      <div class="audio-cover"><img v-if="trackArtwork(selected) && !failedCover" :src="trackArtwork(selected) || undefined" :alt="selected.title" @error="failedCover = true" /><Music2 v-else :size="32" /></div>
      <div class="audio-meta"><span class="status-chip">{{ selected.provider ? providers[selected.provider] : '视频音轨' }}</span><h2>{{ selected.title }}</h2><div class="meta-row"><span>{{ selected.artist || selected.uploader }}</span><time>{{ duration(selected.duration) }}</time><span v-if="selected.access">{{ selected.access }}</span><a :href="selected.webpage_url" target="_blank" rel="noreferrer">原始页面<ExternalLink :size="13" /></a></div></div>
      <div class="audio-selected-actions"><button class="track-play track-favorite" :class="{ liked: isLiked(selected) }" type="button" :title="isLiked(selected) ? '取消喜欢' : '喜欢'" :aria-label="`${isLiked(selected) ? '取消喜欢' : '喜欢'} ${selected.title}`" :aria-pressed="isLiked(selected)" @click="toggleFavorite(selected)"><Heart :size="17" :fill="isLiked(selected) ? 'currentColor' : 'none'" /></button><button v-if="embedded" class="track-play" type="button" title="更多操作" :aria-label="`更多操作 ${selected.title}`" @click="emit('track-menu', selected, $event)"><MoreHorizontal :size="18" /></button><button class="composer-submit" :disabled="activeTask || busy" @click="download"><LoaderCircle v-if="activeTask" :size="18" class="spin" /><Download v-else :size="18" />{{ activeTask ? '处理中' : `下载 ${audioFormat.toUpperCase()}` }}</button></div>
    </div>
    <div v-if="task" class="audio-task" :class="{ failed: task.status === 'failed' }" aria-live="polite">
      <div class="task-head"><span>{{ task.error || task.message }}</span><strong>{{ Math.round(task.progress) }}%</strong></div>
      <progress :value="task.progress" max="100" aria-label="音轨下载进度" />
      <button v-if="pollingError" class="secondary-action" @click="poll(task.task_id)"><RefreshCw :size="16" />刷新任务</button>
      <div v-if="downloadedTrack" class="audio-finished"><button type="button" class="composer-submit" @click="toggleTrack(downloadedTrack)"><Pause v-if="currentTrack?.local_url === downloadedTrack.local_url && playbackState === 'playing'" :size="18" /><Play v-else :size="18" />{{ currentTrack?.local_url === downloadedTrack.local_url && playbackState === 'playing' ? '暂停' : '播放已下载歌曲' }}</button><button class="composer-submit" :disabled="saving" @click="save"><Download :size="18" />{{ saving ? '保存中' : '另存为' }}</button></div>
    </div>
    <div v-if="tracks.length" class="track-results">
      <div class="results-heading"><h2>搜索结果</h2><span>{{ tracks.length }} 首</span></div>
      <div class="batch-toolbar"><label class="track-check"><input type="checkbox" :checked="allChecked" :indeterminate="checkedTracks.length > 0 && !allChecked" aria-label="全选搜索结果" @change="toggleAll" /><span>{{ tracks.length > 100 ? '选择前 100 首' : '全选' }}</span></label><select class="batch-quick-select" value="" aria-label="快捷选择歌曲" @change="quickSelect"><option value="" disabled>快捷选择</option><option value="10">前 10 首</option><option value="20">前 20 首</option><option value="all">{{ tracks.length > 100 ? '前 100 首' : '全部歌曲' }}</option><option value="0">取消选择</option></select><span class="batch-selected">已选 {{ checkedTracks.length }} 首</span><button type="button" class="composer-submit" :disabled="!checkedTracks.length || activeTask || batchSaving" @click="startBatch"><LoaderCircle v-if="batchBusy" :size="18" class="spin" /><Download v-else :size="18" />批量下载</button></div>
      <div v-for="(track, index) in tracks" :key="trackKey(track)" class="track-row" :class="{ selected: selected?.webpage_url === track.webpage_url, checked: checked.has(trackKey(track)), playing: isPlaybackTrack(track) }" @click="pick(track)">
        <label class="track-check" @click.stop><input type="checkbox" :checked="checked.has(trackKey(track))" :disabled="checkedTracks.length >= 100 && !checked.has(trackKey(track))" :aria-label="`勾选 ${track.title} ${track.artist || ''}`" @change="toggleChecked(track, $event)" /></label>
        <button type="button" class="track-select" :disabled="singleActive" :aria-label="`选择 ${track.title} ${track.artist || ''}`" @click.stop="pick(track)"><span class="track-number">{{ String(index + 1).padStart(2, '0') }}</span><span class="track-name"><strong>{{ track.title }}</strong><span>{{ track.artist || '未知歌手' }}</span></span></button>
        <button type="button" class="track-play" :class="{ active: isPlaybackTrack(track) }" :title="playbackLabel(track)" :aria-label="playbackLabel(track)" :aria-pressed="isPlaybackTrack(track) && playbackState === 'playing'" :aria-busy="isPlaybackTrack(track) && playbackState === 'loading'" @click.stop="playSearchTrack(track)"><LoaderCircle v-if="isPlaybackTrack(track) && playbackState === 'loading'" :size="17" class="spin" /><Pause v-else-if="isPlaybackTrack(track) && playbackState === 'playing'" :size="17" /><Play v-else :size="17" /></button>
        <span class="track-provider">{{ localFor(track) ? '已下载' : providers[track.provider || ''] }}</span>
        <div class="track-actions" @click.stop><button type="button" class="track-play track-favorite" :class="{ liked: isLiked(track) }" :title="isLiked(track) ? '取消喜欢' : '喜欢'" :aria-label="`${isLiked(track) ? '取消喜欢' : '喜欢'} ${track.title}`" :aria-pressed="isLiked(track)" @click="toggleFavorite(track)"><Heart :size="17" :fill="isLiked(track) ? 'currentColor' : 'none'" /></button><button type="button" class="track-play track-download" :disabled="activeTask || busy || saving" :title="isDownloadingTrack(track) ? `正在下载 ${track.title}` : `下载 ${track.title} · ${audioFormat.toUpperCase()}`" :aria-label="`下载单曲 ${track.title} ${track.artist || ''}`" :aria-busy="isDownloadingTrack(track)" @click="downloadTrack(track)"><LoaderCircle v-if="isDownloadingTrack(track)" :size="17" class="spin" /><Download v-else :size="17" /></button><button v-if="embedded" type="button" class="track-play" title="更多操作" :aria-label="`更多操作 ${track.title}`" @click="emit('track-menu', track, $event)"><MoreHorizontal :size="18" /></button></div>
        <time>{{ duration(track.duration) }}</time><ArrowRight :size="16" aria-hidden="true" />
      </div>
      <div v-if="hasMore" class="music-load-more"><button type="button" class="secondary-action" :disabled="loadingMore || busy" @click="loadMore"><LoaderCircle v-if="loadingMore" :size="16" class="spin" /><ChevronDown v-else :size="16" />{{ loadingMore ? '加载中' : '加载更多' }}</button></div>
    </div>
  </div>
</template>

<style scoped>
.music-workspace .track-row { grid-template-columns: 20px minmax(0, 1fr) 36px 80px 116px 45px 18px; }
.music-workspace .track-row.checked { background: var(--music-accent-soft, #edf9f4); }
.track-actions, .audio-selected-actions { display: flex; align-items: center; gap: 4px; }
.audio-selected-actions { flex-shrink: 0; gap: 8px; }
.track-favorite.liked { color: #d06575; border-color: #e7b2bd; background: #fdf1f4; }
.music-format-toolbar { display: flex; justify-content: flex-end; padding: 12px 0; border-bottom: 1px solid #e1e6ee; }
.music-format-toolbar label { display: inline-flex; align-items: center; gap: 10px; color: #637086; font-size: 12px; }
.music-format-toolbar select { height: 32px; padding: 0 26px 0 10px; border: 1px solid #dce2ec; border-radius: 5px; color: #354760; background: #fff; font-size: 12px; }
.music-batch .batch-items li { grid-template-columns: 18px minmax(0, 1fr) 76px 76px; }
.batch-item-actions { display: flex; align-items: center; gap: 4px; }
.batch-quick-select { height: 34px; min-width: 106px; max-width: 100%; padding: 0 26px 0 10px; border: 1px solid var(--music-line, #dce2ec); border-radius: 5px; color: var(--music-text, #354760); background: var(--music-surface, #fff); font: inherit; font-size: 12px; }
.music-batch.batch-complete { display: flex; align-items: center; flex-wrap: wrap; gap: 12px 20px; padding: 14px 0; }
.batch-complete .batch-heading { flex: 1; min-width: 160px; }
.batch-complete .batch-heading h2 { font-size: 14px; }
.batch-complete .batch-heading p { font-size: 12px; }
.batch-complete .batch-actions { padding: 0; }
@media (max-width: 700px) {
  .music-workspace .track-row { grid-template-columns: 18px minmax(0, 1fr) 32px 104px; gap: 6px; padding-left: 0; padding-right: 0; }
  .music-workspace .track-row > .track-check { grid-column: 1; grid-row: 1; }
  .music-workspace .track-row > .track-select { grid-column: 2; grid-row: 1; }
  .music-workspace .track-row > .track-play { grid-column: 3; grid-row: 1; }
  .music-workspace .track-row > .track-actions { grid-column: 4; grid-row: 1; }
  .music-workspace .track-row > time, .music-workspace .track-row > svg:last-child { display: none; }
  .music-workspace .track-row > .track-provider { grid-column: 2; grid-row: 2; }
  .music-workspace .track-row .track-play { width: 32px; height: 34px; }
  .music-workspace .audio-result { display: grid; grid-template-columns: 64px minmax(0, 1fr); }
  .audio-selected-actions { grid-column: 1 / -1; justify-content: flex-end; }
  .audio-selected-actions .composer-submit { flex: 1; }
  .music-batch .batch-items li { grid-template-columns: 18px minmax(0, 1fr) 55px 64px; gap: 8px; }
  .batch-item-actions .track-play { width: 30px; height: 32px; }
}
</style>
