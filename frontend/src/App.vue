<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import MindMapCanvas from './MindMapCanvas.vue'
import { canSeekMedia, clampSeekTime, filterMindMapTask, type MindMapTask } from './mindMap'
import { getRecentLinks, saveRecentLink } from './recentLinks'
import { readPreviewUrl, selectPreviewFormat } from './videoPreview'
import WatermarkEditor from './WatermarkEditor.vue'
import MusicWorkspace from './MusicWorkspace.vue'
import { extractHttpUrl } from './downloadInput'
import { ArrowRight, Download, Eraser, LoaderCircle, Music2, Video } from 'lucide-vue-next'

type VideoFormat = {
  format_id: string
  label: string
  ext?: string | null
  resolution?: string | null
  filesize?: number | null
}

type ParseResult = {
  title: string
  thumbnail?: string | null
  duration?: number | null
  uploader?: string | null
  webpage_url?: string | null
  formats: VideoFormat[]
}

type TaskStatus = 'pending' | 'running' | 'finished' | 'failed'
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
const saveMessage = ref('')
const recentLinks = ref<string[]>([])
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
const summaryTab = ref<SummaryTab>('summary')

function changeMode(next: 'video' | 'music', focus = false) {
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
const canPreview = computed(() => Boolean(previewFormat.value && previewState.value !== 'loading'))

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

function loadRecentLinks() {
  recentLinks.value = getRecentLinks(window.localStorage)
}

function rememberRecentLink(target: string) {
  recentLinks.value = saveRecentLink(window.localStorage, target)
}

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
  const target = url.value.trim()
  if (!target) return
  errorMessage.value = ''
  saveMessage.value = ''
  loadingSummary.value = true
  summaryTask.value = null
  mindMapTask.value = null
  summaryTab.value = 'summary'
  stopSummaryPolling()
  stopMindMapPolling()

  try {
    const response = await fetch('/api/summaries', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: target }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    const data: { task_id: string } = await response.json()
    await pollSummary(data.task_id)
    summaryPollingTimer.value = window.setInterval(() => pollSummary(data.task_id), 1400)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '总结任务创建失败。'
  } finally {
    loadingSummary.value = false
  }
}

async function pollSummary(taskId: string) {
  try {
    const response = await fetch(`/api/summaries/${taskId}`)
    if (!response.ok) throw new Error(await readApiError(response))
    summaryTask.value = await response.json()
    if (summaryTask.value?.status === 'finished' || summaryTask.value?.status === 'failed') {
      stopSummaryPolling()
    }
  } catch (error) {
    stopSummaryPolling()
    errorMessage.value = error instanceof Error ? error.message : '总结任务状态查询失败。'
  }
}

async function startMindMap(regenerate = false) {
  const summaryId = summaryTask.value?.task_id
  if (!summaryId || summaryTask.value?.status !== 'finished') return
  loadingMindMap.value = true
  errorMessage.value = ''
  stopMindMapPolling()
  try {
    const suffix = regenerate ? '?regenerate=true' : ''
    const response = await fetch(`/api/summaries/${summaryId}/mind-map${suffix}`, { method: 'POST' })
    if (!response.ok) throw new Error(await readApiError(response))
    await pollMindMap(summaryId)
    if (mindMapTask.value?.status !== 'finished' && mindMapTask.value?.status !== 'failed') {
      mindMapPollingTimer.value = window.setInterval(() => pollMindMap(summaryId), 1400)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '思维导图任务创建失败。'
  } finally {
    loadingMindMap.value = false
  }
}

async function pollMindMap(summaryId: string) {
  try {
    const response = await fetch(`/api/summaries/${summaryId}/mind-map`)
    if (!response.ok) throw new Error(await readApiError(response))
    const filtered = filterMindMapTask(await response.json())
    if (!filtered) throw new Error('思维导图接口返回了无效数据。')
    mindMapTask.value = filtered
    if (filtered.status === 'finished' || filtered.status === 'failed') stopMindMapPolling()
  } catch (error) {
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
    return body.detail || '请求失败，请稍后再试。'
  } catch {
    return '请求失败，请稍后再试。'
  }
}

function resetPreview() {
  previewState.value = 'idle'
  previewUrl.value = ''
  previewError.value = ''
  previewVideoRef.value = null
  pendingSeekTime.value = null
}

function handlePreviewError() {
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
  const result = parseResult.value
  const format = previewFormat.value
  const target = result?.webpage_url || url.value.trim()
  if (!result || !format || !target || previewState.value === 'loading') return

  previewState.value = 'loading'
  previewUrl.value = ''
  previewError.value = ''

  try {
    const response = await fetch('/api/previews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: target, format_id: format.format_id }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    const localPreviewUrl = readPreviewUrl(await response.json())
    if (!localPreviewUrl) throw new Error('预览接口返回了无效地址。')
    previewUrl.value = localPreviewUrl
  } catch (error) {
    previewState.value = 'error'
    previewError.value = error instanceof Error ? error.message : '视频连接失败，请稍后重试。'
  }
}

async function parseVideo() {
  const target = extractHttpUrl(url.value)
  if (!target) {
    errorMessage.value = '请先粘贴视频链接或完整的分享文字。'
    return
  }
  rememberRecentLink(target)
  errorMessage.value = ''
  saveMessage.value = ''
  resetPreview()
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

  try {
    const response = await fetch('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: target }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    parseResult.value = await response.json()
    selectedFormat.value = parseResult.value?.formats.find((item) => item.format_id.startsWith('sniffpair:'))?.format_id || parseResult.value?.formats[0]?.format_id || ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '解析失败，请确认链接是否公开可访问。'
  } finally {
    loadingParse.value = false
  }
}

async function startDownload() {
  const target = parseResult.value?.webpage_url || extractHttpUrl(url.value)
  if (!target) return
  errorMessage.value = ''
  saveMessage.value = ''
  loadingDownload.value = true
  task.value = null

  try {
    if (window.showSaveFilePicker) {
      try {
        saveHandle.value = await chooseSaveHandle()
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
      }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    const data: { task_id: string } = await response.json()
    await pollTask(data.task_id)
    pollingTimer.value = window.setInterval(() => pollTask(data.task_id), 1200)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下载任务创建失败。'
  } finally {
    loadingDownload.value = false
  }
}

async function pollTask(taskId: string) {
  try {
    const response = await fetch(`/api/tasks/${taskId}`)
    if (!response.ok) throw new Error(await readApiError(response))
    task.value = await response.json()
    if (task.value?.status === 'finished' || task.value?.status === 'failed') {
      stopPolling()
      if (task.value.status === 'finished' && task.value.download_url && saveHandle.value) {
        await saveFinishedFile(saveHandle.value)
      }
    }
  } catch (error) {
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
  stopPolling()
  stopSummaryPolling()
  stopMindMapPolling()
})

onMounted(() => {
  loadRecentLinks()
})
</script>

<template>
  <div class="page-shell">
    <header class="topbar">
      <a class="brand" href="#">
        <span class="brand-mark">VD</span>
        <span>VideoDream</span>
      </a>
      <nav class="nav-links" aria-label="主导航">
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
            <button id="music-tab" role="tab" :aria-selected="mode === 'music'" :tabindex="mode === 'music' ? 0 : -1" aria-controls="music-panel" :class="{ active: mode === 'music' }" @click="changeMode('music')" @keydown.left.prevent="changeMode('video', true)" @keydown.right.prevent="changeMode('video', true)"><Music2 :size="21" /><span>音乐下载</span></button>
          </div>
          <MusicWorkspace v-show="mode === 'music'" :active="mode === 'music'" />
          <div v-show="mode === 'video'" id="video-panel" role="tabpanel" aria-labelledby="video-tab">
          <div class="search-area">
            <form class="link-composer" @submit.prevent="parseVideo">
              <textarea
                v-model="url"
                aria-label="视频链接或分享文字"
                placeholder="粘贴视频链接或分享文字…"
                autocomplete="off"
                @focus="showRecentLinks = true"
                @blur="hideRecentLinksSoon"
                @keydown.ctrl.enter.prevent="parseVideo"
              />
              <div class="composer-footer"><span class="composer-source">抖音 · B站 · YouTube</span><button class="composer-submit" type="submit" :disabled="loadingParse || !url.trim()"><LoaderCircle v-if="loadingParse" :size="18" class="spin" /><ArrowRight v-else :size="18" />{{ loadingParse ? '解析中' : '解析视频' }}</button></div>
            </form>
            <div v-if="showRecentLinks && hasRecentLinks" class="recent-links" aria-label="最近输入链接">
              <button
                v-for="item in recentLinks"
                :key="item"
                type="button"
                @mousedown.prevent="selectRecentLink(item)"
              >
                <span>最近</span>
                <strong>{{ item }}</strong>
              </button>
            </div>
          </div>

          <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
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
                  {{ previewState === 'error' ? '↻' : '▶' }}
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

            <div v-if="summaryTask.result" class="summary-result">
              <div class="summary-meta">
                <span>{{ summaryTask.result.source }}</span>
                <span>{{ summaryTask.result.language }}</span>
                <span>{{ summaryTask.result.transcript.length }} 条字幕</span>
              </div>
              <div class="summary-actions">
                <a v-if="summaryTask.result.markdown_url" :href="summaryTask.result.markdown_url" download>下载 Markdown</a>
                <a v-if="summaryTask.result.json_url" :href="summaryTask.result.json_url" download>下载 JSON</a>
              </div>
              <div class="summary-tabs" role="tablist" aria-label="总结视图">
                <button type="button" role="tab" :aria-selected="summaryTab === 'summary'" @click="summaryTab = 'summary'">总结</button>
                <button type="button" role="tab" :aria-selected="summaryTab === 'transcript'" @click="summaryTab = 'transcript'">字幕</button>
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
                  <div v-if="mindMapTask.result" class="mind-map-tree-wrap">
                    <MindMapCanvas
                      :result="mindMapTask.result"
                      :seek-enabled="Boolean(previewVideoRef || canPreview)"
                      @seek="seekToTimestamp"
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
</template>
