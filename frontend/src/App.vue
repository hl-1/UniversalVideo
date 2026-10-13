<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MindMapCanvas from './MindMapCanvas.vue'
import { canSeekMedia, clampSeekTime, filterMindMapTask, type MindMapTask } from './mindMap'
import { getRecentLinks } from './recentLinks'
import VideoHistory from './VideoHistory.vue'
import { readRecordLocation, recordLocation, type MapView, type VideoTaskReference, type VideoView } from './videoHistory'
import './video-history.css'
import { readPreviewUrl, selectPreviewFormat } from './videoPreview'
import WatermarkEditor from './WatermarkEditor.vue'
import MusicCenter from './MusicCenter.vue'
import MusicPlayer from './MusicPlayer.vue'
import AdminWorkspace from './AdminWorkspace.vue'
import { account } from './auth'
import { pausePlayback } from './musicPlayer'
import { api, type MusicTrack } from './musicLibrary'
import { extractHttpUrl } from './downloadInput'
import { ArrowRight, Download, Eraser, ExternalLink, History, LoaderCircle, Music2, Play, RotateCcw, ShieldCheck, Trash2, Video, X } from 'lucide-vue-next'

type VideoFormat = {
  format_id: string
  label: string
  ext?: string | null
  resolution?: string | null
  filesize?: number | null
}

type ParseResult = {
  record_id?: string | null
  title: string
  thumbnail?: string | null
  duration?: number | null
  uploader?: string | null
  webpage_url?: string | null
  formats: VideoFormat[]
}

type TaskStatus = 'pending' | 'running' | 'finished' | 'failed' | 'cancelled'
type PreviewState = 'idle' | 'loading' | 'ready' | 'error'
type SummaryTab = 'summary' | 'transcript' | 'mind-map'

type TaskResult = {
  task_id: string
  status: TaskStatus
  progress: number
  message: string
  filename?: string | null
  download_url?: string | null
  error?: string | null
}

type SubtitleSegment = {
  start: number
  end?: number | null
  timestamp: string
  text: string
}

type SummaryResult = {
  title: string
  webpage_url: string
  language: string
  source: string
  summary_markdown: string
  transcript: SubtitleSegment[]
  markdown_url?: string | null
  json_url?: string | null
}

type SummaryTaskResult = {
  task_id: string
  status: TaskStatus
  progress: number
  message: string
  error?: string | null
  result?: SummaryResult | null
}

type FilePickerWritable = {
  write(data: Blob): Promise<void>
  close(): Promise<void>
}

type FilePickerHandle = {
  createWritable(): Promise<FilePickerWritable>
}

declare global {
  interface Window {
    showSaveFilePicker?: (options?: {
      suggestedName?: string
      types?: Array<{
        description: string
        accept: Record<string, string[]>
      }>
    }) => Promise<FilePickerHandle>
  }
}

const url = ref('')
const mode = ref<'video' | 'music'>('video')
const appPage = ref<'music' | 'tools' | 'admin'>('music')
const musicCenter = ref<{ download: (track: MusicTrack) => Promise<void> } | null>(null)
async function openMusicTools(view: 'video' | 'watermark') {
  appPage.value = 'tools'
  if (view === 'video' && !activeRecordId.value && lastRecordId.value) await openVideoRecord(lastRecordId.value)
  void nextTick(() => document.getElementById(view === 'watermark' ? 'watermark' : 'workspace')?.scrollIntoView())
}
function onToolsPlay(event: Event) {
  if (event.target instanceof HTMLVideoElement) pausePlayback()
}
function returnMusicCenter() {
  appPage.value = 'music'
  void nextTick(() => window.scrollTo({ top: 0 }))
}
function downloadFromPlayer(track: MusicTrack) {
  returnMusicCenter()
  void musicCenter.value?.download(track)
}
const selectedFormat = ref('')
const parseResult = ref<ParseResult | null>(null)
const task = ref<TaskResult | null>(null)
const summaryTask = ref<SummaryTaskResult | null>(null)
const mindMapTask = ref<MindMapTask | null>(null)
const loadingParse = ref(false)
const loadingDownload = ref(false)
const loadingSummary = ref(false)
const loadingMindMap = ref(false)
const savingFile = ref(false)
const errorMessage = ref('')
type BrowserSession = { status: 'closed' | 'waiting' | 'open' | 'expired' | 'error'; visible: boolean; expires_in: number; url?: string }
const browserSession = ref<BrowserSession | null>(null)
const verificationTarget = ref('')
const browserBusy = ref(false)
let browserTimer: number | undefined
let browserRevision = 0
let browserDisposed = false
const verificationMessage = computed(() => {
  if (browserBusy.value) return '正在更新验证窗口…'
  if (browserSession.value?.visible) return browserSession.value.status === 'waiting' ? '抖音正在等待安全验证' : '验证窗口已打开'
  if (browserSession.value?.status === 'expired') return '验证窗口已超时关闭，会话已保留'
  if (browserSession.value?.status === 'error') return '验证窗口未能打开'
  if (browserSession.value?.status === 'closed') return '验证窗口已关闭，会话已保留'
  return '抖音浏览器会话已保留'
})
const saveMessage = ref('')
const recentLinks = ref<Array<{ id: string; url: string; submitted_at: number }>>([])
const showRecentLinks = ref(false)
const pollingTimer = ref<number | undefined>()
const summaryPollingTimer = ref<number | undefined>()
const mindMapPollingTimer = ref<number | undefined>()
const coverFailed = ref(false)
const saveHandle = ref<FilePickerHandle | null>(null)
const previewState = ref<PreviewState>('idle')
const previewUrl = ref('')
const previewError = ref('')
const previewVideoRef = ref<HTMLVideoElement | null>(null)
const pendingSeekTime = ref<number | null>(null)
const playbackTime = ref(0)
const summaryTab = ref<SummaryTab>('summary')
const videoWorkspace = ref<'current' | 'history'>('current')
const historyPanel = ref<{ refresh: () => Promise<void> } | null>(null)
const activeRecordId = ref('')
const lastRecordId = ref('')
const recordMessage = ref('')
const relatedTasks = ref<VideoTaskReference[]>([])
const restoredView = ref<VideoView | null>(null)
const currentMapView = ref<MapView | null>(null)
const previousMindMap = ref<MindMapTask | null>(null)
const renderedMindMap = computed(() => mindMapTask.value?.result ? mindMapTask.value : previousMindMap.value)
const summaryVersions = computed(() => relatedTasks.value.filter(item => item.kind === 'summary'))
const mindMapVersions = computed(() => relatedTasks.value.filter(item => item.kind === 'mind_map' && item.summary_task_id === summaryTask.value?.task_id))
const viewSaveStatus = ref('')
const localVideoUrl = ref('')
const localPreviewFailed = ref(false)
let restoringRecord = false
let recordRevision = 0
let summaryRevision = 0
let mapRevision = 0
let previewRevision = 0
let formatsFresh = false
let viewSaveTimer: number | undefined
let viewSaveQueue: Promise<void> = Promise.resolve()
let lastPlaybackSaved = 0

function changeMode(next: 'video' | 'music', focus = false) {
  if (next === 'music') { returnMusicCenter(); return }
  mode.value = next
  if (focus) document.getElementById(`${next}-tab`)?.focus()
}

const supported = [
  { label: 'B站', href: 'https://www.bilibili.com/' },
  { label: 'YouTube', href: 'https://www.youtube.com/' },
  { label: '抖音公开视频', href: 'https://www.douyin.com/' },
  { label: '小红书公开视频', href: 'https://www.xiaohongshu.com/' },
  { label: '微博', href: 'https://weibo.com/' },
  { label: '更多 yt-dlp 支持站点', href: 'https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md' },
]
const taskTone = computed(() => task.value?.status || 'idle')
const summaryTone = computed(() => summaryTask.value?.status || 'idle')
const mindMapTone = computed(() => mindMapTask.value?.status || 'idle')
const canDownload = computed(() => Boolean(parseResult.value && url.value.trim() && !loadingDownload.value))
const canSummarize = computed(() => Boolean(parseResult.value && url.value.trim() && !loadingSummary.value))
const hasRecentLinks = computed(() => recentLinks.value.length > 0)
const sourceHost = computed(() => {
  const target = parseResult.value?.webpage_url
  if (!target) return ''
  try {
    return new URL(target).hostname.replace(/^www\./, '')
  } catch {
    return ''
  }
})
const previewFormat = computed(() => {
  const host = sourceHost.value
  if (!parseResult.value || (host !== 'douyin.com' && !host.endsWith('.douyin.com'))) return null
  return selectPreviewFormat(parseResult.value.formats, selectedFormat.value)
})
const canPreview = computed(() => Boolean((previewFormat.value || (localVideoUrl.value && !localPreviewFailed.value)) && previewState.value !== 'loading'))

function formatDuration(seconds?: number | null) {
  if (!seconds) return ''
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`
}

function safeFilename(name: string, ext: string) {
  const clean = name
    .replace(/[\\/:*?"<>|]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 80)
  return `${clean || 'VideoDream'}-${Date.now()}.${ext}`
}

function plainMarkdown(markdown?: string | null) {
  if (!markdown) return ''
  return markdown
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .replace(/\[(.*?)\]\((.*?)\)/g, '$1')
    .trim()
}

function suggestedExtension() {
  const selected = parseResult.value?.formats.find((item) => item.format_id === selectedFormat.value)
  const ext = selected?.ext?.toLowerCase()
  if (ext === 'm4a' || ext === 'mp3' || selected?.label.includes('音频')) return 'm4a'
  return 'mp4'
}

function stopSummaryPolling() {
  if (summaryPollingTimer.value) {
    window.clearInterval(summaryPollingTimer.value)
    summaryPollingTimer.value = undefined
  }
}

function stopMindMapPolling() {
  if (mindMapPollingTimer.value) {
    window.clearInterval(mindMapPollingTimer.value)
    mindMapPollingTimer.value = undefined
  }
}

async function loadRecentLinks() {
  try {
    const key = `videodream.recentLinks.${account.value?.id}`
    const old = getRecentLinks(window.localStorage, key)
    const data = old.length ? await api('/api/videos/recent-links/migrate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ links: old }) }) : await api('/api/videos/recent-links')
    recentLinks.value = data.items
    if (old.length) window.localStorage.removeItem(key)
  } catch (failure) { errorMessage.value = failure instanceof Error ? failure.message : '最近链接加载失败。' }
}

async function removeRecentLink(id?: string) {
  try {
    const data = await api(`/api/videos/recent-links${id ? `/${id}` : ''}`, { method: 'DELETE' })
    recentLinks.value = data.items
  } catch (failure) { errorMessage.value = failure instanceof Error ? failure.message : '删除最近链接失败。' }
}

function viewPayload(): VideoView {
  return { tab: summaryTab.value, summary_task_id: summaryTask.value?.task_id || null,
    mind_map_id: renderedMindMap.value?.task_id || null,
    selected_format: selectedFormat.value.startsWith('sniff') ? `saved:${Math.max(0, parseResult.value?.formats.findIndex(item => item.format_id === selectedFormat.value) ?? 0)}` : selectedFormat.value,
    playback_time: previewState.value === 'ready' && previewVideoRef.value ? previewVideoRef.value.currentTime : playbackTime.value,
    map_view: currentMapView.value }
}

function saveVideoView(keepalive = false): Promise<void> {
  window.clearTimeout(viewSaveTimer)
  if (!activeRecordId.value || restoringRecord) return Promise.resolve()
  const recordId = activeRecordId.value
  const payload = viewPayload()
  viewSaveQueue = viewSaveQueue.then(async () => {
    try {
      await api(`/api/videos/records/${recordId}/view`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), keepalive })
      if (activeRecordId.value === recordId) viewSaveStatus.value = '已保存'
    } catch (failure) { if (activeRecordId.value === recordId) viewSaveStatus.value = failure instanceof Error ? failure.message : '视图保存失败。' }
  })
  return viewSaveQueue
}

function scheduleVideoView() {
  if (restoringRecord || !activeRecordId.value) return
  viewSaveStatus.value = '保存中'
  window.clearTimeout(viewSaveTimer)
  viewSaveTimer = window.setTimeout(() => void saveVideoView(), 650)
}

function changeMapView(view: MapView) { currentMapView.value = view; scheduleVideoView() }
function savePlaybackProgress() {
  if (!previewVideoRef.value || previewState.value !== 'ready' || previewVideoRef.value.readyState < 1) return
  playbackTime.value = previewVideoRef.value.currentTime
  if (Date.now() - lastPlaybackSaved > 5000) {
    lastPlaybackSaved = Date.now()
    scheduleVideoView()
  }
}

async function refreshTaskReferences() {
  if (!activeRecordId.value) return
  const id = activeRecordId.value
  const revision = recordRevision
  const detail = await api(`/api/videos/records/${id}`)
  if (revision === recordRevision && id === activeRecordId.value) relatedTasks.value = detail.tasks
}

async function openVideoRecord(id: string, requestedTab?: SummaryTab) {
  const revision = ++recordRevision
  await saveVideoView()
  if (revision !== recordRevision) return
  restoringRecord = true
  loadingParse.value = false; loadingSummary.value = false; loadingMindMap.value = false; loadingDownload.value = false
  appPage.value = 'tools'; videoWorkspace.value = 'current'
  errorMessage.value = ''; saveMessage.value = ''; viewSaveStatus.value = ''
  stopPolling(); stopSummaryPolling(); stopMindMapPolling(); resetPreview()
  try {
    const detail = await api(`/api/videos/records/${id}`)
    if (revision !== recordRevision) return
    activeRecordId.value = id; lastRecordId.value = id
    url.value = detail.url
    parseResult.value = detail.result ? { ...detail.result, record_id: id } : null
    selectedFormat.value = detail.view?.selected_format || detail.result?.formats[0]?.format_id || ''
    formatsFresh = false; coverFailed.value = false; saveHandle.value = null
    relatedTasks.value = detail.tasks; restoredView.value = detail.view
    playbackTime.value = detail.view?.playback_time || 0
    currentMapView.value = detail.view?.map_view || null
    summaryTab.value = requestedTab || detail.view?.tab || 'summary'
    recordMessage.value = detail.error || (detail.status === 'running' ? '视频正在解析' : '')
    summaryTask.value = null; mindMapTask.value = null; previousMindMap.value = null; task.value = null
    localVideoUrl.value = ''; localPreviewFailed.value = false
    const summary = summaryVersions.value.find(item => item.task_id === detail.view?.summary_task_id) || summaryVersions.value[0]
    if (summary) {
      const restoredSummary = await api(`/api/summaries/${summary.task_id}`)
      if (revision !== recordRevision) return
      summaryTask.value = restoredSummary
      if (['pending', 'running'].includes(summaryTask.value!.status)) summaryPollingTimer.value = window.setInterval(() => pollSummary(summary.task_id), 1400)
      await restoreMindMapVersions(summary.task_id, detail.view?.mind_map_id)
    }
    if (revision !== recordRevision) return
    const download = detail.tasks.find((item: VideoTaskReference) => item.kind === 'download' && item.status === 'finished' && /\.(mp4|webm)$/i.test(item.filename || '')) || detail.tasks.find((item: VideoTaskReference) => item.kind === 'download' && item.status === 'finished') || detail.tasks.find((item: VideoTaskReference) => item.kind === 'download')
    if (download) {
      const restoredTask = await api(`/api/tasks/${download.task_id}`)
      if (revision !== recordRevision) return
      task.value = restoredTask
      if (task.value?.status === 'finished' && /\.(mp4|webm)$/i.test(task.value.filename || '')) localVideoUrl.value = task.value.download_url || ''
      if (['pending', 'running'].includes(task.value?.status || '')) pollingTimer.value = window.setInterval(() => pollTask(download.task_id), 1200)
    }
    if (detail.view?.mind_map_id !== renderedMindMap.value?.task_id) currentMapView.value = null
    window.history.replaceState(null, '', recordLocation(id, summaryTab.value))
  } catch (failure) {
    if (revision === recordRevision) { activeRecordId.value = ''; parseResult.value = null; summaryTask.value = null; mindMapTask.value = null; task.value = null; errorMessage.value = failure instanceof Error ? failure.message : '记录加载失败。'; window.history.replaceState(null, '', '#workspace') }
  } finally {
    if (revision === recordRevision) { restoringRecord = false; scheduleVideoView() }
  }
}

async function selectSummary(event: Event) {
  if (!(event.target instanceof HTMLSelectElement)) return
  const id = event.target.value
  const revision = recordRevision
  const selection = ++summaryRevision
  mapRevision++
  loadingSummary.value = false; loadingMindMap.value = false
  stopSummaryPolling(); stopMindMapPolling()
  try {
    const selected = await api(`/api/summaries/${id}`)
    if (revision !== recordRevision || selection !== summaryRevision) return
    summaryTask.value = selected
    mindMapTask.value = null; previousMindMap.value = null; currentMapView.value = null
    if (['pending', 'running'].includes(selected.status)) summaryPollingTimer.value = window.setInterval(() => pollSummary(id), 1400)
    await restoreMindMapVersions(id)
    if (revision === recordRevision && selection === summaryRevision) scheduleVideoView()
  } catch (failure) {
    if (revision === recordRevision && selection === summaryRevision) errorMessage.value = failure instanceof Error ? failure.message : '总结版本加载失败。'
  }
}

async function selectMindMap(event: Event) {
  if (!(event.target instanceof HTMLSelectElement) || !summaryTask.value) return
  const revision = recordRevision
  const selection = ++mapRevision
  loadingMindMap.value = false
  stopMindMapPolling(); previousMindMap.value = null; currentMapView.value = null
  try {
    await restoreMindMapVersions(summaryTask.value.task_id, event.target.value, false)
    if (revision === recordRevision && selection === mapRevision) scheduleVideoView()
  } catch (failure) {
    if (revision === recordRevision && selection === mapRevision) errorMessage.value = failure instanceof Error ? failure.message : '导图版本加载失败。'
  }
}

async function restoreMindMapVersions(summaryId: string, preferredId?: string, trackPending = true) {
  const revision = recordRevision
  const selection = mapRevision
  const versions = mindMapVersions.value
  const preferred = versions.find(item => item.task_id === preferredId)
  const finished = versions.find(item => item.task_id === preferredId && item.status === 'finished') || versions.find(item => item.status === 'finished')
  const pending = trackPending ? versions.find(item => ['pending', 'running'].includes(item.status)) : undefined
  const selected = pending || preferred || finished || versions[0]
  if (!selected) return
  if (finished && finished.task_id !== selected.task_id) {
    const preview = filterMindMapTask(await api(`/api/summaries/${summaryId}/mind-map?map_id=${finished.task_id}`))
    if (revision !== recordRevision || selection !== mapRevision || summaryTask.value?.task_id !== summaryId) return
    previousMindMap.value = preview
    if (preferredId !== finished.task_id) currentMapView.value = null
  }
  await pollMindMap(summaryId, selected.task_id)
  if (revision !== recordRevision || selection !== mapRevision || summaryTask.value?.task_id !== summaryId) return
  if (['pending', 'running'].includes(mindMapTask.value?.status || '')) mindMapPollingTimer.value = window.setInterval(() => pollMindMap(summaryId, selected.task_id), 1400)
}

async function prepareFormats(force = false) {
  if (!activeRecordId.value || (formatsFresh && !force)) return true
  const revision = recordRevision
  const id = activeRecordId.value
  const selected = parseResult.value?.formats.find(item => item.format_id === selectedFormat.value)
  const fresh: ParseResult = await api(`/api/videos/records/${id}/formats?force=${force}`, { method: 'POST' })
  if (revision !== recordRevision || id !== activeRecordId.value) return false
  parseResult.value = fresh
  selectedFormat.value = fresh.formats.find(item => item.label === selected?.label && item.ext === selected?.ext)?.format_id || fresh.formats[0]?.format_id || ''
  formatsFresh = true
  return true
}

function reparseHistory(target: string) { videoWorkspace.value = 'current'; url.value = target; void parseVideo(true) }
function removedVideoRecord(id: string) {
  if (activeRecordId.value === id) { recordRevision++; activeRecordId.value = ''; parseResult.value = null; summaryTask.value = null; mindMapTask.value = null; task.value = null; resetPreview(); stopPolling(); stopSummaryPolling(); stopMindMapPolling(); window.history.replaceState(null, '', '#workspace') }
  if (!activeRecordId.value) { loadingParse.value = false; loadingSummary.value = false; loadingMindMap.value = false; loadingDownload.value = false; restoringRecord = false }
  if (lastRecordId.value === id) lastRecordId.value = ''
}
function restoreFromLocation() { const route = readRecordLocation(window.location.hash); if (route && route.recordId !== activeRecordId.value) void openVideoRecord(route.recordId, route.tab) }
function flushVideoView() { void saveVideoView(true) }
watch(summaryTab, () => { if (activeRecordId.value && !restoringRecord) window.history.replaceState(null, '', recordLocation(activeRecordId.value, summaryTab.value)); scheduleVideoView() })
watch(selectedFormat, scheduleVideoView)

function selectRecentLink(target: string) {
  url.value = target
  showRecentLinks.value = false
}

function hideRecentLinksSoon() {
  window.setTimeout(() => {
    showRecentLinks.value = false
  }, 120)
}

async function startSummary() {
  const target = parseResult.value?.webpage_url || extractHttpUrl(url.value)
  if (!target || loadingSummary.value) return
  const revision = recordRevision
  const selection = ++summaryRevision
  mapRevision++
  const recordId = activeRecordId.value
  errorMessage.value = ''
  saveMessage.value = ''
  loadingSummary.value = true
  summaryTask.value = null
  mindMapTask.value = null
  previousMindMap.value = null; currentMapView.value = null
  summaryTab.value = 'summary'
  stopSummaryPolling()
  stopMindMapPolling()

  try {
    const response = await fetch('/api/summaries', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: target, parse_record_id: recordId || undefined }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    const data: { task_id: string } = await response.json()
    if (revision !== recordRevision || selection !== summaryRevision) return
    await pollSummary(data.task_id)
    if (revision !== recordRevision || selection !== summaryRevision) return
    if (['pending', 'running'].includes(summaryTone.value)) summaryPollingTimer.value = window.setInterval(() => pollSummary(data.task_id), 1400)
    await refreshTaskReferences()
    if (revision === recordRevision && selection === summaryRevision) scheduleVideoView()
  } catch (error) {
    if (revision === recordRevision && selection === summaryRevision) errorMessage.value = error instanceof Error ? error.message : '总结任务创建失败。'
  } finally {
    if (revision === recordRevision && selection === summaryRevision) loadingSummary.value = false
  }
}

async function pollSummary(taskId: string) {
  const revision = recordRevision
  const selection = summaryRevision
  try {
    const response = await fetch(`/api/summaries/${taskId}`)
    if (!response.ok) throw new Error(await readApiError(response))
    const data = await response.json()
    if (revision !== recordRevision || selection !== summaryRevision || (summaryTask.value && summaryTask.value.task_id !== taskId)) return
    summaryTask.value = data
    if (['finished', 'failed', 'cancelled'].includes(summaryTask.value?.status || '')) {
      stopSummaryPolling()
      await refreshTaskReferences()
    }
  } catch (error) {
    if (revision !== recordRevision || selection !== summaryRevision || (summaryTask.value && summaryTask.value.task_id !== taskId)) return
    stopSummaryPolling()
    errorMessage.value = error instanceof Error ? error.message : '总结任务状态查询失败。'
  }
}

async function startMindMap(regenerate = false) {
  const summaryId = summaryTask.value?.task_id
  if (!summaryId || summaryTask.value?.status !== 'finished' || loadingMindMap.value) return
  const revision = recordRevision
  const selection = ++mapRevision
  loadingMindMap.value = true
  errorMessage.value = ''
  stopMindMapPolling()
  previousMindMap.value = renderedMindMap.value
  try {
    const suffix = regenerate ? '?regenerate=true' : ''
    const response = await fetch(`/api/summaries/${summaryId}/mind-map${suffix}`, { method: 'POST' })
    if (!response.ok) throw new Error(await readApiError(response))
    const created = await response.json()
    if (revision !== recordRevision || selection !== mapRevision || summaryTask.value?.task_id !== summaryId) return
    await pollMindMap(summaryId, created.task_id)
    if (revision !== recordRevision || selection !== mapRevision || summaryTask.value?.task_id !== summaryId) return
    if (['pending', 'running'].includes(mindMapTask.value?.status || '')) {
      mindMapPollingTimer.value = window.setInterval(() => pollMindMap(summaryId, created.task_id), 1400)
    }
    await refreshTaskReferences()
    if (revision === recordRevision && selection === mapRevision) scheduleVideoView()
  } catch (error) {
    if (revision === recordRevision && selection === mapRevision) errorMessage.value = error instanceof Error ? error.message : '思维导图任务创建失败。'
  } finally {
    if (revision === recordRevision && selection === mapRevision) loadingMindMap.value = false
  }
}

async function pollMindMap(summaryId: string, mapId?: string) {
  const revision = recordRevision
  const selection = mapRevision
  try {
    const response = await fetch(`/api/summaries/${summaryId}/mind-map${mapId ? `?map_id=${mapId}` : ''}`)
    if (!response.ok) throw new Error(await readApiError(response))
    const filtered = filterMindMapTask(await response.json())
    if (!filtered) throw new Error('思维导图接口返回了无效数据。')
    if (revision !== recordRevision || selection !== mapRevision || summaryTask.value?.task_id !== summaryId) return
    if (filtered.result && renderedMindMap.value && renderedMindMap.value.task_id !== filtered.task_id) currentMapView.value = null
    mindMapTask.value = filtered
    if (filtered.result) previousMindMap.value = null
    if (['finished', 'failed', 'cancelled'].includes(filtered.status)) {
      stopMindMapPolling()
      await refreshTaskReferences()
      scheduleVideoView()
    }
  } catch (error) {
    if (revision !== recordRevision || selection !== mapRevision || summaryTask.value?.task_id !== summaryId) return
    stopMindMapPolling()
    errorMessage.value = error instanceof Error ? error.message : '思维导图状态查询失败。'
  }
}

async function chooseSaveHandle() {
  if (!window.showSaveFilePicker || !parseResult.value) return null
  const ext = suggestedExtension()
  return window.showSaveFilePicker({
    suggestedName: safeFilename(parseResult.value.title, ext),
    types: [
      {
        description: ext === 'm4a' ? 'Audio file' : 'Video file',
        accept:
          ext === 'm4a'
            ? { 'audio/mp4': ['.m4a'], 'audio/mpeg': ['.mp3'] }
            : { 'video/mp4': ['.mp4'], 'video/webm': ['.webm'], 'video/x-matroska': ['.mkv'] },
      },
    ],
  })
}

async function readApiError(response: Response) {
  try {
    const body = await response.json()
    if (Array.isArray(body.detail)) return body.detail[0]?.msg || '请求失败，请稍后再试。'
    return body.detail?.message || body.detail || '请求失败，请稍后再试。'
  } catch {
    return '请求失败，请稍后再试。'
  }
}

function stopBrowserPolling() {
  browserRevision++
  if (browserTimer !== undefined) window.clearTimeout(browserTimer)
  browserTimer = undefined
}

async function refreshBrowserSession() {
  stopBrowserPolling()
  const revision = browserRevision
  try {
    const response = await fetch('/api/browser-session')
    if (!response.ok) throw new Error(await readApiError(response))
    const data: BrowserSession = await response.json()
    if (browserDisposed || browserRevision !== revision) return
    browserSession.value = data
    if (browserSession.value?.visible) {
      verificationTarget.value ||= browserSession.value.url || ''
      browserTimer = window.setTimeout(() => void refreshBrowserSession(), 3000)
    }
  } catch (error) {
    if (!browserDisposed && browserRevision === revision && verificationTarget.value) errorMessage.value = error instanceof Error ? error.message : '读取验证窗口状态失败。'
  }
}

async function openVerificationBrowser() {
  if (!verificationTarget.value || browserBusy.value || loadingParse.value) return
  browserBusy.value = true
  errorMessage.value = ''
  stopBrowserPolling()
  try {
    const response = await fetch('/api/browser-session/open', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: verificationTarget.value }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    browserSession.value = await response.json()
    if (!browserDisposed) browserTimer = window.setTimeout(() => void refreshBrowserSession(), 3000)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '打开验证窗口失败。'
  } finally {
    browserBusy.value = false
  }
}

async function closeVerificationBrowser(reset = false) {
  if (browserBusy.value || loadingParse.value) return
  browserBusy.value = true
  stopBrowserPolling()
  try {
    const response = await fetch(`/api/browser-session?reset=${reset}`, { method: 'DELETE' })
    if (!response.ok) throw new Error(await readApiError(response))
    browserSession.value = await response.json()
    if (reset) verificationTarget.value = ''
    errorMessage.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '关闭验证窗口失败。'
  } finally {
    browserBusy.value = false
  }
}

async function retryVerifiedVideo() {
  url.value = verificationTarget.value
  await parseVideo()
}

function resetPreview() {
  previewRevision++
  if (previewState.value === 'ready' && previewVideoRef.value) playbackTime.value = previewVideoRef.value.currentTime
  previewState.value = 'idle'
  previewUrl.value = ''
  previewError.value = ''
  previewVideoRef.value = null
  pendingSeekTime.value = null
}

function handlePreviewError() {
  if (previewUrl.value === localVideoUrl.value) localPreviewFailed.value = true
  formatsFresh = false
  previewUrl.value = ''
  previewState.value = 'error'
  previewError.value = '视频连接失败，请重新解析或稍后重试。'
}

function handlePreviewReady() {
  previewState.value = 'ready'
  if (pendingSeekTime.value !== null && previewVideoRef.value) {
    previewVideoRef.value.currentTime = clampSeekTime(pendingSeekTime.value, previewVideoRef.value.duration)
    pendingSeekTime.value = null
    void previewVideoRef.value.play().catch(() => undefined)
  } else if (previewVideoRef.value && playbackTime.value) {
    previewVideoRef.value.currentTime = clampSeekTime(playbackTime.value, previewVideoRef.value.duration)
  }
}

async function seekToTimestamp(timestamp: number) {
  if (previewVideoRef.value && canSeekMedia(previewVideoRef.value)) {
    previewVideoRef.value.currentTime = clampSeekTime(timestamp, previewVideoRef.value.duration)
    await previewVideoRef.value.play().catch(() => undefined)
    return
  }
  if (previewVideoRef.value) {
    pendingSeekTime.value = timestamp
    return
  }
  if (canPreview.value) {
    pendingSeekTime.value = timestamp
    await startPreview()
    return
  }
  errorMessage.value = '当前视频没有可跳转的在线预览。'
}

async function startPreview() {
  if (!parseResult.value || previewState.value === 'loading') return
  const revision = recordRevision
  const request = ++previewRevision
  const recordId = activeRecordId.value

  previewState.value = 'loading'
  previewUrl.value = ''
  previewError.value = ''

  try {
    if (localVideoUrl.value && !localPreviewFailed.value) { previewUrl.value = localVideoUrl.value; return }
    if (!await prepareFormats() || revision !== recordRevision || request !== previewRevision) return
    const format = previewFormat.value
    const target = parseResult.value?.webpage_url || extractHttpUrl(url.value)
    if (!format || !target) throw new Error('当前视频没有可用的播放源。')
    const response = await fetch('/api/previews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: target, format_id: format.format_id, parse_record_id: recordId || undefined }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    const localPreviewUrl = readPreviewUrl(await response.json())
    if (revision !== recordRevision || request !== previewRevision) return
    if (!localPreviewUrl) throw new Error('预览接口返回了无效地址。')
    previewUrl.value = localPreviewUrl
  } catch (error) {
    if (revision !== recordRevision || request !== previewRevision) return
    previewState.value = 'error'
    previewError.value = error instanceof Error ? error.message : '视频连接失败，请稍后重试。'
  }
}

async function parseVideo(force = false) {
  if (loadingParse.value || browserBusy.value) return
  const target = extractHttpUrl(url.value)
  if (!target) {
    errorMessage.value = '请先粘贴视频链接或完整的分享文字。'
    return
  }
  loadingParse.value = true
  const revision = ++recordRevision
  await saveVideoView()
  if (revision !== recordRevision) return
  restoringRecord = false
  loadingSummary.value = false; loadingMindMap.value = false; loadingDownload.value = false
  activeRecordId.value = ''; relatedTasks.value = []; restoredView.value = null; currentMapView.value = null
  previousMindMap.value = null; localVideoUrl.value = ''; localPreviewFailed.value = false; recordMessage.value = ''; viewSaveStatus.value = ''
  errorMessage.value = ''
  saveMessage.value = ''
  resetPreview()
  playbackTime.value = 0
  stopPolling()
  parseResult.value = null
  task.value = null
  summaryTask.value = null
  mindMapTask.value = null
  summaryTab.value = 'summary'
  selectedFormat.value = ''
  coverFailed.value = false
  saveHandle.value = null
  stopSummaryPolling()
  stopMindMapPolling()
  loadingParse.value = true
  stopBrowserPolling()
  verificationTarget.value = ''

  try {
    const response = await fetch('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: target, force }),
    })
    if (!response.ok) {
      const detail = (await response.clone().json().catch(() => ({}))).detail
      if (revision !== recordRevision) return
      if (detail?.record_id) { activeRecordId.value = detail.record_id; lastRecordId.value = detail.record_id; window.history.replaceState(null, '', recordLocation(detail.record_id)) }
      if (detail?.code === 'browser_verification_required') {
        verificationTarget.value = detail.url || target
        void refreshBrowserSession()
      }
      throw new Error(await readApiError(response))
    }
    const parsed = await response.json()
    if (revision !== recordRevision) return
    parseResult.value = parsed
    activeRecordId.value = parseResult.value?.record_id || ''
    lastRecordId.value = activeRecordId.value
    formatsFresh = true
    selectedFormat.value = parseResult.value?.formats.find((item) => item.format_id.startsWith('sniffpair:'))?.format_id || parseResult.value?.formats[0]?.format_id || ''
    if (activeRecordId.value) window.history.replaceState(null, '', recordLocation(activeRecordId.value))
    scheduleVideoView()
  } catch (error) {
    if (revision === recordRevision) errorMessage.value = error instanceof Error ? error.message : '解析失败，请确认链接是否公开可访问。'
  } finally {
    if (revision === recordRevision) loadingParse.value = false
    void loadRecentLinks()
    void historyPanel.value?.refresh()
    if (browserSession.value?.visible && browserTimer === undefined) void refreshBrowserSession()
  }
}

async function startDownload() {
  const target = parseResult.value?.webpage_url || extractHttpUrl(url.value)
  if (!target || loadingDownload.value) return
  const revision = recordRevision
  const recordId = activeRecordId.value
  errorMessage.value = ''
  saveMessage.value = ''
  loadingDownload.value = true
  task.value = null

  try {
    if (!await prepareFormats() || revision !== recordRevision) return
    if (window.showSaveFilePicker) {
      try {
        const handle = await chooseSaveHandle()
        if (revision !== recordRevision) return
        saveHandle.value = handle
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        throw error
      }
    }

    const response = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: target,
        format_id: selectedFormat.value || undefined,
        parse_record_id: recordId || undefined,
      }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    const data: { task_id: string } = await response.json()
    if (revision !== recordRevision) return
    await pollTask(data.task_id)
    if (revision !== recordRevision) return
    if (['pending', 'running'].includes(taskTone.value)) pollingTimer.value = window.setInterval(() => pollTask(data.task_id), 1200)
    await refreshTaskReferences()
  } catch (error) {
    if (revision === recordRevision) errorMessage.value = error instanceof Error ? error.message : '下载任务创建失败。'
  } finally {
    if (revision === recordRevision) loadingDownload.value = false
  }
}

async function pollTask(taskId: string) {
  const revision = recordRevision
  try {
    const response = await fetch(`/api/tasks/${taskId}`)
    if (!response.ok) throw new Error(await readApiError(response))
    const data = await response.json()
    if (revision !== recordRevision || (task.value && task.value.task_id !== taskId)) return
    task.value = data
    if (['finished', 'failed', 'cancelled'].includes(task.value?.status || '')) {
      stopPolling()
      await refreshTaskReferences()
      if (revision !== recordRevision || task.value?.task_id !== taskId) return
      if (task.value?.status === 'finished' && task.value.download_url && /\.(mp4|webm)$/i.test(task.value.filename || '')) localVideoUrl.value = task.value.download_url
      if (task.value?.status === 'finished' && task.value.download_url && saveHandle.value) {
        await saveFinishedFile(saveHandle.value)
      }
    }
  } catch (error) {
    if (revision !== recordRevision || (task.value && task.value.task_id !== taskId)) return
    stopPolling()
    errorMessage.value = error instanceof Error ? error.message : '任务状态查询失败。'
  }
}

async function saveFinishedFile(handle?: FilePickerHandle | null) {
  if (!task.value?.download_url) return
  savingFile.value = true
  errorMessage.value = ''
  saveMessage.value = ''

  try {
    const response = await fetch(task.value.download_url)
    if (!response.ok) throw new Error(await readApiError(response))
    const blob = await response.blob()

    if (handle || window.showSaveFilePicker) {
      const targetHandle = handle || (await chooseSaveHandle())
      if (!targetHandle) return
      const writable = await targetHandle.createWritable()
      await writable.write(blob)
      await writable.close()
      saveMessage.value = '已保存到你选择的位置。'
      saveHandle.value = null
      return
    }

    const objectUrl = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = task.value.filename || safeFilename(parseResult.value?.title || 'VideoDream', suggestedExtension())
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(objectUrl)
    saveMessage.value = '浏览器已开始下载文件。'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '保存文件失败。'
  } finally {
    savingFile.value = false
  }
}

function stopPolling() {
  if (pollingTimer.value) {
    window.clearInterval(pollingTimer.value)
    pollingTimer.value = undefined
  }
}

onBeforeUnmount(() => {
  flushVideoView()
  window.removeEventListener('hashchange', restoreFromLocation)
  window.removeEventListener('pagehide', flushVideoView)
  browserDisposed = true
  stopBrowserPolling()
  stopPolling()
  stopSummaryPolling()
  stopMindMapPolling()
})

onMounted(() => {
  void loadRecentLinks()
  void refreshBrowserSession()
  window.addEventListener('hashchange', restoreFromLocation)
  window.addEventListener('pagehide', flushVideoView)
  const route = readRecordLocation(window.location.hash)
  if (route) void openVideoRecord(route.recordId, route.tab)
  else void api('/api/videos/last-record').then(data => { lastRecordId.value = data.record_id || '' }).catch(() => {})
})
</script>

<template>
  <div class="page-shell" :class="{ 'music-page': appPage === 'music' }">
    <div v-show="appPage === 'music'"><MusicCenter ref="musicCenter" :active="appPage === 'music'" @open-tools="openMusicTools" @open-admin="pausePlayback(); appPage = 'admin'" /></div>
    <AdminWorkspace v-if="appPage === 'admin'" @back="appPage = 'music'" />
    <div v-show="appPage === 'tools'" class="tools-page" @play.capture="onToolsPlay">
    <header class="topbar">
      <a class="brand" href="#" @click.prevent="returnMusicCenter">
        <span class="brand-mark">VD</span>
        <span>VideoDream</span>
      </a>
      <nav class="nav-links" aria-label="主导航">
        <a href="#" @click.prevent="returnMusicCenter">音乐中心</a>
        <a href="#workspace">下载工作台</a>
        <a href="#watermark">视频去水印</a>
        <a href="#platforms">支持平台</a>
        <a href="#notice">授权提醒</a>
      </nav>
      <a class="login-pill" href="#workspace"><Download :size="16" />下载工作台</a>
    </header>

    <main>
      <section id="workspace" class="hero">
        <div class="hero-copy">
          <span class="eyebrow">视频与音乐</span>
          <h1>VideoDream</h1>
        </div>
        <section class="download-panel" aria-label="下载工作台">
          <div class="download-modes" role="tablist" aria-label="下载类型">
            <button id="video-tab" role="tab" :aria-selected="mode === 'video'" :tabindex="mode === 'video' ? 0 : -1" aria-controls="video-panel" :class="{ active: mode === 'video' }" @click="changeMode('video')" @keydown.right.prevent="changeMode('music', true)" @keydown.left.prevent="changeMode('music', true)"><Video :size="21" /><span>视频下载</span></button>
            <button type="button" @click="changeMode('music')"><Music2 :size="21" /><span>音乐中心</span></button>
          </div>
          <div v-show="mode === 'video'" id="video-panel" role="tabpanel" aria-labelledby="video-tab">
          <nav class="video-workspace-tabs" aria-label="视频工作台视图"><button :class="{ active: videoWorkspace === 'current' }" @click="videoWorkspace = 'current'"><Video :size="17" />当前视频</button><button :class="{ active: videoWorkspace === 'history' }" @click="saveVideoView(); videoWorkspace = 'history'"><History :size="17" />解析历史</button></nav>
          <VideoHistory v-if="videoWorkspace === 'history'" ref="historyPanel" @open="openVideoRecord" @reparse="reparseHistory" @removed="removedVideoRecord" />
          <div v-show="videoWorkspace === 'current'">
          <div class="search-area">
            <form class="link-composer" @submit.prevent="parseVideo()">
              <textarea
                v-model="url"
                aria-label="视频链接或分享文字"
                placeholder="粘贴视频链接或分享文字…"
                autocomplete="off"
                @focus="showRecentLinks = true"
                @blur="hideRecentLinksSoon"
                @keydown.ctrl.enter.prevent="parseVideo()"
              />
              <div class="composer-footer"><span class="composer-source">抖音 · B站 · YouTube</span><button class="composer-submit" type="submit" :disabled="loadingParse || !url.trim()"><LoaderCircle v-if="loadingParse" :size="18" class="spin" /><ArrowRight v-else :size="18" />{{ loadingParse ? '解析中' : '解析视频' }}</button></div>
            </form>
            <div v-if="showRecentLinks && hasRecentLinks" class="recent-links" aria-label="最近输入链接">
              <div class="video-recent-head"><span>最近提交</span><button class="video-icon" title="清空最近链接" aria-label="清空最近链接" @mousedown.prevent="removeRecentLink()"><Trash2 :size="15" /></button></div>
              <div v-for="item in recentLinks" :key="item.id" class="video-recent-row"><button type="button" @mousedown.prevent="selectRecentLink(item.url)"><span>最近</span><strong>{{ item.url }}</strong></button><button class="video-icon" title="删除最近链接" aria-label="删除最近链接" @mousedown.prevent="removeRecentLink(item.id)"><X :size="15" /></button></div>
            </div>
          </div>

          <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
          <p v-if="recordMessage" class="video-record-message">{{ recordMessage }}</p>
          <p v-if="activeRecordId && viewSaveStatus" class="video-save-status" :class="{ failed: !['已保存', '保存中'].includes(viewSaveStatus) }" role="status">{{ viewSaveStatus }}</p>
          <div v-if="verificationTarget || browserSession?.visible" class="browser-verification" role="status" aria-live="polite">
            <div class="verification-status"><ShieldCheck :size="18" /><span>{{ verificationMessage }}</span></div>
            <div class="verification-actions">
              <button v-if="verificationTarget" type="button" class="secondary-action compact" :disabled="browserBusy || loadingParse" @click="openVerificationBrowser"><ExternalLink :size="16" />{{ browserSession?.visible ? '显示验证窗口' : '打开验证窗口' }}</button>
              <button v-if="verificationTarget" type="button" class="composer-submit" :disabled="browserBusy || loadingParse" @click="retryVerifiedVideo"><LoaderCircle v-if="loadingParse" :size="16" class="spin" /><RotateCcw v-else :size="16" />验证完成，重新解析</button>
              <button v-if="browserSession?.visible" type="button" class="verification-icon" aria-label="关闭验证窗口" title="关闭验证窗口，保留会话" :disabled="browserBusy || loadingParse" @click="closeVerificationBrowser()"><X :size="18" /></button>
              <button type="button" class="verification-icon" aria-label="清除抖音会话" title="清除抖音会话" :disabled="browserBusy || loadingParse" @click="closeVerificationBrowser(true)"><Eraser :size="18" /></button>
            </div>
          </div>
          <p v-if="saveMessage" class="success-text">{{ saveMessage }}</p>

          <div v-if="parseResult" class="result-card">
            <div class="cover-wrap">
              <video
                v-if="previewUrl"
                ref="previewVideoRef"
                :src="previewUrl"
                :poster="parseResult.thumbnail || undefined"
                controls
                playsinline
                preload="metadata"
                @timeupdate="savePlaybackProgress"
                @pause="scheduleVideoView"
                @loadedmetadata="handlePreviewReady"
                @error="handlePreviewError"
              />
              <template v-else>
                <img
                  v-if="parseResult.thumbnail && !coverFailed"
                  :src="parseResult.thumbnail"
                  :alt="parseResult.title"
                  @error="coverFailed = true"
                />
                <div v-else class="cover-empty">VD</div>
                <button
                  v-if="canPreview"
                  type="button"
                  class="preview-play-button"
                  :aria-label="previewState === 'error' ? '重新连接视频' : '播放视频'"
                  :title="previewState === 'error' ? '重新连接视频' : '播放视频'"
                  @click="startPreview"
                >
                  <RotateCcw v-if="previewState === 'error'" :size="21" /><Play v-else :size="21" />
                </button>
              </template>
              <div v-if="previewState === 'loading'" class="preview-loading" role="status">
                <span class="preview-spinner" aria-hidden="true"></span>
                <span>正在连接视频</span>
              </div>
              <p v-if="previewState === 'error' && previewError" class="preview-error">
                {{ previewError }}
              </p>
            </div>
            <div class="video-meta">
              <span class="status-chip">解析成功</span>
              <h2>{{ parseResult.title }}</h2>
              <div class="meta-row">
                <span v-if="parseResult.uploader">{{ parseResult.uploader }}</span>
                <span v-if="parseResult.duration">{{ formatDuration(parseResult.duration) }}</span>
                <a
                  v-if="parseResult.webpage_url"
                  :href="parseResult.webpage_url"
                  target="_blank"
                  rel="noreferrer"
                >
                  {{ sourceHost || '打开原网页' }}
                </a>
              </div>
              <label class="format-select">
                <span>下载格式</span>
                <select v-model="selectedFormat" @change="resetPreview">
                  <option value="">自动选择最佳格式</option>
                  <option v-for="item in parseResult.formats" :key="item.format_id" :value="item.format_id">
                    {{ item.label }}
                  </option>
                </select>
              </label>
              <button class="primary-action" :disabled="!canDownload" @click="startDownload">
                {{ loadingDownload ? '准备文件中' : '选择位置并下载' }}
              </button>
              <button class="secondary-action full-width" :disabled="!canSummarize" @click="startSummary">
                {{ loadingSummary ? '启动总结中' : '生成 AI 总结' }}
              </button>
            </div>
          </div>

          <div v-if="summaryTask" class="summary-card" :class="`tone-${summaryTone}`">
            <div class="task-head">
              <span>{{ summaryTask.status === 'finished' ? '总结完成' : summaryTask.status === 'failed' ? '总结失败' : '正在总结' }}</span>
              <strong>{{ Math.round(summaryTask.progress) }}%</strong>
            </div>
            <div class="progress-track">
              <span :style="{ width: `${Math.max(4, summaryTask.progress)}%` }"></span>
            </div>
            <p class="summary-message">{{ summaryTask.error || summaryTask.message }}</p>
            <div v-if="summaryVersions.length > 1" class="video-version-controls"><label>总结版本<select :value="summaryTask.task_id" aria-label="总结版本" @change="selectSummary"><option v-for="item in summaryVersions" :key="item.task_id" :value="item.task_id">{{ new Date(item.created_at * 1000).toLocaleString() }} · {{ item.status === 'finished' ? '完成' : item.status === 'failed' ? '失败' : item.status === 'cancelled' ? '已取消' : '处理中' }}</option></select></label></div>

            <div v-if="summaryTask.result" class="summary-result">
              <div class="summary-meta">
                <span>{{ summaryTask.result.source }}</span>
                <span>{{ summaryTask.result.language }}</span>
                <span>{{ summaryTask.result.transcript.length }} 段文本</span>
              </div>
              <div class="summary-actions">
                <a v-if="summaryTask.result.markdown_url" :href="summaryTask.result.markdown_url" download>下载 Markdown</a>
                <a v-if="summaryTask.result.json_url" :href="summaryTask.result.json_url" download>下载 JSON</a>
              </div>
              <div class="summary-tabs" role="tablist" aria-label="总结视图">
                <button type="button" role="tab" :aria-selected="summaryTab === 'summary'" @click="summaryTab = 'summary'">总结</button>
                <button type="button" role="tab" :aria-selected="summaryTab === 'transcript'" @click="summaryTab = 'transcript'">字幕与转写</button>
                <button type="button" role="tab" :aria-selected="summaryTab === 'mind-map'" @click="summaryTab = 'mind-map'">思维导图</button>
              </div>
              <section v-if="summaryTab === 'summary'" class="summary-text" aria-label="AI 总结">
                <pre>{{ plainMarkdown(summaryTask.result.summary_markdown) }}</pre>
              </section>
              <section v-else-if="summaryTab === 'transcript'" class="transcript-list" aria-label="字幕文本">
                <div v-for="item in summaryTask.result.transcript.slice(0, 80)" :key="`${item.timestamp}-${item.text}`">
                  <time>{{ item.timestamp }}</time>
                  <span>{{ item.text }}</span>
                </div>
              </section>
              <section v-else class="mind-map-panel" aria-label="思维导图">
                <div v-if="mindMapVersions.length" class="video-version-controls"><label>导图版本<select :value="mindMapTask?.task_id || renderedMindMap?.task_id" aria-label="导图版本" @change="selectMindMap"><option v-for="item in mindMapVersions" :key="item.task_id" :value="item.task_id">v{{ item.version }} · {{ item.status === 'finished' ? '完成' : item.status === 'failed' ? '失败' : item.status === 'cancelled' ? '已取消' : '生成中' }}</option></select></label><button class="video-icon" title="重新生成思维导图" aria-label="重新生成思维导图" :disabled="loadingMindMap || ['pending', 'running'].includes(mindMapTask?.status || '')" @click="startMindMap(true)"><RotateCcw :size="17" /></button></div>
                <div v-if="!mindMapTask" class="mind-map-empty">
                  <button type="button" class="secondary-action" :disabled="loadingMindMap" @click="startMindMap(false)">
                    {{ loadingMindMap ? '正在启动' : '生成思维导图' }}
                  </button>
                </div>
                <template v-else>
                  <div class="task-head mind-map-task-head">
                    <span>{{ mindMapTask.status === 'finished' ? '思维导图完成' : mindMapTask.status === 'failed' ? '生成失败' : '正在生成思维导图' }}</span>
                    <strong>{{ Math.round(mindMapTask.progress) }}%</strong>
                  </div>
                  <div class="progress-track" :class="`tone-${mindMapTone}`">
                    <span :style="{ width: `${Math.max(4, mindMapTask.progress)}%` }"></span>
                  </div>
                  <p v-if="mindMapTask.error || mindMapTask.status !== 'finished'" class="summary-message">
                    {{ mindMapTask.error || mindMapTask.message }}
                  </p>
                  <div v-if="mindMapTask.status === 'failed'" class="mind-map-retry">
                    <button type="button" class="secondary-action" :disabled="loadingMindMap" @click="startMindMap(true)">重新生成</button>
                  </div>
                  <div v-if="renderedMindMap?.result" class="mind-map-tree-wrap">
                    <MindMapCanvas
                      :key="renderedMindMap.task_id"
                      :result="renderedMindMap.result"
                      :saved-view="currentMapView"
                      :seek-enabled="Boolean(previewVideoRef || canPreview)"
                      @seek="seekToTimestamp"
                      @view-change="changeMapView"
                    />
                  </div>
                </template>
              </section>
            </div>
          </div>

          <div v-if="task" class="task-card" :class="`tone-${taskTone}`">
            <div class="task-head">
              <span>{{ task.status === 'finished' ? '下载完成' : task.status === 'failed' ? '处理失败' : '任务进行中' }}</span>
              <strong>{{ Math.round(task.progress) }}%</strong>
            </div>
            <div class="progress-track">
              <span :style="{ width: `${Math.max(4, task.progress)}%` }"></span>
            </div>
            <p>{{ task.error || task.message }}</p>
            <button v-if="task.download_url" class="download-link" :disabled="savingFile" @click="saveFinishedFile()">
              {{ savingFile ? '保存中' : '另存为' }}
            </button>
            <a v-if="task.status === 'finished' && task.filename" class="secondary-action compact watermark-entry" href="#watermark"><Eraser :size="16" aria-hidden="true" />去水印</a>
          </div>
          </div>
          </div>
        </section>
      </section>

      <WatermarkEditor :preferred-filename="task?.status === 'finished' ? task.filename : null" />

      <section id="platforms" class="platform-band">
        <div class="section-title">
          <span>通用支持</span>
          <h2>来源平台</h2>
        </div>
        <div class="tag-grid">
          <a v-for="item in supported" :key="item.href" :href="item.href" target="_blank" rel="noreferrer">
            {{ item.label }}
          </a>
        </div>
      </section>

      <section id="notice" class="notice">
        <strong>授权提醒</strong>
        <span>请仅下载你拥有权利、获得授权或平台允许保存的内容。</span>
      </section>
    </main>
    </div>
    <div v-show="appPage !== 'admin'"><MusicPlayer @download="downloadFromPlayer" /></div>
  </div>
</template>
