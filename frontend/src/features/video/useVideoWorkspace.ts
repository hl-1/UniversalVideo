import { useVideoSummary } from './useVideoSummary'
import { useVideoDownload } from './useVideoDownload'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { canSeekMedia, clampSeekTime, filterMindMapTask, type MindMapTask } from './mindMap'
import { getRecentLinks } from './recentLinks'
import { readRecordLocation, recordLocation, type MapView, type VideoTaskReference, type VideoView } from './videoHistory'
import './video-history.css'
import { readPreviewUrl, selectPreviewFormat } from './videoPreview'
import { account } from '../accounts/auth'
import { api } from '../../shared/api/client'
import { extractHttpUrl } from '../../shared/utils/downloadInput'
import type { ParseResult, TaskResult, SummaryTaskResult, PreviewState, SummaryTab, FilePickerHandle } from './types'
import { readApiError } from '../../shared/api/client'
import { useBrowserSession } from './useBrowserSession'

export function useVideoWorkspace(returnMusicCenter: () => void, activate: () => void) {
  const url = ref('')
  const mode = ref<'video' | 'music'>('video')
  async function open(view: 'video' | 'watermark') {
    if (view === 'video' && !activeRecordId.value && lastRecordId.value) await openVideoRecord(lastRecordId.value)
    void nextTick(() => document.getElementById(view === 'watermark' ? 'watermark' : 'workspace')?.scrollIntoView())
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
  const generateMindMap = ref(true)
  const statusFetchMs = ref<number | null>(null)
  const mapRenderMs = ref<number | null>(null)
  const savingFile = ref(false)
  const errorMessage = ref('')
  const { browserSession, verificationTarget, browserBusy, verificationMessage, refreshBrowserSession, openVerificationBrowser, closeVerificationBrowser, stopBrowserPolling } = useBrowserSession(loadingParse, errorMessage)
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
  watch(() => renderedMindMap.value?.task_id, () => { mapRenderMs.value = null })
  const summaryVersions = computed(() => relatedTasks.value.filter(item => item.kind === 'summary'))
  const mindMapVersions = computed(() => relatedTasks.value.filter(item => item.kind === 'mind_map' && item.summary_task_id === summaryTask.value?.task_id))
  const viewSaveStatus = ref('')
  const localVideoUrl = ref('')
  const localPreviewFailed = ref(false)
  const revisions = { record: 0, summary: 0, map: 0, preview: 0 }
  let restoringRecord = false
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
  const canSummarize = computed(() => Boolean(parseResult.value && url.value.trim() && !loadingSummary.value
    && !['pending', 'running'].includes(summaryTask.value?.status || '') && !['pending', 'running'].includes(mindMapTask.value?.status || '')))
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

  const { stopSummaryPolling, stopMindMapPolling, startSummary, pollSummary, startMindMap, pollMindMap } = useVideoSummary({
    url,
    parseResult,
    activeRecordId,
    revisions,
    errorMessage,
    saveMessage,
    loadingSummary,
    loadingMindMap,
    generateMindMap,
    statusFetchMs,
    summaryTask,
    mindMapTask,
    previousMindMap,
    currentMapView,
    summaryTab,
    summaryTone,
    renderedMindMap,
    summaryPollingTimer,
    mindMapPollingTimer,
    refreshTaskReferences,
    scheduleVideoView,
  })
  const { chooseSaveHandle, startDownload, pollTask, saveFinishedFile, stopPolling } = useVideoDownload({
    url,
    parseResult,
    activeRecordId,
    revisions,
    errorMessage,
    saveMessage,
    loadingDownload,
    task,
    selectedFormat,
    saveHandle,
    savingFile,
    taskTone,
    pollingTimer,
    localVideoUrl,
    prepareFormats,
    suggestedExtension,
    refreshTaskReferences,
  })

  function suggestedExtension() {
    const selected = parseResult.value?.formats.find((item) => item.format_id === selectedFormat.value)
    const ext = selected?.ext?.toLowerCase()
    if (ext === 'm4a' || ext === 'mp3' || selected?.label.includes('音频')) return 'm4a'
    return 'mp4'
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
    const revision = revisions.record
    const detail = await api(`/api/videos/records/${id}`)
    if (revision === revisions.record && id === activeRecordId.value) relatedTasks.value = detail.tasks
  }

  async function openVideoRecord(id: string, requestedTab?: SummaryTab) {
    const revision = ++revisions.record
    await saveVideoView()
    if (revision !== revisions.record) return
    restoringRecord = true
    loadingParse.value = false; loadingSummary.value = false; loadingMindMap.value = false; loadingDownload.value = false
    activate(); videoWorkspace.value = 'current'
    errorMessage.value = ''; saveMessage.value = ''; viewSaveStatus.value = ''
    stopPolling(); stopSummaryPolling(); stopMindMapPolling(); resetPreview()
    try {
      const detail = await api(`/api/videos/records/${id}`)
      if (revision !== revisions.record) return
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
        if (revision !== revisions.record) return
        summaryTask.value = restoredSummary
        if (['pending', 'running'].includes(summaryTask.value!.status)) summaryPollingTimer.value = window.setInterval(() => pollSummary(summary.task_id), 1400)
        await restoreMindMapVersions(summary.task_id, detail.view?.mind_map_id)
      }
      if (revision !== revisions.record) return
      const download = detail.tasks.find((item: VideoTaskReference) => item.kind === 'download' && item.status === 'finished' && /\.(mp4|webm)$/i.test(item.filename || '')) || detail.tasks.find((item: VideoTaskReference) => item.kind === 'download' && item.status === 'finished') || detail.tasks.find((item: VideoTaskReference) => item.kind === 'download')
      if (download) {
        const restoredTask = await api(`/api/tasks/${download.task_id}`)
        if (revision !== revisions.record) return
        task.value = restoredTask
        if (task.value?.status === 'finished' && /\.(mp4|webm)$/i.test(task.value.filename || '')) localVideoUrl.value = task.value.download_url || ''
        if (['pending', 'running'].includes(task.value?.status || '')) pollingTimer.value = window.setInterval(() => pollTask(download.task_id), 1200)
      }
      if (detail.view?.mind_map_id !== renderedMindMap.value?.task_id) currentMapView.value = null
      window.history.replaceState(null, '', recordLocation(id, summaryTab.value))
    } catch (failure) {
      if (revision === revisions.record) { activeRecordId.value = ''; parseResult.value = null; summaryTask.value = null; mindMapTask.value = null; task.value = null; errorMessage.value = failure instanceof Error ? failure.message : '记录加载失败。'; window.history.replaceState(null, '', '#workspace') }
    } finally {
      if (revision === revisions.record) { restoringRecord = false; scheduleVideoView() }
    }
  }

  async function selectSummary(event: Event) {
    if (!(event.target instanceof HTMLSelectElement)) return
    const id = event.target.value
    const revision = revisions.record
    const selection = ++revisions.summary
    revisions.map++
    loadingSummary.value = false; loadingMindMap.value = false
    stopSummaryPolling(); stopMindMapPolling()
    try {
      const selected = await api(`/api/summaries/${id}`)
      if (revision !== revisions.record || selection !== revisions.summary) return
      summaryTask.value = selected
      mindMapTask.value = null; previousMindMap.value = null; currentMapView.value = null
      if (['pending', 'running'].includes(selected.status)) summaryPollingTimer.value = window.setInterval(() => pollSummary(id), 1400)
      await restoreMindMapVersions(id)
      if (revision === revisions.record && selection === revisions.summary) scheduleVideoView()
    } catch (failure) {
      if (revision === revisions.record && selection === revisions.summary) errorMessage.value = failure instanceof Error ? failure.message : '总结版本加载失败。'
    }
  }

  async function selectMindMap(event: Event) {
    if (!(event.target instanceof HTMLSelectElement) || !summaryTask.value) return
    const revision = revisions.record
    const selection = ++revisions.map
    loadingMindMap.value = false
    stopMindMapPolling(); previousMindMap.value = null; currentMapView.value = null
    try {
      await restoreMindMapVersions(summaryTask.value.task_id, event.target.value, false)
      if (revision === revisions.record && selection === revisions.map) scheduleVideoView()
    } catch (failure) {
      if (revision === revisions.record && selection === revisions.map) errorMessage.value = failure instanceof Error ? failure.message : '导图版本加载失败。'
    }
  }

  async function restoreMindMapVersions(summaryId: string, preferredId?: string, trackPending = true) {
    const revision = revisions.record
    const selection = revisions.map
    const versions = mindMapVersions.value
    const preferred = versions.find(item => item.task_id === preferredId)
    const finished = versions.find(item => item.task_id === preferredId && item.status === 'finished') || versions.find(item => item.status === 'finished')
    const pending = trackPending ? versions.find(item => ['pending', 'running'].includes(item.status)) : undefined
    const selected = pending || preferred || finished || versions[0]
    if (!selected) return
    if (finished && finished.task_id !== selected.task_id) {
      const preview = filterMindMapTask(await api(`/api/summaries/${summaryId}/mind-map?map_id=${finished.task_id}`))
      if (revision !== revisions.record || selection !== revisions.map || summaryTask.value?.task_id !== summaryId) return
      previousMindMap.value = preview
      if (preferredId !== finished.task_id) currentMapView.value = null
    }
    await pollMindMap(summaryId, selected.task_id)
    if (revision !== revisions.record || selection !== revisions.map || summaryTask.value?.task_id !== summaryId) return
    if (['pending', 'running'].includes(mindMapTask.value?.status || '')) mindMapPollingTimer.value = window.setInterval(() => pollMindMap(summaryId, selected.task_id), 1400)
  }

  async function prepareFormats(force = false) {
    if (!activeRecordId.value || (formatsFresh && !force)) return true
    const revision = revisions.record
    const id = activeRecordId.value
    const selected = parseResult.value?.formats.find(item => item.format_id === selectedFormat.value)
    const fresh: ParseResult = await api(`/api/videos/records/${id}/formats?force=${force}`, { method: 'POST' })
    if (revision !== revisions.record || id !== activeRecordId.value) return false
    parseResult.value = fresh
    selectedFormat.value = fresh.formats.find(item => item.label === selected?.label && item.ext === selected?.ext)?.format_id || fresh.formats[0]?.format_id || ''
    formatsFresh = true
    return true
  }

  function reparseHistory(target: string) { videoWorkspace.value = 'current'; url.value = target; void parseVideo(true) }
  function removedVideoRecord(id: string) {
    if (activeRecordId.value === id) { revisions.record++; activeRecordId.value = ''; parseResult.value = null; summaryTask.value = null; mindMapTask.value = null; task.value = null; resetPreview(); stopPolling(); stopSummaryPolling(); stopMindMapPolling(); window.history.replaceState(null, '', '#workspace') }
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

  async function retryVerifiedVideo() {
    url.value = verificationTarget.value
    await parseVideo()
  }

  function resetPreview() {
    revisions.preview++
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
    const revision = revisions.record
    const request = ++revisions.preview
    const recordId = activeRecordId.value

    previewState.value = 'loading'
    previewUrl.value = ''
    previewError.value = ''

    try {
      if (localVideoUrl.value && !localPreviewFailed.value) { previewUrl.value = localVideoUrl.value; return }
      if (!await prepareFormats() || revision !== revisions.record || request !== revisions.preview) return
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
      if (revision !== revisions.record || request !== revisions.preview) return
      if (!localPreviewUrl) throw new Error('预览接口返回了无效地址。')
      previewUrl.value = localPreviewUrl
    } catch (error) {
      if (revision !== revisions.record || request !== revisions.preview) return
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
    const revision = ++revisions.record
    await saveVideoView()
    if (revision !== revisions.record) return
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
        if (revision !== revisions.record) return
        if (detail?.record_id) { activeRecordId.value = detail.record_id; lastRecordId.value = detail.record_id; window.history.replaceState(null, '', recordLocation(detail.record_id)) }
        if (detail?.code === 'browser_verification_required') {
          verificationTarget.value = detail.url || target
          void refreshBrowserSession()
        }
        throw new Error(await readApiError(response))
      }
      const parsed = await response.json()
      if (revision !== revisions.record) return
      parseResult.value = parsed
      activeRecordId.value = parseResult.value?.record_id || ''
      lastRecordId.value = activeRecordId.value
      formatsFresh = true
      selectedFormat.value = parseResult.value?.formats.find((item) => item.format_id.startsWith('sniffpair:'))?.format_id || parseResult.value?.formats[0]?.format_id || ''
      if (activeRecordId.value) window.history.replaceState(null, '', recordLocation(activeRecordId.value))
      scheduleVideoView()
    } catch (error) {
      if (revision === revisions.record) errorMessage.value = error instanceof Error ? error.message : '解析失败，请确认链接是否公开可访问。'
    } finally {
      if (revision === revisions.record) loadingParse.value = false
      void loadRecentLinks()
      void historyPanel.value?.refresh()
      if (browserSession.value?.visible) void refreshBrowserSession()
    }
  }

  onBeforeUnmount(() => {
    flushVideoView()
    window.removeEventListener('hashchange', restoreFromLocation)
    window.removeEventListener('pagehide', flushVideoView)
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

  return {
    activeRecordId,
    browserBusy,
    browserSession,
    canDownload,
    canPreview,
    canSummarize,
    changeMapView,
    changeMode,
    closeVerificationBrowser,
    coverFailed,
    currentMapView,
    errorMessage,
    handlePreviewError,
    handlePreviewReady,
    hasRecentLinks,
    hideRecentLinksSoon,
    historyPanel,
    loadingDownload,
    loadingMindMap,
    loadingParse,
    loadingSummary,
    generateMindMap,
    statusFetchMs,
    mapRenderMs,
    mindMapTask,
    mindMapTone,
    mindMapVersions,
    mode,
    open,
    openVerificationBrowser,
    openVideoRecord,
    parseResult,
    parseVideo,
    previewError,
    previewState,
    previewUrl,
    previewVideoRef,
    recentLinks,
    recordMessage,
    removeRecentLink,
    removedVideoRecord,
    renderedMindMap,
    reparseHistory,
    resetPreview,
    retryVerifiedVideo,
    saveFinishedFile,
    saveMessage,
    savePlaybackProgress,
    saveVideoView,
    savingFile,
    scheduleVideoView,
    seekToTimestamp,
    selectMindMap,
    selectRecentLink,
    selectSummary,
    selectedFormat,
    showRecentLinks,
    sourceHost,
    startDownload,
    startMindMap,
    startPreview,
    startSummary,
    summaryTab,
    summaryTask,
    summaryTone,
    summaryVersions,
    supported,
    task,
    taskTone,
    url,
    verificationMessage,
    verificationTarget,
    videoWorkspace,
    viewSaveStatus
  }
}
