import { useVideoSummary } from './useVideoSummary'
import { useBilibiliSession } from './useBilibiliSession'
import { useVideoDownload } from './useVideoDownload'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { canSeekMedia, clampSeekTime, filterMindMapTask, type MindMapTask } from './mindMap'
import { getRecentLinks } from './recentLinks'
import { readRecordLocation, recordLocation, type MapView, type VideoTaskReference, type VideoView } from './videoHistory'
import './video-history.css'
import { downloadedPreviewUrl, monitorPreview, readPreviewStatusUrl, readPreviewUrl, selectPreviewFormat, waitForPreview, type PreviewStatus } from './videoPreview'
import { matchRefreshedFormat, refreshDownloadFormats } from './videoFormats'
import { account } from '../accounts/auth'
import { api } from '../../shared/api/client'
import { mediaFailureMessage, probeMedia } from '../../shared/media'
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
  let previewRecoveryUsed = false
  let previewRecovering = false
  const previewMessage = ref('')
  const previewNotice = ref('')
  const silentPreviewUrl = ref('')
  let previewSessionUrl = ''
  let previewAbort: AbortController | null = null
  let allowPreviewFallback = false
  let previewStarted = 0
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
  const bilibili = useBilibiliSession()
  watch(activeRecordId, () => bilibili.closeBilibiliLogin())
  watch(() => account.value?.id, () => {
    revisions.record++
    loadingSummary.value = false
    bilibili.resetBilibiliSession()
    if (account.value) void bilibili.refreshBilibiliStatus()
  })
  let restoringRecord = false
  let formatsFresh = false
  let viewSaveTimer: number | undefined
  let viewSaveQueue: Promise<void> = Promise.resolve()
  let lastPlaybackSaved = 0

  function changeMode(next: 'video' | 'music', focus = false) {
    if (next === 'music') { resetPreview(); returnMusicCenter(); return }
    mode.value = next
    if (focus) document.getElementById(`${next}-tab`)?.focus()
  }

  const supported = [
    { label: 'B站', href: 'https://www.bilibili.com/' },
    { label: '抖音公开视频', href: 'https://www.douyin.com/' },
    { label: '小红书公开视频', href: 'https://www.xiaohongshu.com/' },
    { label: '微博', href: 'https://weibo.com/' },
  ]
  const taskTone = computed(() => task.value?.status || 'idle')
  const summaryTone = computed(() => summaryTask.value?.status || 'idle')
  const mindMapTone = computed(() => mindMapTask.value?.status || 'idle')
  const canDownload = computed(() => Boolean(parseResult.value && url.value.trim() && !loadingDownload.value && !savingFile.value
    && !['pending', 'running'].includes(task.value?.status || '')))
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
    if (!parseResult.value) return null
    return selectPreviewFormat(parseResult.value.formats, selectedFormat.value)
  })
  const canPreview = computed(() => Boolean(parseResult.value && (previewFormat.value || !parseResult.value.formats.length
    || (localVideoUrl.value && !localPreviewFailed.value)) && previewState.value !== 'loading'))

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
    requestBilibiliLogin: bilibili.requestBilibiliLogin,
    requestBilibiliSpeechConfirmation: bilibili.requestBilibiliSpeechConfirmation,
  })
  const { startDownload, startPolling, saveFinishedFile, resetDownload, cancelSave, fileSaved, saveProgress, downloadDestination } = useVideoDownload({
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
    bilibili.closeBilibiliLogin()
    const revision = ++revisions.record
    resetDownload()
    await saveVideoView()
    if (revision !== revisions.record) return
    restoringRecord = true
    loadingParse.value = false; loadingSummary.value = false; loadingMindMap.value = false; loadingDownload.value = false
    activate(); videoWorkspace.value = 'current'
    errorMessage.value = ''; saveMessage.value = ''; viewSaveStatus.value = ''
    stopSummaryPolling(); stopMindMapPolling(); resetPreview()
    try {
      const detail = await api(`/api/videos/records/${id}`)
      if (revision !== revisions.record) return
      activeRecordId.value = id; lastRecordId.value = id
      url.value = detail.url
      parseResult.value = detail.result ? { ...detail.result, record_id: id } : null
      selectedFormat.value = detail.view?.selected_format ?? detail.result?.formats[0]?.format_id ?? ''
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
        localVideoUrl.value = downloadedPreviewUrl(task.value) || ''
        if (['pending', 'running'].includes(task.value?.status || '')) await startPolling(download.task_id)
      }
      if (revision !== revisions.record) return
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
    bilibili.closeBilibiliLogin()
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
    const fresh: ParseResult = await api(`/api/videos/records/${id}/formats?force=${force}`, { method: 'POST' })
    if (revision !== revisions.record || id !== activeRecordId.value || !parseResult.value) return false
    const refreshed = refreshDownloadFormats(parseResult.value, fresh, selectedFormat.value)
    parseResult.value = refreshed.result
    selectedFormat.value = refreshed.selectedId
    formatsFresh = true
    return true
  }

  function reparseHistory(target: string) { videoWorkspace.value = 'current'; url.value = target; void parseVideo(true) }
  function removedVideoRecord(id: string) {
    if (activeRecordId.value === id) { revisions.record++; resetDownload(); activeRecordId.value = ''; parseResult.value = null; summaryTask.value = null; mindMapTask.value = null; task.value = null; resetPreview(); stopSummaryPolling(); stopMindMapPolling(); window.history.replaceState(null, '', '#workspace') }
    if (!activeRecordId.value) { loadingParse.value = false; loadingSummary.value = false; loadingMindMap.value = false; loadingDownload.value = false; restoringRecord = false }
    if (lastRecordId.value === id) lastRecordId.value = ''
  }
  function restoreFromLocation() { const route = readRecordLocation(window.location.hash); if (route && route.recordId !== activeRecordId.value) void openVideoRecord(route.recordId, route.tab) }
  function flushVideoView() { void saveVideoView(true) }
  watch(summaryTab, () => { if (activeRecordId.value && !restoringRecord) window.history.replaceState(null, '', recordLocation(activeRecordId.value, summaryTab.value)); scheduleVideoView() })
  watch(selectedFormat, scheduleVideoView)
  watch(localVideoUrl, () => { localPreviewFailed.value = false })

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

  function releasePreview() {
    previewAbort?.abort()
    previewAbort = null
    const target = previewSessionUrl
    previewSessionUrl = ''
    return target ? fetch(target, { method: 'DELETE', keepalive: true }).then(() => undefined).catch(() => undefined) : Promise.resolve()
  }

  function leavePage() {
    flushVideoView()
    resetPreview()
  }

  function resetPreview() {
    allowPreviewFallback = false
    revisions.preview++
    if (previewState.value === 'ready' && previewVideoRef.value) playbackTime.value = previewVideoRef.value.currentTime
    previewState.value = 'idle'
    previewUrl.value = ''
    previewError.value = ''
    releasePreview()
    previewMessage.value = ''
    previewNotice.value = ''
    silentPreviewUrl.value = ''
    previewVideoRef.value = null
    pendingSeekTime.value = null
  }

  async function handlePreviewError() {
    if (previewRecovering || !previewUrl.value) return
    const source = previewUrl.value
    const revision = revisions.preview
    const video = previewVideoRef.value
    const position = video?.currentTime || playbackTime.value
    if (previewRecoveryUsed) {
      previewState.value = 'error'
      previewError.value = '视频恢复失败，请重新点击播放。'
      return
    }
    previewRecoveryUsed = true
    previewRecovering = true
    const diagnostic = new AbortController()
    const timer = window.setTimeout(() => diagnostic.abort(), 12000)
    try {
      const result = await probeMedia(source, diagnostic.signal)
      if (revision !== revisions.preview || source !== previewUrl.value) return
      if (result.recovered && video) {
        pendingSeekTime.value = position
        previewState.value = 'loading'
        video.load()
        void video.play().catch(() => undefined)
      } else if (result.status === 410 && source !== localVideoUrl.value) {
        formatsFresh = false
        pendingSeekTime.value = position
        previewState.value = 'error'
        await startPreview(true, true)
      } else if (result.status >= 200 && result.status < 300 && (source === localVideoUrl.value || allowPreviewFallback)) {
        const forcePrepare = source !== localVideoUrl.value
        if (!forcePrepare) localPreviewFailed.value = true
        allowPreviewFallback = false
        pendingSeekTime.value = position
        previewState.value = 'error'
        await startPreview(forcePrepare, true)
      } else {
        if (source === localVideoUrl.value) localPreviewFailed.value = true
        previewState.value = 'error'
        previewError.value = mediaFailureMessage(result.status, video?.error?.code)
      }
    } catch {
      if (revision === revisions.preview) {
        previewState.value = 'error'
        previewError.value = '视频连接中断，请检查网络后重试。'
      }
    } finally { window.clearTimeout(timer); previewRecovering = false }
  }

  function continueSilentPreview() {
    if (!silentPreviewUrl.value) return
    previewState.value = 'loading'
    previewUrl.value = silentPreviewUrl.value
    silentPreviewUrl.value = ''
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

  function handlePreviewFirstFrame() {
    if (previewStarted) {
      performance.measure('video-preview:first-frame', { start: previewStarted, end: performance.now() })
      previewStarted = 0
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

  async function startPreview(forcePrepare = false, retry = false) {
    if (!parseResult.value || previewState.value === 'loading') return
    if (retry !== true) previewRecoveryUsed = false
    const revision = revisions.record
    const request = ++revisions.preview
    const recordId = activeRecordId.value

    previewState.value = 'loading'
    previewUrl.value = ''
    previewError.value = ''
    await releasePreview()
    if (revision !== revisions.record || request !== revisions.preview) return
    previewStarted = performance.now()
    allowPreviewFallback = false
    silentPreviewUrl.value = ''
    previewNotice.value = ''
    previewMessage.value = '正在准备音视频'
    const controller = new AbortController()
    previewAbort = controller

    try {
      if (!forcePrepare && localVideoUrl.value && !localPreviewFailed.value) {
        previewNotice.value = '正在播放已下载的视频'
        previewUrl.value = localVideoUrl.value
        return
      }
      const target = parseResult.value?.webpage_url || extractHttpUrl(url.value)
      if (!target) throw new Error('当前视频没有可用的播放源。')
      let playbackFormat = previewFormat.value
      let initial: PreviewStatus | null = null
      if (!forcePrepare && recordId) {
        const cached = await fetch('/api/previews/reuse', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: target, format_id: selectedFormat.value.startsWith('saved:') ? undefined : selectedFormat.value || undefined, parse_record_id: recordId }),
        })
        if (!cached.ok) throw new Error(await readApiError(cached))
        initial = await cached.json()
        if (revision !== revisions.record || request !== revisions.preview) {
          const stale = initial && readPreviewStatusUrl(initial)
          if (stale) void fetch(stale, { method: 'DELETE', keepalive: true }).catch(() => undefined)
          return
        }
      }
      if (!initial) {
        const selected = parseResult.value?.formats.find(item => item.format_id === selectedFormat.value)
        // Preview URLs may expire independently of the displayed download choices.
        const fresh = recordId && !formatsFresh
          ? await api<ParseResult>(`/api/videos/records/${recordId}/formats?force=false`, { method: 'POST', signal: controller.signal })
          : parseResult.value
        if (revision !== revisions.record || request !== revisions.preview) return
        const matched = matchRefreshedFormat(fresh?.formats || [], selected)
        playbackFormat = selectPreviewFormat(fresh?.formats || [], matched?.format_id || '')
        if (!playbackFormat) throw new Error('当前视频没有可用的播放源。')
        const response = await fetch('/api/previews', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: target, format_id: playbackFormat.format_id, parse_record_id: recordId || undefined, force_prepare: forcePrepare }),
        })
        if (!response.ok) throw new Error(await readApiError(response))
        initial = await response.json()
      }
      if (!initial) throw new Error('预览接口返回了无效状态。')
      const sessionUrl = readPreviewStatusUrl(initial)
      if (revision !== revisions.record || request !== revisions.preview) {
        if (sessionUrl) void fetch(sessionUrl, { method: 'DELETE', keepalive: true }).catch(() => undefined)
        return
      }
      previewSessionUrl = sessionUrl || ''
      const prepared = await waitForPreview(initial, (target, signal) => api<PreviewStatus>(target, { signal }), controller.signal,
        status => { previewMessage.value = status.message || '正在准备音视频' })
      if (revision !== revisions.record || request !== revisions.preview) return
      const localPreviewUrl = readPreviewUrl(prepared)
      if (!localPreviewUrl) throw new Error('预览接口返回了无效地址。')
      allowPreviewFallback = !forcePrepare && (prepared.reused || prepared.delivery === 'proxy' || prepared.delivery === 'fragmented')
      if (prepared.delivery === 'fragmented' && prepared.complete === false) {
        void monitorPreview(prepared, (target, signal) => api<PreviewStatus>(target, { signal }), controller.signal).catch(error => {
          if (controller.signal.aborted || request !== revisions.preview || revision !== revisions.record) return
          allowPreviewFallback = false
          previewUrl.value = ''
          previewState.value = 'error'
          previewError.value = error instanceof Error ? error.message : '视频准备中断，请重试播放。'
          void releasePreview()
        })
      }
      previewNotice.value = prepared.has_audio === false ? '当前源仅含画面，没有可用音频。' : prepared.message || (playbackFormat?.format_id.startsWith('sniffpair:') && playbackFormat.format_id !== selectedFormat.value ? '预览使用完整音视频线路' : '')
      if (prepared.has_audio === false) {
        previewState.value = 'idle'
        silentPreviewUrl.value = localPreviewUrl
      } else previewUrl.value = localPreviewUrl
    } catch (error) {
      if (revision !== revisions.record || request !== revisions.preview) return
      previewState.value = 'error'
      previewError.value = error instanceof Error ? error.message : '视频连接失败，请稍后重试。'
      releasePreview()
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
    bilibili.closeBilibiliLogin()
    const revision = ++revisions.record
    resetDownload()
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
    revisions.record++
    flushVideoView()
    resetPreview()
    window.removeEventListener('hashchange', restoreFromLocation)
    window.removeEventListener('pagehide', leavePage)
    resetDownload()
    stopSummaryPolling()
    stopMindMapPolling()
  })

  onMounted(() => {
    void bilibili.refreshBilibiliStatus()
    void loadRecentLinks()
    void refreshBrowserSession()
    window.addEventListener('hashchange', restoreFromLocation)
    window.addEventListener('pagehide', leavePage)
    const route = readRecordLocation(window.location.hash)
    if (route) void openVideoRecord(route.recordId, route.tab)
    else void api('/api/videos/last-record').then(data => { lastRecordId.value = data.record_id || '' }).catch(() => {})
  })

  return {
    ...bilibili,
    activeRecordId,
    browserBusy,
    browserSession,
    canDownload,
    canPreview,
    canSummarize,
    cancelSave,
    changeMapView,
    changeMode,
    closeVerificationBrowser,
    coverFailed,
    currentMapView,
    errorMessage,
    fileSaved,
    handlePreviewError,
    handlePreviewReady,
    handlePreviewFirstFrame,
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
    previewMessage,
    previewNotice,
    silentPreviewUrl,
    continueSilentPreview,
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
    saveProgress,
    downloadDestination,
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
