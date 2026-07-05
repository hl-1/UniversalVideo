<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

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

type TaskResult = {
  task_id: string
  status: TaskStatus
  progress: number
  message: string
  filename?: string | null
  download_url?: string | null
  error?: string | null
}

type CookiePlatform = 'bilibili' | 'youtube' | 'pornhub' | 'douyin'
type BrowserSource = 'chrome' | 'edge' | 'firefox' | 'brave' | 'vivaldi'

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
const loadingParse = ref(false)
const loadingDownload = ref(false)
const savingFile = ref(false)
const errorMessage = ref('')
const saveMessage = ref('')
const cookieMessage = ref('')
const pollingTimer = ref<number | undefined>()
const coverFailed = ref(false)
const saveHandle = ref<FilePickerHandle | null>(null)
const cookieModalOpen = ref(false)
const cookieContent = ref('')
const savingCookies = ref(false)
const useBrowserCookies = ref(false)
const browserSource = ref<BrowserSource>('chrome')

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
  { title: '选择位置保存', text: '支持浏览器系统保存对话框，把文件保存到你指定的位置。' },
  { title: '自动合并音频', text: '遇到视频和音频分离的平台格式，默认合并最佳音频流。' },
]
const samples = ['公开课留档', '素材归档', '运营备份', '学习资料整理']
const browserOptions: Array<{ label: string; value: BrowserSource }> = [
  { label: 'Chrome', value: 'chrome' },
  { label: 'Edge', value: 'edge' },
  { label: 'Firefox', value: 'firefox' },
  { label: 'Brave', value: 'brave' },
  { label: 'Vivaldi', value: 'vivaldi' },
]

const taskTone = computed(() => task.value?.status || 'idle')
const canDownload = computed(() => Boolean(parseResult.value && url.value.trim() && !loadingDownload.value))
const currentPlatform = computed<CookiePlatform | null>(() => detectPlatform(url.value))
const sourceHost = computed(() => {
  const target = parseResult.value?.webpage_url
  if (!target) return ''
  try {
    return new URL(target).hostname.replace(/^www\./, '')
  } catch {
    return ''
  }
})
const canShowCookieHelp = computed(() => {
  const text = errorMessage.value.toLowerCase()
  return Boolean(
    currentPlatform.value &&
      (text.includes('cookies') ||
        text.includes('登录态') ||
        text.includes('机器人') ||
        text.includes('412') ||
        text.includes('410')),
  )
})

function detectPlatform(rawUrl: string): CookiePlatform | null {
  try {
    const host = new URL(rawUrl.trim()).hostname.toLowerCase()
    if (host.includes('bilibili.com') || host.includes('b23.tv')) return 'bilibili'
    if (host.includes('youtube.com') || host.includes('youtu.be')) return 'youtube'
    if (host.includes('pornhub.com')) return 'pornhub'
    if (host.includes('douyin.com') || host.includes('iesdouyin.com')) return 'douyin'
  } catch {
    return null
  }
  return null
}

function platformName(platform: CookiePlatform | null) {
  if (platform === 'bilibili') return 'B站'
  if (platform === 'youtube') return 'YouTube'
  if (platform === 'pornhub') return 'PornHub'
  if (platform === 'douyin') return '抖音'
  return '当前平台'
}

function platformLoginUrl(platform: CookiePlatform | null) {
  if (platform === 'bilibili') return 'https://passport.bilibili.com/login'
  if (platform === 'youtube') return 'https://accounts.google.com/'
  if (platform === 'pornhub') return 'https://www.pornhub.com/login'
  if (platform === 'douyin') return 'https://www.douyin.com/'
  return '#'
}

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

function suggestedExtension() {
  const selected = parseResult.value?.formats.find((item) => item.format_id === selectedFormat.value)
  const ext = selected?.ext?.toLowerCase()
  if (ext === 'm4a' || ext === 'mp3' || selected?.label.includes('音频')) return 'm4a'
  return 'mp4'
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

async function parseVideo() {
  const target = url.value.trim()
  if (!target) {
    errorMessage.value = '请先粘贴一个公开视频链接。'
    return
  }
  errorMessage.value = ''
  saveMessage.value = ''
  cookieMessage.value = ''
  parseResult.value = null
  task.value = null
  selectedFormat.value = ''
  coverFailed.value = false
  saveHandle.value = null
  loadingParse.value = true

  try {
    const response = await fetch('/api/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: target,
        use_browser_cookies: useBrowserCookies.value,
        browser: browserSource.value,
      }),
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

function openCookieModal() {
  cookieContent.value = ''
  cookieMessage.value = ''
  cookieModalOpen.value = true
}

function openPlatformLogin() {
  const target = platformLoginUrl(currentPlatform.value)
  if (target !== '#') window.open(target, '_blank', 'noreferrer')
}

async function importCookieFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  cookieContent.value = await file.text()
  cookieMessage.value = `已读取 ${file.name}，请确认后保存并重试。`
  input.value = ''
}

async function saveCookiesAndRetry() {
  const platform = currentPlatform.value
  if (!platform) {
    cookieMessage.value = '无法识别当前链接平台。'
    return
  }
  savingCookies.value = true
  cookieMessage.value = ''

  try {
    const response = await fetch('/api/cookies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        platform,
        content: cookieContent.value,
      }),
    })
    if (!response.ok) throw new Error(await readApiError(response))
    cookieModalOpen.value = false
    cookieContent.value = ''
    cookieMessage.value = `${platformName(platform)} cookies 已保存，正在重新解析。`
    await parseVideo()
  } catch (error) {
    cookieMessage.value = error instanceof Error ? error.message : '保存 cookies 失败。'
  } finally {
    savingCookies.value = false
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
        use_browser_cookies: useBrowserCookies.value,
        browser: browserSource.value,
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

onBeforeUnmount(stopPolling)
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
          <div class="search-bar">
            <span class="search-icon">⌕</span>
            <input
              v-model="url"
              type="url"
              placeholder="粘贴公开视频链接，例如 https://..."
              @keyup.enter="parseVideo"
            />
            <button :disabled="loadingParse" @click="parseVideo">
              {{ loadingParse ? '解析中' : '解析' }}
            </button>
          </div>

          <div class="auth-strip">
            <label class="switch-line">
              <input v-model="useBrowserCookies" type="checkbox" />
              <span>本机授权模式</span>
            </label>
            <label class="browser-picker">
              <span>读取</span>
              <select v-model="browserSource" :disabled="!useBrowserCookies">
                <option v-for="item in browserOptions" :key="item.value" :value="item.value">
                  {{ item.label }}
                </option>
              </select>
              <span>已登录状态</span>
            </label>
          </div>
          <p v-if="useBrowserCookies" class="auth-hint">
            仅在你的电脑本机调用 yt-dlp 读取所选浏览器登录态，不在页面展示 Cookie，也不写入项目配置文件。
          </p>

          <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
          <p v-if="saveMessage" class="success-text">{{ saveMessage }}</p>
          <p v-if="cookieMessage" class="success-text">{{ cookieMessage }}</p>
          <button v-if="canShowCookieHelp" class="secondary-action" @click="openCookieModal">
            使用 {{ platformName(currentPlatform) }} 授权 cookies 兜底
          </button>

          <div v-if="parseResult" class="result-card">
            <div class="cover-wrap">
              <img
                v-if="parseResult.thumbnail && !coverFailed"
                :src="parseResult.thumbnail"
                :alt="parseResult.title"
                @error="coverFailed = true"
              />
              <div v-else class="cover-empty">VD</div>
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
                <select v-model="selectedFormat">
                  <option value="">自动选择最佳格式</option>
                  <option v-for="item in parseResult.formats" :key="item.format_id" :value="item.format_id">
                    {{ item.label }}
                  </option>
                </select>
              </label>
              <button class="primary-action" :disabled="!canDownload" @click="startDownload">
                {{ loadingDownload ? '准备文件中' : '选择位置并下载' }}
              </button>
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

    <div v-if="cookieModalOpen" class="modal-mask" role="dialog" aria-modal="true" aria-label="填写 cookies">
      <section class="cookie-modal">
        <div class="modal-head">
          <div>
            <span class="eyebrow">{{ platformName(currentPlatform) }} cookies</span>
            <h2>粘贴 Netscape 格式 cookies</h2>
          </div>
          <button class="icon-button" aria-label="关闭" @click="cookieModalOpen = false">×</button>
        </div>
        <p>
          cookies 只会保存到本机项目目录的 <code>config</code> 文件夹，用于 yt-dlp
          解析当前平台。请只粘贴你本人账号导出的 Netscape 格式内容。
        </p>
        <div class="cookie-tools">
          <button class="secondary-action compact" @click="openPlatformLogin">
            打开 {{ platformName(currentPlatform) }} 登录页
          </button>
          <label class="secondary-action compact file-trigger">
            上传 cookies.txt
            <input type="file" accept=".txt,text/plain" @change="importCookieFile" />
          </label>
        </div>
        <textarea
          v-model="cookieContent"
          spellcheck="false"
          placeholder="# Netscape HTTP Cookie File&#10;.youtube.com&#9;TRUE&#9;/&#9;FALSE&#9;..."
        ></textarea>
        <p v-if="cookieMessage" class="error-text">{{ cookieMessage }}</p>
        <div class="modal-actions">
          <button class="secondary-action compact" @click="cookieModalOpen = false">取消</button>
          <button class="primary-action compact" :disabled="savingCookies" @click="saveCookiesAndRetry">
            {{ savingCookies ? '保存中' : '保存并重试' }}
          </button>
        </div>
      </section>
    </div>
  </div>
</template>
