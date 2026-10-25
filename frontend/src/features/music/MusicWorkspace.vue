<script setup lang="ts">
import { downloadUrl } from '../../shared/media'
import { createMusicBatch, deviceBatch, cancelDeviceBatch, retryDeviceBatch } from './musicBatch'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { account } from '../accounts/auth'
import { ArrowRight, Check, ChevronDown, Download, ExternalLink, FileArchive, Heart, ListChecks, LoaderCircle, MoreHorizontal, Music2, Pause, Play, RefreshCw, Search, Square, X } from 'lucide-vue-next'
import TrackCover from './components/TrackCover.vue'
import MusicListControls from './components/MusicListControls.vue'
import { useMusicList } from './useMusicList'
import { extractHttpUrl } from '../../shared/utils/downloadInput'
import { getTracks, isLiked, libraryRevision, refreshLibrary, renewTrack, toggleFavorite, trackArtwork, trackKey, type MusicTrack } from './musicLibrary'
import { chooseFile, downloadMedia, progressText, saveRemoteFile, saveMessage, type DownloadProgress } from '../../shared/downloads'
import { hasLibraryFile, isMusicDownloaded, musicDownloadLabel, refreshMusicDownloadStatus, rememberMusicDownloads } from './musicDownloads'
import { currentTrack, isPlaybackTrack, playbackState, playTrack, togglePlayback } from './player/musicPlayer'

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
type BatchItem = Omit<Task, 'status'> & { status: Task['status'] | 'cancelled' | 'handed_off'; title: string; artist: string; provider: string; transfer?: DownloadProgress }
type Batch = {
  batch_id: string
  status: 'pending' | 'running' | 'cancelling' | 'finished' | 'failed' | 'cancelled'
  audio_format: string
  progress: number
  total: number
  finished: number
  failed: number
  cancelled: number
  handed_off?: number
  items: BatchItem[]
  download_url?: string | null
  device?: boolean
  message?: string
  delivery_status?: string
}
const props = withDefaults(defineProps<{ active?: boolean; embedded?: boolean; queueOnly?: boolean; provider?: string }>(), { active: true, embedded: false, queueOnly: false })
const emit = defineEmits<{ 'track-menu': [track: MusicTrack, event: MouseEvent]; 'source-change': [source: string] }>()
const input = ref('')
const source = ref('video')
const audioFormat = ref<'original' | 'm4a' | 'mp3'>('mp3')
const supportsDirectorySave = Boolean(window.showDirectoryPicker)
const batchMode = ref<'files' | 'directory'>(supportsDirectorySave ? 'directory' : 'files')
const transferProgress = ref('')
let activeTransfer: AbortController | undefined
function cancelTransfer() { activeTransfer?.abort(new DOMException('下载已取消', 'AbortError')) }
const busy = ref(false)
const hasSearched = ref(false)
const slowSearch = ref(false)
const selectionMode = ref(false)
let slowSearchTimer: number | undefined
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
const sourceMessages = ref<Array<{ provider: string; message: string; available?: boolean }>>([])
const sourceSummary = computed(() => sourceMessages.value.some(item => item.available === false) ? '部分平台暂不可用' : `搜索来源 · ${sourceMessages.value.length} 个平台`)
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
const batchComplete = computed(() => batch.value?.status === 'finished' && batch.value.finished + (batch.value.handed_off || 0) === batch.value.total && !batch.value.failed && !batch.value.cancelled)
const activeTask = computed(() => singleActive.value || batchActive.value)
const { filters: listFilters, artists: listArtists, filtered: visibleTracks, hasTime: listHasTime } = useMusicList(() => tracks.value, () => `search:${searchQuery.value}:${searchSource.value}`)
const downloadableTracks = computed(() => visibleTracks.value.filter(track => !isDownloaded(track)))
const checkedTracks = computed(() => tracks.value.filter(track => !isDownloaded(track) && checked.value.has(trackKey(track))))
const allChecked = computed(() => downloadableTracks.value.length > 0 && downloadableTracks.value.slice(0, 100).every(track => checked.value.has(trackKey(track))))
const visibleCheckedCount = computed(() => downloadableTracks.value.filter(track => checked.value.has(trackKey(track))).length)
const batchHeading = computed(() => batch.value?.device && batch.value.delivery_status === 'handed_off' ? '已交给浏览器下载' : batch.value?.device && batch.value.status === 'failed' && batch.value.finished + (batch.value.handed_off || 0) > 0 ? '批量下载结束 · 部分失败' : ({ pending: '等待下载', running: '批量下载中', cancelling: '正在停止后续歌曲', finished: '批量下载完成', failed: '批量下载失败', cancelled: '批量下载已停止' }[batch.value?.status || 'pending']))
const providers: Record<string, string> = { qq: 'QQ 音乐', netease: '网易云音乐', kugou: '酷狗音乐', all: '全部音乐平台', video: '视频音轨' }
const downloadedTrack = computed<Track | null>(() => selected.value && task.value?.status === 'finished' && task.value.download_url
  ? { ...selected.value, filename: task.value.filename || undefined, local_url: task.value.download_url }
  : null)

async function loadLocalTracks() {
  const revision = ++localRevision
  const candidates = [...tracks.value, ...(selected.value ? [selected.value] : [])]
  if (!candidates.length) return
  try {
    const statuses = await refreshMusicDownloadStatus(candidates)
    if (disposed || revision !== localRevision) return
    for (const track of candidates) Object.assign(track, statuses.get(trackKey(track)))
    localTracks.value = candidates.filter(hasLibraryFile)
  } catch { /* Search annotations remain available when a refresh fails. */ }
}
function localFor(track: Track) { return hasLibraryFile(track) ? track : localTracks.value.find(item => trackKey(item) === trackKey(track) && hasLibraryFile(item)) }
function isDownloaded(track: Track) { return isMusicDownloaded(localFor(track) || track) }
function downloadLabel(track: Track) { return musicDownloadLabel(localFor(track) || track) }

function playableTrack(track: Track) { return track }
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
  await toggleTrack(playableTrack(track), visibleTracks.value.map(playableTrack))
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
  if (isDownloaded(track)) { checked.value.delete(key); (event.target as HTMLInputElement).checked = false; return }
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
  checked.value = new Set(downloadableTracks.value.slice(0, Math.min(count, 100)).map(trackKey))
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
    const created = await createMusicBatch(checkedTracks.value, audioFormat.value, undefined, { local_mode: batchMode.value === 'directory' ? 'directory' : 'files' })
    if (created.batch_id.startsWith('device-')) { batch.value = deviceBatch.value; checked.value = new Set(); return }
    const response = await fetch(`/api/music/batches/${created.batch_id}`)
    if (!response.ok) throw new Error(await apiError(response))
    const data = await response.json()
    if (disposed) return
    batch.value = data
    rememberBatch(data.batch_id)
    checked.value = new Set()
    await pollBatch(data.batch_id, revision)
  } catch (failure) { batchError.value = failure instanceof Error ? failure.message : '批量任务创建失败。' }
  finally { batchBusy.value = false }
}
async function updateBatch(action: 'cancel' | 'retry') {
  if (batch.value?.device) { if (action === 'cancel') cancelDeviceBatch(); else { try { await retryDeviceBatch() } catch (failure) { batchError.value = failure instanceof Error ? failure.message : '重试失败' } }; return }
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
    const result = await saveRemoteFile(url, name)
    batchSaved.value = saveMessage(result)
  } catch (failure) {
    if (!(failure instanceof DOMException && failure.name === 'AbortError')) batchError.value = failure instanceof Error ? failure.message : '打包保存失败。'
  } finally { batchSaving.value = false }
}
function itemStatus(item: BatchItem) {
  return { pending: '等待中', running: '保存中', finished: '已保存', handed_off: '交给浏览器', failed: '失败', cancelled: '已取消' }[item.status]
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
async function downloadTrack(track: Track, force = false) {
  if (activeTask.value || busy.value || saving.value || (!force && isDownloaded(track))) return
  void pick(track)
  await download(force)
}
async function submit() {
  if (singleActive.value) return
  const text = input.value.trim()
  if (!text) { error.value = '请输入歌曲名称或粘贴链接。'; return }
  const link = extractHttpUrl(text)
  if (!link && source.value === 'video') { error.value = '请粘贴视频链接或完整的抖音分享文字。'; return }
  if (busy.value && text === searchQuery.value && source.value === searchSource.value) return
  if (batchComplete.value) dismissBatch()
  controller?.abort()
  controller = new AbortController()
  const requestController = controller
  const current = ++generation
  let timedOut = false
  const requestTimer = window.setTimeout(() => { timedOut = true; requestController.abort() }, 30000)
  window.clearTimeout(slowSearchTimer)
  slowSearch.value = false
  slowSearchTimer = window.setTimeout(() => { if (current === generation) slowSearch.value = true }, 3000)
  busy.value = true
  hasSearched.value = true
  selectionMode.value = false
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
      ? await fetch('/api/music/parse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: link }), signal: requestController.signal })
      : await fetch(`/api/music/search?q=${encodeURIComponent(text)}&source=${searchSource.value}`, { signal: requestController.signal })
    if (!response.ok) throw new Error(await apiError(response))
    const data = await response.json()
    if (current !== generation) return
    if (link) { selected.value = data; void loadLocalTracks() }
    else {
      tracks.value = data.tracks; sourceMessages.value = data.sources; hasMore.value = data.has_more
      if (!data.tracks.length && data.sources.length && data.sources.every((item: { available?: boolean }) => item.available === false)) error.value = '当前搜索平台暂不可用，请重试或切换平台。'
    }
  } catch (failure) {
    if (current !== generation) return
    if (timedOut) { error.value = link ? '链接解析超时，请重试。' : '搜索超时，请重试或切换平台。'; return }
    if (failure instanceof DOMException && failure.name === 'AbortError') return
    error.value = failure instanceof Error ? failure.message : '音乐解析失败。'
  } finally {
    window.clearTimeout(requestTimer)
    if (current === generation) { busy.value = false; window.clearTimeout(slowSearchTimer) }
  }
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
async function download(force = false) {
  if (!selected.value || activeTask.value || (!force && isDownloaded(selected.value))) return
  const original = selected.value
  const uid = account.value?.id
  const controller = new AbortController()
  activeTransfer = controller
  downloading.value = true
  error.value = ''; saved.value = ''; transferProgress.value = '正在选择保存位置…'
  stopPolling()
  try {
    const extension = audioFormat.value === 'original' ? 'm4a' : audioFormat.value
    const handle = await chooseFile(`${original.title}.${extension}`)
    transferProgress.value = '正在解析与准备…'
    const track = await renewTrack(original)
    if (disposed) return
    selected.value = track
    const result = await downloadMedia({ url: track.webpage_url, title: track.title, media_type: 'audio', audio_format: audioFormat.value,
      destination: 'device', format_id: track.token ? `music:${track.token}` : track.formats?.[0]?.format_id }, {
      handle, controller, track: original, onProgress(progress) { transferProgress.value = progressText(progress) }, onTask(data) { if (!disposed) task.value = data },
    })
    if (disposed || uid !== account.value?.id) return
    if (result.status === 'saved') rememberMusicDownloads([original], uid, { bytes: result.bytes, download_id: result.plan.download_id })
    if (result.status === 'library') { await refreshLibrary(); await loadLocalTracks() }
    else task.value = null
    saved.value = saveMessage(result)
  } catch (failure) {
    if (!(failure instanceof DOMException && failure.name === 'AbortError')) error.value = failure instanceof Error ? failure.message : '下载失败。'
    else saved.value = '下载已取消。'
  } finally { downloading.value = false; transferProgress.value = ''; if (activeTransfer === controller) activeTransfer = undefined }
}

async function save() {
  if (!task.value?.download_url) return
  const downloadUrl = task.value.download_url
  const filename = task.value.filename
  const title = selected.value?.title || 'audio'
  const original = selected.value
  const uid = account.value?.id
  saving.value = true
  error.value = ''
  try {
    const ext = filename?.split('.').pop() || 'm4a'
    const name = title.replace(/[\\/:*?"<>|]+/g, ' ').slice(0, 80) + `.${ext}`
    const result = await saveRemoteFile(downloadUrl, name, { track: original })
    if (result.status === 'saved' && original) rememberMusicDownloads([original], uid, { bytes: result.bytes })
    saved.value = saveMessage(result)
  } catch (failure) {
    if (!(failure instanceof DOMException && failure.name === 'AbortError')) error.value = failure instanceof Error ? failure.message : '保存失败。'
  } finally { saving.value = false }
}
async function search(query: string, provider = 'all') {
  input.value = query
  source.value = provider
  await submit()
}
async function showBatch(id: string) {
  if (id.startsWith('device-')) { batch.value = deviceBatch.value; return }
  stopBatchPolling()
  const revision = ++batchGeneration
  batchError.value = ''
  batchSaved.value = ''
  batchBusy.value = true
  rememberBatch(id)
  await pollBatch(id, revision)
}
defineExpose({ search, downloadTrack, isDownloaded, showBatch, activeTask, singleActive, busy, searchQuery, searchSource })
watch(() => props.provider, value => { if (value) source.value = value }, { immediate: true })
watch(deviceBatch, value => { if (value) batch.value = value }, { immediate: true })
watch(() => tracks.value.filter(track => !isDownloaded(track)), available => { const keys = new Set(available.map(trackKey)); checked.value = new Set([...checked.value].filter(key => keys.has(key))) })
watch(libraryRevision, () => { void loadLocalTracks() })
watch(() => trackArtwork(selected.value), () => { failedCover.value = false })
watch(() => props.active, active => { if (!active && batchComplete.value) dismissBatch() })
onMounted(() => {
  void loadLocalTracks()
  let id: string | null = null
  try { id = localStorage.getItem(batchStorageKey) } catch { return }
  if (id && /^[a-f0-9]{12}$/.test(id)) { batchBusy.value = true; void pollBatch(id, batchGeneration, true) }
})
onBeforeUnmount(() => { cancelTransfer(); disposed = true; localRevision++; batchGeneration++; stopBatchPolling(); generation++; controller?.abort(); stopPolling(); window.clearTimeout(slowSearchTimer) })
</script>

<template>
  <div class="music-workspace" :class="{ embedded, selecting: selectionMode }" :role="embedded ? undefined : 'tabpanel'" :id="embedded ? undefined : 'music-panel'" :aria-labelledby="embedded ? undefined : 'music-tab'" :aria-busy="busy">
    <form v-if="!embedded && !queueOnly" class="link-composer" @submit.prevent="submit">
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
    <div v-if="embedded && !queueOnly" class="music-format-toolbar"><label class="music-mobile-source"><span class="sr-only">搜索平台</span><select v-model="source" aria-label="搜索平台" @change="emit('source-change', source)"><option value="all">全部平台</option><option value="qq">QQ 音乐</option><option value="netease">网易云音乐</option><option value="kugou">酷狗音乐</option><option value="video">视频音轨</option></select></label><label><span class="music-format-label">下载格式</span><select v-model="audioFormat" :disabled="activeTask" aria-label="音频导出格式"><option value="m4a">M4A</option><option value="mp3">MP3</option></select></label><button v-if="tracks.length" type="button" class="music-selection-toggle" :aria-pressed="selectionMode" @click="selectionMode = !selectionMode; checked = new Set()"><ListChecks :size="16" />{{ selectionMode ? '取消' : '多选' }}</button></div>
    <div v-if="error && !queueOnly" class="music-search-error" role="alert"><p class="error-text">{{ error }}</p><button v-if="hasSearched && !selected && !busy" type="button" class="mc-command" @click="submit"><RefreshCw :size="16" />重试</button></div>
    <section v-if="busy && !queueOnly" class="music-search-loading" role="status" aria-live="polite"><p><LoaderCircle :size="20" class="spin" /><span>{{ slowSearch ? '仍在处理中，请稍候…' : hasLink ? '正在解析链接…' : `正在搜索「${searchQuery}」…` }}</span></p><div v-for="row in 3" :key="row" class="music-search-skeleton" aria-hidden="true"><span /><span /></div></section>
    <div v-else-if="!queueOnly && !error && !selected && !tracks.length" class="mc-empty music-search-empty"><Search :size="30" /><p>{{ hasSearched ? `没有找到「${searchQuery}」的相关歌曲` : '输入歌名、歌手，或粘贴链接开始搜索' }}</p><small v-if="hasSearched">试试修改关键词或切换搜索平台</small></div>
    <p v-if="transferProgress" class="success-text" role="status">{{ transferProgress }} <button type="button" class="secondary-action" @click="cancelTransfer">取消下载</button></p>
    <p v-if="saved && !queueOnly" class="success-text" role="status">{{ saved }}</p>
    <p v-if="batchError" class="error-text" role="alert">{{ batchError }}</p>
    <p v-if="batchSaved" class="success-text" role="status">{{ batchSaved }}</p>
    <section v-if="batch" class="music-batch" :class="{ 'batch-complete': batchComplete }" aria-label="批量下载队列">
      <div class="batch-heading"><div><h2>{{ batchHeading }}</h2><p v-if="batch.device">{{ batch.message }}</p><p v-else>成功 {{ batch.finished }} / {{ batch.total }}<span v-if="batch.failed"> · 失败 {{ batch.failed }}</span><span v-if="batch.cancelled"> · 已取消 {{ batch.cancelled }}</span> · {{ batch.audio_format.toUpperCase() }}</p></div><strong v-if="!batchComplete">{{ Math.round(batch.progress) }}%</strong></div>
      <progress v-if="!batchComplete" :value="batch.progress" max="100" aria-label="批量音频下载进度" />
      <div class="batch-actions">
        <button v-if="batchActive" type="button" class="secondary-action" :disabled="(batchBusy && !batch.device) || batch.status === 'cancelling'" @click="updateBatch('cancel')"><Square :size="15" />{{ batch.status === 'cancelling' ? '等待当前歌曲结束' : '停止后续' }}</button>
        <button v-if="(batch.failed || batch.cancelled) && !batchActive" type="button" class="secondary-action" :disabled="batchSaving" @click="updateBatch('retry')"><RefreshCw :size="16" />{{ batch.failed ? '重试失败歌曲' : '重试未完成歌曲' }}</button>
        <button v-if="batch.download_url" type="button" class="composer-submit" :disabled="batchSaving" @click="saveBatch"><LoaderCircle v-if="batchSaving" :size="18" class="spin" /><FileArchive v-else :size="18" />{{ batchSaving ? '打包保存中' : `保存 ZIP · ${batch.finished} 首` }}</button>
        <button v-if="batchPollingError" type="button" class="track-play" title="刷新队列" aria-label="刷新队列" @click="stopBatchPolling(); pollBatch(batch.batch_id)"><RefreshCw :size="17" /></button>
        <button v-if="!batchActive" type="button" class="track-play" :disabled="batchSaving" title="关闭下载记录" aria-label="关闭下载记录" @click="dismissBatch"><X :size="18" /></button>
      </div>
      <ol v-if="!batchComplete || (batch.device && batch.handed_off)" class="batch-items">
        <li v-for="item in batch.items" :key="item.task_id" :class="item.status">
          <Check v-if="item.status === 'finished'" :size="16" /><LoaderCircle v-else-if="item.status === 'running'" :size="16" class="spin" /><X v-else-if="item.status === 'failed' || item.status === 'cancelled'" :size="16" /><Music2 v-else :size="16" />
          <div class="batch-item-name"><strong>{{ item.title }}</strong><span>{{ item.artist }} · {{ providers[item.provider] }}</span><p v-if="item.status === 'running' && item.message">{{ item.message }}</p><progress v-if="item.status === 'running' && item.transfer" :value="item.transfer.phase === 'transferring' && item.transfer.total ? Math.min(99, item.transfer.bytes / item.transfer.total * 100) : undefined" max="100" :aria-label="`${item.title} 保存进度`" /><p v-if="item.error">{{ item.error }}</p></div>
          <span class="batch-item-status">{{ itemStatus(item) }}</span>
          <a v-if="item.status === 'handed_off' && item.download_url" class="secondary-action" :href="downloadUrl(item.download_url)" :download="item.filename || item.title" :aria-label="`保存歌曲 ${item.title}`">保存此歌曲</a><div v-else-if="item.status === 'finished' && item.download_url" class="batch-item-actions"><button type="button" class="track-play" :title="`播放已下载的 ${item.title}`" :aria-label="`播放已下载的 ${item.title}`" @click="playBatchItem(item)"><Play :size="16" /></button><a class="track-play" :href="downloadUrl(item.download_url)" :download="`${item.artist} - ${item.title}.${batch.audio_format}`" @click.prevent="saveRemoteFile(item.download_url, item.filename || `${item.artist} - ${item.title}.${batch.audio_format}`).catch(failure => batchError = failure.message)" :title="`保存 ${item.title}`" :aria-label="`保存 ${item.title}`"><Download :size="16" /></a></div><span v-else class="batch-item-placeholder" />
        </li>
      </ol>
    </section>
    <details v-if="sourceMessages.length && !queueOnly && !busy" class="source-messages"><summary>{{ sourceSummary }}<span>查看详情</span></summary><p v-for="item in sourceMessages" :key="item.provider"><strong>{{ providers[item.provider] }}</strong>{{ item.message }}</p></details>
    <div v-if="selected && !queueOnly" ref="audioResult" class="audio-result">
      <div class="audio-cover"><img v-if="trackArtwork(selected) && !failedCover" :src="trackArtwork(selected) || undefined" :alt="selected.title" @error="failedCover = true" /><Music2 v-else :size="32" /></div>
      <div class="audio-meta"><span class="status-chip">{{ selected.provider ? providers[selected.provider] : '视频音轨' }}</span><h2>{{ selected.title }}</h2><div class="meta-row"><span>{{ selected.artist || selected.uploader }}</span><time>{{ duration(selected.duration) }}</time><span v-if="selected.access">{{ selected.access }}</span><a :href="selected.webpage_url" target="_blank" rel="noreferrer">原始页面<ExternalLink :size="13" /></a></div></div>
      <div class="audio-selected-actions"><button class="track-play track-favorite" :class="{ liked: isLiked(selected) }" type="button" :title="isLiked(selected) ? '取消喜欢' : '喜欢'" :aria-label="`${isLiked(selected) ? '取消喜欢' : '喜欢'} ${selected.title}`" :aria-pressed="isLiked(selected)" @click="toggleFavorite(selected)"><Heart :size="17" :fill="isLiked(selected) ? 'currentColor' : 'none'" /></button><button v-if="embedded" class="track-play" type="button" title="更多操作" :aria-label="`更多操作 ${selected.title}`" @click="emit('track-menu', selected, $event)"><MoreHorizontal :size="18" /></button><span v-if="isDownloaded(selected)" class="status-chip">{{ downloadLabel(selected) }}</span><button v-if="!isDownloaded(selected)" class="composer-submit" :disabled="activeTask || busy" @click="download()"><LoaderCircle v-if="activeTask" :size="18" class="spin" /><Download v-else :size="18" />{{ activeTask ? '处理中' : `下载 ${audioFormat.toUpperCase()}` }}</button></div>
    </div>
    <div v-if="task" class="audio-task" :class="{ failed: task.status === 'failed' }" aria-live="polite">
      <div class="task-head"><span>{{ task.error || task.message }}</span><strong>{{ Math.round(task.progress) }}%</strong></div>
      <progress :value="task.progress" max="100" aria-label="音轨下载进度" />
      <button v-if="pollingError" class="secondary-action" @click="poll(task.task_id)"><RefreshCw :size="16" />刷新任务</button>
      <div v-if="downloadedTrack" class="audio-finished"><button type="button" class="composer-submit" @click="toggleTrack(downloadedTrack)"><Pause v-if="currentTrack?.local_url === downloadedTrack.local_url && playbackState === 'playing'" :size="18" /><Play v-else :size="18" />{{ currentTrack?.local_url === downloadedTrack.local_url && playbackState === 'playing' ? '暂停' : '播放已下载歌曲' }}</button><button class="composer-submit" :disabled="saving" @click="save"><Download :size="18" />{{ saving ? '保存中' : '另存为' }}</button></div>
    </div>
    <div v-if="tracks.length && !queueOnly" class="track-results">
      <div class="results-heading"><h2>搜索结果</h2><span>{{ tracks.length }} 首</span></div>
      <MusicListControls v-model="listFilters" :artists="listArtists" :total="tracks.length" :count="visibleTracks.length" :has-time="listHasTime" />
      <p v-if="!visibleTracks.length" role="status">没有匹配的歌曲，请调整筛选条件</p>
      <div class="batch-toolbar"><label class="track-check"><input type="checkbox" :checked="allChecked" :indeterminate="visibleCheckedCount > 0 && !allChecked" aria-label="全选搜索结果" @change="toggleAll" /><span>{{ downloadableTracks.length > 100 ? '选择前 100 首' : '全选' }}</span></label><select class="batch-quick-select" value="" aria-label="快捷选择歌曲" @change="quickSelect"><option value="" disabled>快捷选择</option><option value="10">前 10 首</option><option value="20">前 20 首</option><option value="all">{{ downloadableTracks.length > 100 ? '前 100 首' : '全部歌曲' }}</option><option value="0">取消选择</option></select><span class="batch-selected">已选 {{ checkedTracks.length }} 首 · 跳过 {{ visibleTracks.length - downloadableTracks.length }} 首已下载</span><select v-model="batchMode" aria-label="批量保存方式"><option value="files">浏览器下载（逐首）</option><option v-if="supportsDirectorySave" value="directory">保存到本机文件夹</option></select><button type="button" class="composer-submit" :disabled="!checkedTracks.length || activeTask || batchSaving" @click="startBatch"><LoaderCircle v-if="batchBusy" :size="18" class="spin" /><Download v-else :size="18" />批量下载</button></div>
      <div v-for="(track, index) in visibleTracks" :key="trackKey(track)" class="track-row" :class="{ selected: selected?.webpage_url === track.webpage_url, checked: checked.has(trackKey(track)), playing: isPlaybackTrack(track) }" @click="pick(track)">
        <label class="track-check" @click.stop><input type="checkbox" :checked="checked.has(trackKey(track))" :disabled="isDownloaded(track) || (checkedTracks.length >= 100 && !checked.has(trackKey(track)))" :aria-label="`勾选 ${track.title} ${track.artist || ''}`" @change="toggleChecked(track, $event)" /></label>
        <button type="button" class="track-select" :disabled="singleActive" :aria-label="`选择 ${track.title} ${track.artist || ''}`" @click.stop="pick(track)"><span class="track-number">{{ String(index + 1).padStart(2, '0') }}</span><TrackCover v-if="embedded" class="search-track-cover" :src="trackArtwork(track)" :title="track.title" /><span class="track-name"><strong>{{ track.title }}</strong><span>{{ track.artist || '未知歌手' }}<span v-if="embedded" class="search-track-provider"> · {{ downloadLabel(track) || providers[track.provider || ''] }}</span></span></span></button>
        <button type="button" class="track-play" :class="{ active: isPlaybackTrack(track) }" :title="playbackLabel(track)" :aria-label="playbackLabel(track)" :aria-pressed="isPlaybackTrack(track) && playbackState === 'playing'" :aria-busy="isPlaybackTrack(track) && playbackState === 'loading'" @click.stop="playSearchTrack(track)"><LoaderCircle v-if="isPlaybackTrack(track) && playbackState === 'loading'" :size="17" class="spin" /><Pause v-else-if="isPlaybackTrack(track) && playbackState === 'playing'" :size="17" /><Play v-else :size="17" /></button>
        <span class="track-provider">{{ downloadLabel(track) || providers[track.provider || ''] }}</span>
        <div class="track-actions" @click.stop><button type="button" class="track-play track-favorite" :class="{ liked: isLiked(track) }" :title="isLiked(track) ? '取消喜欢' : '喜欢'" :aria-label="`${isLiked(track) ? '取消喜欢' : '喜欢'} ${track.title}`" :aria-pressed="isLiked(track)" @click="toggleFavorite(track)"><Heart :size="17" :fill="isLiked(track) ? 'currentColor' : 'none'" /></button><button v-if="!isDownloaded(track)" type="button" class="track-play track-download" :disabled="activeTask || busy || saving" :title="isDownloadingTrack(track) ? `正在下载 ${track.title}` : `下载 ${track.title} · ${audioFormat.toUpperCase()}`" :aria-label="`下载单曲 ${track.title} ${track.artist || ''}`" :aria-busy="isDownloadingTrack(track)" @click="downloadTrack(track)"><LoaderCircle v-if="isDownloadingTrack(track)" :size="17" class="spin" /><Download v-else :size="17" /></button><button v-if="embedded" type="button" class="track-play" title="更多操作" :aria-label="`更多操作 ${track.title}`" @click="emit('track-menu', track, $event)"><MoreHorizontal :size="18" /></button></div>
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
.music-batch .batch-items { flex-basis: 100%; min-width: 0; width: 100%; }
.batch-item-name p, .batch-heading p { overflow-wrap: anywhere; }
.batch-item-name strong { color: var(--music-text, #233249); }
.batch-item-name progress { display: block; width: 100%; max-width: 100%; height: 5px; margin-top: 6px; accent-color: var(--music-accent, #228c74); }
.batch-item-status { white-space: normal; }
.batch-items > li > .secondary-action { min-width: 0; padding: 6px; white-space: normal; text-align: center; font-size: 12px; color: var(--music-highlight, #109b6d); background: var(--music-soft, #eef1ec); border-color: var(--music-line, #dce2ec); }
.batch-item-actions { display: flex; align-items: center; gap: 4px; }
.batch-quick-select { height: 34px; min-width: 106px; max-width: 100%; padding: 0 26px 0 10px; border: 1px solid var(--music-line, #dce2ec); border-radius: 5px; color: var(--music-text, #354760); background: var(--music-surface, #fff); font: inherit; font-size: 12px; }
.music-batch.batch-complete { display: flex; align-items: center; flex-wrap: wrap; gap: 12px 20px; padding: 14px 0; }
.batch-complete .batch-heading { flex: 1; min-width: 160px; }
.batch-complete .batch-heading h2 { font-size: 14px; }
.batch-complete .batch-heading p { font-size: 12px; }
.batch-complete .batch-actions { padding: 0; }
.search-track-cover, .search-track-provider, .music-mobile-source, .music-selection-toggle { display: none; }
.music-format-toolbar { gap: 12px; align-items: center; }
.music-search-error { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin: 14px 0; }
.music-search-error .error-text { flex: 1; margin: 0; }
.music-search-loading { color: var(--music-highlight, #109b6d); }
.music-search-loading > p { display: flex; align-items: center; gap: 9px; min-height: 44px; margin: 12px 0 2px; font-size: 14px; }
.music-search-skeleton { display: flex; align-items: center; gap: 12px; min-height: 70px; border-bottom: 1px solid var(--music-line, #e1e6ee); }
.music-search-skeleton > span:first-child { width: 44px; height: 44px; border-radius: 8px; background: var(--music-soft, #eef1ec); }
.music-search-skeleton > span:last-child { width: 48%; height: 28px; border-radius: 5px; background: var(--music-soft, #eef1ec); }
.music-search-empty { gap: 14px; text-align: center; }
.music-search-empty p { overflow-wrap: anywhere; }
.music-search-empty small { font-size: 12px; }
.source-messages { margin-top: 8px; padding: 8px 0; font-size: 12px; }
.source-messages summary { min-height: 36px; color: var(--music-muted, #637086); cursor: pointer; }
.source-messages summary span { margin-left: 10px; font-size: 11px; }
.source-messages p { color: var(--music-muted, #637086); font-size: 12px; }
.source-messages strong { color: var(--music-text, #364152); }
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
  .music-batch .batch-items li { grid-template-columns: 18px minmax(0, 1fr) 64px 80px; gap: 8px; }
  .batch-item-actions .track-play { width: 30px; height: 32px; }
}
@media (max-width: 760px) {
  .music-format-toolbar { justify-content: flex-end; padding: 0 0 10px; gap: 10px; border-color: var(--music-line, #e1e6ee); }
  .music-format-toolbar .music-mobile-source { display: inline-flex; margin-right: auto; min-width: 0; }
  .music-mobile-source .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
  .music-format-label { display: none; }
  .music-format-toolbar select { min-height: 44px; height: 44px; padding-left: 9px; padding-right: 21px; max-width: 125px; font-size: 12px; border: 0; border-radius: 9px; background: var(--music-soft, #eef1ec); color: var(--music-text, #354760); }
  .music-selection-toggle { display: inline-flex; align-items: center; justify-content: center; gap: 4px; min-height: 44px; padding: 0 3px; border: 0; background: transparent; color: var(--music-muted, #637086); font-size: 12px; white-space: nowrap; }
  .music-selection-toggle[aria-pressed=true] { color: var(--music-highlight, #109b6d); }
  .embedded:not(.selecting) .batch-toolbar, .embedded:not(.selecting) .track-row > .track-check { display: none; }
  .music-workspace.embedded .track-row { grid-template-columns: minmax(0, 1fr) 44px 88px; gap: 0; min-height: 70px; padding: 8px 0; }
  .music-workspace.embedded.selecting .track-row { grid-template-columns: 24px minmax(0, 1fr) 44px 88px; }
  .music-workspace.embedded .track-row > .track-select { grid-column: 1; grid-row: 1; }
  .music-workspace.embedded .track-row > .track-play { grid-column: 2; grid-row: 1; }
  .music-workspace.embedded .track-row > .track-actions { grid-column: 3; grid-row: 1; gap: 0; }
  .music-workspace.embedded.selecting .track-row > .track-select { grid-column: 2; }
  .music-workspace.embedded.selecting .track-row > .track-play { grid-column: 3; }
  .music-workspace.embedded.selecting .track-row > .track-actions { grid-column: 4; }
  .music-workspace.embedded .track-row .track-play { width: 44px; height: 44px; padding: 0; border: 0; background: transparent; }
  .music-workspace.embedded .track-row .track-favorite, .music-workspace.embedded .track-number, .music-workspace.embedded .track-row > .track-provider, .music-workspace.embedded .track-row > time { display: none; }
  .music-workspace.embedded .track-row .track-select { grid-template-columns: 40px minmax(0, 1fr); gap: 10px; min-height: 44px; }
  .search-track-cover { display: inline-flex; width: 40px; height: 40px; border-radius: 8px; }
  .search-track-provider { display: inline; }
  .track-name strong, .track-name > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .track-name strong { font-size: 14px; font-weight: 500; }
  .track-name > span { font-size: 11px; color: var(--music-muted, #66768e); }
  .embedded .track-play.track-download { color: var(--music-highlight, #109b6d); }
  .embedded .batch-toolbar { gap: 8px; }
  .embedded .batch-toolbar .composer-submit { min-height: 44px; }
  .results-heading { padding-top: 12px; padding-bottom: 8px; }
  .source-messages { margin-top: 4px; padding: 2px 0; }
  .source-messages summary { min-height: 44px; display: flex; align-items: center; }
  .source-messages summary span { margin-left: auto; }
}
@media (max-width: 360px) {
  .music-workspace.embedded .track-row .track-select { grid-template-columns: 32px minmax(0, 1fr); gap: 6px; }
  .search-track-cover { width: 32px; height: 32px; }
  .music-workspace.embedded.selecting .track-row .track-select { grid-template-columns: minmax(0, 1fr); }
  .embedded.selecting .search-track-cover { display: none; }
}
</style>
