<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { getRecentLinks, saveRecentLink } from './recentLinks'
import { readPreviewUrl, selectPreviewFormat } from './videoPreview'

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
const selectedFormat = ref('')
const parseResult = ref<ParseResult | null>(null)
const task = ref<TaskResult | null>(null)
const summaryTask = ref<SummaryTaskResult | null>(null)
const loadingParse = ref(false)
const loadingDownload = ref(false)
const loadingSummary = ref(false)
const savingFile = ref(false)
const errorMessage = ref('')
const saveMessage = ref('')
const recentLinks = ref<string[]>([])
const showRecentLinks = ref(false)
const pollingTimer = ref<number | undefined>()
const summaryPollingTimer = ref<number | undefined>()
const coverFailed = ref(false)
const saveHandle = ref<FilePickerHandle | null>(null)
const previewState = ref<PreviewState>('idle')
const previewUrl = ref('')
const previewError = ref('')

const supported = [
  { label: 'B站', href: 'https://www.bilibili.com/' },
  { label: 'YouTube', href: 'https://www.youtube.com/' },
  { label: '抖音公开视频', href: 'https://www.douyin.com/' },
  { label: '小红书公开视频', href: 'https://www.xiaohongshu.com/' },
  { label: '微博', href: 'https://weibo.com/' },
  { label: '更多 yt-dlp 支持站点', href: 'https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md' },
]
const perks = [
  { title: '先解析再下载', text: '标题、封面、格式信息先确认，避免盲目保存错误内容。' },
  { title: '字幕驱动总结', text: '优先提取平台字幕与自动字幕，再生成结构化摘要和时间线。' },
  { title: '选择位置保存', text: '支持浏览器系统保存对话框，把文件保存到你指定的位置。' },
]
const samples = ['公开课留档', '素材归档', 'AI 视频总结', '字幕笔记整理']

const taskTone = computed(() => task.value?.status || 'idle')
const summaryTone = computed(() => summaryTask.value?.status || 'idle')
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
  stopSummaryPolling()

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
}

function handlePreviewError() {
  previewUrl.value = ''
  previewState.value = 'error'
  previewError.value = '视频连接失败，请重新解析或稍后重试。'
}

function handlePreviewReady() {
  previewState.value = 'ready'
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
  const target = url.value.trim()
  if (!target) {
    errorMessage.value = '请先粘贴一个公开视频链接。'
    return
  }
  rememberRecentLink(target)
  errorMessage.value = ''
  saveMessage.value = ''
  resetPreview()
  parseResult.value = null
  task.value = null
  summaryTask.value = null
  selectedFormat.value = ''
  coverFailed.value = false
  saveHandle.value = null
  stopSummaryPolling()
  loadingParse.value = true

  try {
    const response = await fetch('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: target }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    parseResult.value = await response.json()
    selectedFormat.value = parseResult.value?.formats[0]?.format_id || ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '解析失败，请确认链接是否公开可访问。'
  } finally {
    loadingParse.value = false
  }
}

async function startDownload() {
  const target = url.value.trim()
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
        <a href="#platforms">支持平台</a>
        <a href="#notice">授权提醒</a>
      </nav>
      <a class="login-pill" href="#workspace">立即体验</a>
    </header>

    <main>
      <section id="workspace" class="hero">
        <div class="hero-copy">
          <span class="eyebrow">公开视频下载助手</span>
          <h1>VideoDream，让公开视频保存更像一套专业工作流</h1>
          <p>
            粘贴链接，先解析标题、封面与格式，再选择保存位置并创建下载任务。轻量封装 yt-dlp，
            为内容整理、学习留档和素材归档提供清爽入口。
          </p>
          <div class="scenario-list" aria-label="使用场景">
            <span v-for="item in samples" :key="item">{{ item }}</span>
          </div>
        </div>

        <section class="download-panel" aria-label="视频下载工作台">
          <div class="panel-kicker">
            <span>解析工作台</span>
            <strong>yt-dlp powered</strong>
          </div>
          <div class="search-area">
            <div class="search-bar">
              <span class="search-icon">⌕</span>
              <input
                v-model="url"
                type="url"
                placeholder="粘贴公开视频链接，例如 https://..."
                autocomplete="off"
                @focus="showRecentLinks = true"
                @blur="hideRecentLinksSoon"
                @keyup.enter="parseVideo"
              />
              <button :disabled="loadingParse" @click="parseVideo">
                {{ loadingParse ? '解析中' : '解析' }}
              </button>
            </div>
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
              <section class="summary-text" aria-label="AI 总结">
                <pre>{{ plainMarkdown(summaryTask.result.summary_markdown) }}</pre>
              </section>
              <section class="transcript-list" aria-label="字幕文本">
                <div v-for="item in summaryTask.result.transcript.slice(0, 80)" :key="`${item.timestamp}-${item.text}`">
                  <time>{{ item.timestamp }}</time>
                  <span>{{ item.text }}</span>
                </div>
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
          </div>
        </section>
      </section>

      <section id="platforms" class="platform-band">
        <div class="section-title">
          <span>通用支持</span>
          <h2>把复杂下载能力收进一个干净入口</h2>
        </div>
        <div class="tag-grid">
          <a v-for="item in supported" :key="item.href" :href="item.href" target="_blank" rel="noreferrer">
            {{ item.label }}
          </a>
        </div>
      </section>

      <section class="perk-grid" aria-label="核心优势">
        <article v-for="item in perks" :key="item.title">
          <h3>{{ item.title }}</h3>
          <p>{{ item.text }}</p>
        </article>
      </section>

      <section id="notice" class="notice">
        <strong>授权提醒</strong>
        <span>请仅下载你拥有权利、获得授权或平台允许保存的公开视频内容。VideoDream 不提供登录代办、会员内容下载或规避访问限制能力。</span>
      </section>
    </main>
  </div>
</template>
