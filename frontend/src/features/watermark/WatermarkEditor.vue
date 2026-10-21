<script setup lang="ts">
import { saveRemoteFile, saveMessage } from '../../shared/downloads'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Crosshair, Download, Eraser, Pause, Play, RefreshCw, ScanEye, Square, Trash2, Undo2 } from 'lucide-vue-next'
import { clampRegion, regionFromPoints, type Point, type WatermarkRegion } from './watermarkRegions'
import { downloadUrl, mediaFailureMessage, probeMedia } from '../../shared/media'

const props = defineProps<{ preferredFilename?: string | null }>()
type VideoInfo = { filename: string; width: number; height: number; duration: number; preview_url: string; fps: number }
type Job = {
  task_id: string
  status: 'pending' | 'running' | 'finished' | 'failed' | 'cancelled'
  progress: number
  message: string
  error?: string | null
  filename?: string | null
  download_url?: string | null
  frames_processed?: number
  total_frames?: number
  elapsed_seconds?: number
  remaining_seconds?: number | null
  processing_device?: string | null
}
const files = ref<Array<{ filename: string; size: number; original_filename?: string | null }>>([])
const filename = ref('')
const info = ref<VideoInfo | null>(null)
const regions = ref<WatermarkRegion[]>([])
const activeRegion = ref(-1)
const draft = ref<WatermarkRegion | null>(null)
const video = ref<HTMLVideoElement | null>(null)
const job = ref<Job | null>(null)
const view = ref<'original' | 'preview' | 'result'>('original')
const method = ref<'lama' | 'delogo'>('lama')
const lamaAvailable = ref(false)
const modelMessage = ref('正在检查修复模型')
const processingDevice = ref('')
const previewImage = ref('')
const previewing = ref(false)
const estimatedMinutes = ref(0)
const error = ref('')
const videoError = ref('')
const savingMessage = ref('')
const loadingFiles = ref(false)
const loadingInfo = ref(false)
const submitting = ref(false)
const saving = ref(false)
const playing = ref(false)
const ready = ref(false)
const currentTime = ref(0)
let infoRequest = 0
let pollTimer: number | undefined
let startPoint: Point | null = null
let pointerId: number | null = null
let unmounted = false

const busy = computed(() => previewing.value || submitting.value || job.value?.status === 'pending' || job.value?.status === 'running')
const selectedRegion = computed(() => regions.value[activeRegion.value])
const originalFilename = computed(() => files.value.find(file => file.filename === filename.value)?.original_filename)
const sourceUrl = computed(() => view.value === 'result' ? job.value?.download_url || '' : info.value?.preview_url || '')
let mediaRecoveryUsed = false
watch(sourceUrl, () => { mediaRecoveryUsed = false })
async function handleVideoError() {
  const source = sourceUrl.value
  const element = video.value
  if (!source || !element) return
  if (mediaRecoveryUsed) { videoError.value = '视频恢复失败，请重新选择文件。'; return }
  mediaRecoveryUsed = true
  const position = currentTime.value
  const diagnostic = new AbortController()
  const timer = window.setTimeout(() => diagnostic.abort(), 12000)
  try {
    const result = await probeMedia(source, diagnostic.signal)
    if (unmounted || source !== sourceUrl.value) return
    if (result.recovered) {
      element.addEventListener('loadedmetadata', () => { element.currentTime = position }, { once: true })
      element.load()
    } else videoError.value = mediaFailureMessage(result.status, element.error?.code)
  } catch {
    if (!unmounted && source === sourceUrl.value) videoError.value = '视频连接中断，请检查网络后重试。'
  } finally { window.clearTimeout(timer) }
}
const canEdit = computed(() => Boolean(info.value && !busy.value && view.value === 'original'))
const canProcess = computed(() => Boolean(info.value && !busy.value && regions.value.length && view.value !== 'result' && (method.value === 'delogo' || lamaAvailable.value)))
const stageStyle = computed(() => info.value ? {
  aspectRatio: `${info.value.width} / ${info.value.height}`,
  width: `min(100%, ${440 * info.value.width / info.value.height}px)`,
} : {})

class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(typeof payload?.detail === 'string' ? payload.detail : '请求失败，请稍后重试。', response.status)
  if (payload === null) throw new Error('服务返回了无效响应，请稍后重试。')
  return payload
}

function stopPolling() {
  window.clearTimeout(pollTimer)
  pollTimer = undefined
}

function clearRepairPreview() {
  if (previewImage.value) URL.revokeObjectURL(previewImage.value)
  previewImage.value = ''
  estimatedMinutes.value = 0
  if (view.value === 'preview') view.value = 'original'
}

async function loadCapabilities() {
  try {
    const capabilities = await api<{ lama: { available: boolean; message: string; device: string } }>('/api/watermarks/capabilities')
    lamaAvailable.value = capabilities.lama.available
    modelMessage.value = capabilities.lama.message
    processingDevice.value = capabilities.lama.device === '等待初始化' ? '' : capabilities.lama.device
  } catch { modelMessage.value = '修复模型状态读取失败' }
}

async function refreshFiles(preferred?: string | null) {
  void loadCapabilities()
  loadingFiles.value = true
  error.value = ''
  try {
    files.value = await api('/api/watermarks/videos')
    if (!busy.value) {
      const desired = preferred || filename.value
      filename.value = files.value.some(file => file.filename === desired) ? desired : files.value[0]?.original_filename || files.value[0]?.filename || ''
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '文件列表加载失败。'
  } finally {
    loadingFiles.value = false
  }
}

watch(filename, async value => {
  const request = ++infoRequest
  info.value = null
  regions.value = []
  activeRegion.value = -1
  job.value = null
  view.value = 'original'
  error.value = ''
  savingMessage.value = ''
  stopPolling()
  clearRepairPreview()
  if (!value) return
  loadingInfo.value = true
  try {
    const result = await api<VideoInfo>(`/api/watermarks/videos/${encodeURIComponent(value)}/info`)
    if (request === infoRequest) info.value = result
  } catch (reason) {
    if (request === infoRequest) error.value = reason instanceof Error ? reason.message : '视频信息加载失败。'
  } finally {
    if (request === infoRequest) loadingInfo.value = false
  }
})

watch(() => props.preferredFilename, value => { if (value) void refreshFiles(value) })
watch([regions, method], clearRepairPreview, { deep: true })
watch(sourceUrl, () => {
  ready.value = false
  playing.value = false
  currentTime.value = 0
  videoError.value = ''
  draft.value = null
  startPoint = null
  pointerId = null
})

function boxStyle(region: WatermarkRegion) {
  if (!info.value) return {}
  return {
    left: `${region.x / info.value.width * 100}%`, top: `${region.y / info.value.height * 100}%`,
    width: `${region.width / info.value.width * 100}%`, height: `${region.height / info.value.height * 100}%`,
  }
}

function pointerPosition(event: PointerEvent): Point {
  const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect()
  return { x: (event.clientX - bounds.left) / bounds.width * info.value!.width, y: (event.clientY - bounds.top) / bounds.height * info.value!.height }
}

function beginRegion(event: PointerEvent) {
  if (!canEdit.value || !ready.value || event.button !== 0 || pointerId !== null) return
  const index = (event.target as HTMLElement).closest<HTMLElement>('[data-region]')?.dataset.region
  if (index !== undefined) { activeRegion.value = Number(index); return }
  if (regions.value.length >= 8) return
  video.value?.pause()
  startPoint = pointerPosition(event)
  pointerId = event.pointerId
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}

function moveRegion(event: PointerEvent) {
  if (!startPoint || pointerId !== event.pointerId || !info.value) return
  draft.value = regionFromPoints(startPoint, pointerPosition(event), info.value.width, info.value.height)
}

function finishRegion(event: PointerEvent) {
  if (pointerId !== event.pointerId) return
  moveRegion(event)
  if (draft.value && canEdit.value) {
    regions.value.push(draft.value)
    activeRegion.value = regions.value.length - 1
  }
  cancelRegion()
}

function cancelRegion() { startPoint = null; pointerId = null; draft.value = null }

function addRegion() {
  if (!canEdit.value || !info.value || regions.value.length >= 8) return
  const { width, height } = info.value
  regions.value.push(clampRegion({ x: width * 0.72, y: height * 0.06, width: width * 0.2, height: height * 0.12 }, width, height))
  activeRegion.value = regions.value.length - 1
}

function updateRegion(key: keyof WatermarkRegion, event: Event) {
  if (!selectedRegion.value || !info.value) return
  const value = Number((event.target as HTMLInputElement).value)
  if (!Number.isFinite(value)) return
  regions.value[activeRegion.value] = clampRegion({ ...selectedRegion.value, [key]: value }, info.value.width, info.value.height)
  ;(event.target as HTMLInputElement).value = String(regions.value[activeRegion.value]![key])
}

function removeRegion(index: number) {
  regions.value.splice(index, 1)
  activeRegion.value = Math.min(activeRegion.value, regions.value.length - 1)
}

async function pollJob() {
  if (!job.value || unmounted) return
  const taskId = job.value.task_id
  try {
    const result = await api<Job>(`/api/watermarks/tasks/${taskId}`)
    if (unmounted || job.value?.task_id !== taskId || job.value.status === 'cancelled') return
    job.value = result
    processingDevice.value = result.processing_device || processingDevice.value
    error.value = ''
    if (job.value.status === 'pending' || job.value.status === 'running') {
      pollTimer = window.setTimeout(pollJob, 1000)
    } else if (job.value.status === 'finished') {
      view.value = 'result'
      void refreshFiles()
    }
  } catch (reason) {
    if (unmounted || job.value?.task_id !== taskId || job.value.status === 'cancelled') return
    if (reason instanceof ApiError && reason.status === 404) {
      job.value = { ...job.value, status: 'failed', message: '任务已失效', error: reason.message }
      return
    }
    error.value = reason instanceof Error ? reason.message : '任务状态查询失败。'
    if (!unmounted) pollTimer = window.setTimeout(pollJob, 3000)
  }
}

async function processVideo() {
  if (!canProcess.value) return
  submitting.value = true
  error.value = ''
  savingMessage.value = ''
  video.value?.pause()
  stopPolling()
  try {
    job.value = await api<Job>('/api/watermarks/tasks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: filename.value, regions: regions.value, method: method.value }),
    })
    void pollJob()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '去水印任务创建失败。'
  } finally { submitting.value = false }
}

async function previewRepair() {
  if (!canProcess.value) return
  video.value?.pause()
  previewing.value = true
  error.value = ''
  try {
    const response = await fetch('/api/watermarks/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: filename.value, regions: regions.value, method: method.value, time: currentTime.value }),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => null)
      throw new Error(payload?.detail || '修复预览失败。')
    }
    const image = await response.blob()
    if (unmounted) return
    clearRepairPreview()
    previewImage.value = URL.createObjectURL(image)
    processingDevice.value = response.headers.get('X-Processing-Device') || ''
    const seconds = Number(response.headers.get('X-Processing-Seconds'))
    if (info.value && seconds > 0) estimatedMinutes.value = Math.max(1, Math.ceil(seconds * info.value.duration * info.value.fps / 60))
    view.value = 'preview'
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '修复预览失败。' }
  finally { previewing.value = false }
}

async function cancelJob() {
  if (!job.value) return
  try {
    job.value = await api<Job>(`/api/watermarks/tasks/${job.value.task_id}`, { method: 'DELETE' })
    stopPolling()
  } catch (reason) { error.value = reason instanceof Error ? reason.message : '取消失败。' }
}

async function saveResult() {
  if (!job.value?.download_url || !job.value.filename) return
  saving.value = true
  error.value = ''
  try {
    savingMessage.value = saveMessage(await saveRemoteFile(job.value.download_url, job.value.filename))
  } catch (reason) {
    if (!(reason instanceof DOMException && reason.name === 'AbortError')) error.value = reason instanceof Error ? reason.message : '保存失败。'
  } finally { saving.value = false }
}

async function togglePlayback() {
  if (!video.value) return
  if (playing.value) video.value.pause()
  else { try { await video.value.play() } catch { videoError.value = '视频无法播放，请确认文件格式受浏览器支持。' } }
}

function seek(event: Event) { if (video.value) video.value.currentTime = Number((event.target as HTMLInputElement).value) }
function clock(value: number) { return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, '0')}` }
function durationLabel(seconds: number) {
  const value = Math.max(0, Math.ceil(seconds))
  if (value < 60) return `${value} 秒`
  const minutes = Math.ceil(value / 60)
  if (minutes < 60) return `${minutes} 分钟`
  return `${Math.floor(minutes / 60)} 小时${minutes % 60 ? ` ${minutes % 60} 分钟` : ''}`
}
function progressLabel(progress: number) {
  if (progress === 0 || progress === 100) return String(progress)
  return progress.toFixed(progress < 1 ? 2 : 1)
}

onMounted(() => { void refreshFiles(props.preferredFilename) })
onBeforeUnmount(() => { unmounted = true; ++infoRequest; stopPolling(); clearRepairPreview() })
</script>

<template>
  <section id="watermark" class="wm-root" aria-labelledby="wm-title">
    <div class="wm-heading">
      <h2 id="wm-title"><Eraser :size="22" aria-hidden="true" /> 视频去水印</h2>
      <span v-if="info" class="wm-dimensions">{{ info.width }} × {{ info.height }} · {{ clock(info.duration) }}</span>
    </div>
    <div class="wm-file-row">
      <label for="wm-file">已下载视频</label>
      <select id="wm-file" v-model="filename" :disabled="busy || loadingFiles" :title="filename">
        <option value="" disabled>{{ files.length ? '选择视频' : '暂无已下载视频' }}</option>
        <option v-for="file in files" :key="file.filename" :value="file.filename">{{ file.filename }}</option>
      </select>
      <button class="wm-icon" type="button" title="刷新视频列表" aria-label="刷新视频列表" :disabled="busy || loadingFiles" @click="refreshFiles()"><RefreshCw :size="18" :class="{ 'wm-spin': loadingFiles }" /></button>
    </div>
    <div v-if="originalFilename" class="wm-original-row"><span>已处理文件</span><button type="button" :disabled="busy" @click="filename = originalFilename || filename"><Undo2 :size="16" />使用原视频</button></div>
    <p v-if="error" class="wm-error" role="alert">{{ error }}</p>
    <p v-if="savingMessage" class="wm-success" role="status">{{ savingMessage }}</p>
    <div v-if="loadingInfo" class="wm-empty" role="status">正在读取视频</div>
    <div v-else-if="info" class="wm-layout">
      <div class="wm-preview">
        <div class="wm-tabs" role="group" aria-label="预览版本">
          <button type="button" :aria-pressed="view === 'original'" :disabled="previewing" @click="view = 'original'">原视频</button>
          <button type="button" :aria-pressed="view === 'preview'" :disabled="!previewImage || previewing" @click="view = 'preview'">修复预览</button>
          <button type="button" :aria-pressed="view === 'result'" :disabled="job?.status !== 'finished' || previewing" @click="view = 'result'">处理结果</button>
        </div>
        <div class="wm-canvas">
          <div class="wm-stage" :style="stageStyle">
            <img v-if="view === 'preview' && previewImage" :src="previewImage" alt="当前帧修复预览" />
            <video v-else ref="video" :src="sourceUrl" playsinline preload="auto" @loadeddata="ready = true" @play="playing = true" @pause="playing = false" @ended="playing = false" @timeupdate="currentTime = video?.currentTime || 0" @error="handleVideoError" />
            <div v-if="view === 'original'" class="wm-selection" :class="{ 'wm-editable': canEdit && ready }" aria-label="水印区域选择" @pointerdown="beginRegion" @pointermove="moveRegion" @pointerup="finishRegion" @pointercancel="cancelRegion" @lostpointercapture="cancelRegion">
              <div v-for="(region, index) in regions" :key="index" class="wm-box" :class="{ 'wm-box-active': activeRegion === index }" :data-region="index" :style="boxStyle(region)"><span>{{ index + 1 }}</span></div>
              <div v-if="draft" class="wm-box wm-draft" :style="boxStyle(draft)"></div>
            </div>
          </div>
        </div>
        <div class="wm-playback">
          <button class="wm-icon" type="button" :title="playing ? '暂停' : '播放'" :aria-label="playing ? '暂停' : '播放'" :disabled="!ready || view === 'preview' || previewing" @click="togglePlayback"><Pause v-if="playing" :size="18" /><Play v-else :size="18" /></button>
          <input type="range" min="0" :max="info.duration" step="0.1" :value="currentTime" :disabled="!ready || view === 'preview' || previewing" aria-label="视频进度" @input="seek" />
          <time>{{ clock(currentTime) }} / {{ clock(info.duration) }}</time>
        </div>
        <p v-if="videoError" class="wm-error" role="alert">{{ videoError }}</p>
      </div>
      <div class="wm-settings">
        <div class="wm-method" role="group" aria-label="去水印方式">
          <button type="button" :aria-pressed="method === 'lama'" :disabled="busy" @click="method = 'lama'">AI 修复</button>
          <button type="button" :aria-pressed="method === 'delogo'" :disabled="busy" @click="method = 'delogo'">快速填补</button>
        </div>
        <p v-if="method === 'lama' && !lamaAvailable" class="wm-error" role="status">{{ modelMessage }}</p>
        <p v-if="method === 'lama' && processingDevice" class="wm-runtime">{{ processingDevice }}</p>
        <div class="wm-region-heading"><h3>水印区域 <span>{{ regions.length }}/8</span></h3><button class="wm-icon" type="button" title="添加水印区域" aria-label="添加水印区域" :disabled="!canEdit || regions.length >= 8" @click="addRegion"><Crosshair :size="18" /></button></div>
        <div class="wm-regions" aria-label="已选水印区域">
          <p v-if="!regions.length" class="wm-muted">未选择区域</p>
          <div v-for="(region, index) in regions" :key="index" class="wm-region-row" :class="{ 'wm-region-active': activeRegion === index }">
            <button type="button" :disabled="!canEdit" @click="activeRegion = index"><span>区域 {{ index + 1 }}</span><small>{{ region.width }} × {{ region.height }}</small></button>
            <button class="wm-icon" type="button" :title="`删除区域 ${index + 1}`" :aria-label="`删除区域 ${index + 1}`" :disabled="!canEdit" @click="removeRegion(index)"><Trash2 :size="16" /></button>
          </div>
        </div>
        <div v-if="selectedRegion" class="wm-coordinates">
          <label v-for="field in [{ key: 'x', label: 'X' }, { key: 'y', label: 'Y' }, { key: 'width', label: '宽度' }, { key: 'height', label: '高度' }]" :key="field.key">
            <span>{{ field.label }} <small>px</small></span><input type="number" step="1" :min="field.key === 'x' || field.key === 'y' ? 1 : 4" :max="field.key === 'x' || field.key === 'width' ? info.width - 2 : info.height - 2" :value="selectedRegion[field.key as keyof WatermarkRegion]" :disabled="!canEdit" @change="updateRegion(field.key as keyof WatermarkRegion, $event)" />
          </label>
        </div>
        <button class="wm-preview-action" type="button" :disabled="!canProcess" @click="previewRepair"><ScanEye :size="18" />{{ previewing ? '正在生成预览' : '预览当前帧' }}</button>
        <p v-if="estimatedMinutes" class="wm-estimate">整段修复预计约 {{ estimatedMinutes }} 分钟</p>
        <button class="wm-process" type="button" :disabled="!canProcess" @click="processVideo"><Eraser :size="18" />{{ busy && !previewing ? '正在处理' : '处理整段视频' }}</button>
        <div v-if="job" class="wm-job" aria-live="polite">
          <div class="wm-job-head"><span>{{ job.message }}</span><strong>{{ progressLabel(job.progress) }}%</strong></div>
          <progress :value="job.progress" max="100" aria-label="去水印进度"></progress>
          <div v-if="job.status !== 'pending' && job.total_frames" class="wm-job-details">
            <span>{{ job.processing_device || '初始化' }} · {{ job.frames_processed || 0 }} / {{ job.total_frames }} 帧</span>
            <span>已用 {{ durationLabel(job.elapsed_seconds || 0) }}<template v-if="job.status === 'running' && job.remaining_seconds != null"> · 预计剩余 {{ durationLabel(job.remaining_seconds) }}</template></span>
          </div>
          <p v-if="job.error" class="wm-error">{{ job.error }}</p>
          <button v-if="busy && !submitting" class="wm-command" type="button" @click="cancelJob"><Square :size="15" />取消处理</button>
          <button v-if="job.status === 'finished'" class="wm-save" type="button" :disabled="saving" @click="saveResult"><Download :size="18" />{{ saving ? '保存中' : '另存为处理结果' }}</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.wm-root { padding: 28px 0 34px; border-top: 1px solid #dce1e6; scroll-margin-top: 76px; color: #242b33; }
.wm-heading, .wm-file-row, .wm-region-heading, .wm-job-head { display: flex; align-items: center; gap: 12px; }
.wm-heading { justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; }
.wm-heading h2 { display: flex; align-items: center; gap: 9px; font-size: 22px; line-height: 1.4; }
.wm-heading h2 svg { color: #167a65; }
.wm-dimensions, .wm-muted { color: #71777f; font-size: 13px; }
.wm-file-row { margin-bottom: 18px; }
.wm-file-row label { flex-shrink: 0; font-size: 14px; font-weight: 600; }
.wm-original-row { display: flex; gap: 12px; align-items: center; margin-bottom: 14px; font-size: 13px; color: #71777f; }
.wm-original-row button { padding: 0 10px; }
.wm-file-row select { width: 100%; min-width: 0; height: 40px; padding: 0 10px; border: 1px solid #cfd5db; border-radius: 6px; background: #fff; color: #30373f; text-overflow: ellipsis; }
.wm-root button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 38px; border: 1px solid #cfd5db; border-radius: 6px; background: #fff; color: #30373f; cursor: pointer; font-size: 14px; }
.wm-root button:hover:not(:disabled) { background: #eef4f1; border-color: #91afa5; }
.wm-root button:focus-visible, .wm-root input:focus-visible, .wm-root select:focus-visible { outline: 2px solid #167a65; outline-offset: 2px; }
.wm-root .wm-icon { flex: 0 0 38px; width: 38px; height: 38px; padding: 0; }
.wm-root svg { flex-shrink: 0; }
.wm-layout { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 26px; align-items: start; }
.wm-preview, .wm-settings { min-width: 0; }
.wm-tabs { display: inline-flex; gap: 2px; margin-bottom: 12px; padding: 3px; background: #eff1f3; border-radius: 6px; }
.wm-tabs button { min-height: 32px; padding: 0 14px; border-color: transparent; background: transparent; }
.wm-tabs button[aria-pressed='true'] { background: #fff; box-shadow: 0 1px 3px #00000016; }
.wm-canvas { display: flex; justify-content: center; min-height: 160px; background: #202224; }
.wm-stage { position: relative; flex-shrink: 0; max-width: 100%; }
.wm-stage video, .wm-stage img { display: block; width: 100%; height: 100%; object-fit: fill; }
.wm-selection { position: absolute; inset: 0; touch-action: none; overflow: hidden; }
.wm-editable { cursor: crosshair; }
.wm-box { position: absolute; border: 2px solid #f0b54b; background: #f0b54b20; }
.wm-box-active, .wm-draft { border-color: #5ee8ba; background: #5ee8ba25; }
.wm-box span { display: grid; place-items: center; position: absolute; top: 0; left: 0; width: 20px; height: 20px; background: #f0b54b; color: #14231e; font-size: 12px; font-weight: 700; pointer-events: none; }
.wm-box-active span { background: #5ee8ba; }
.wm-playback { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.wm-playback input { flex: 1; min-width: 0; accent-color: #167a65; }
.wm-playback time { min-width: 100px; text-align: right; font-size: 12px; font-variant-numeric: tabular-nums; }
.wm-region-heading { justify-content: space-between; min-height: 42px; padding-bottom: 10px; border-bottom: 1px solid #e0e4e8; }
.wm-region-heading h3 { font-size: 15px; }
.wm-region-heading h3 span { margin-left: 8px; color: #71777f; font-size: 12px; font-weight: 400; }
.wm-regions { margin-top: 10px; }
.wm-regions > p { padding: 12px 0; }
.wm-region-row { display: flex; align-items: center; gap: 6px; padding: 4px; border-bottom: 1px solid #e9ecef; }
.wm-region-active { background: #edf6f2; }
.wm-region-row > button:first-child { display: flex; flex: 1; justify-content: space-between; gap: 6px; min-width: 0; padding: 0 5px; border: 0; background: transparent; }
.wm-region-row small { color: #71777f; font-size: 12px; }
.wm-region-row .wm-icon { border: 0; background: transparent; }
.wm-coordinates { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }
.wm-coordinates label { min-width: 0; font-size: 13px; }
.wm-coordinates span { display: flex; justify-content: space-between; margin-bottom: 5px; }
.wm-coordinates small { color: #71777f; }
.wm-coordinates input { width: 100%; min-width: 0; height: 36px; padding: 0 9px; color: #30373f; border: 1px solid #cfd5db; border-radius: 5px; background: #fff; }
.wm-root .wm-process, .wm-root .wm-save { width: 100%; padding: 8px 12px; background: #167a65; color: #fff; border-color: #167a65; }
.wm-root .wm-process { margin-top: 20px; }
.wm-method { display: flex; gap: 3px; margin-bottom: 14px; padding: 3px; background: #eff1f3; border-radius: 6px; }
.wm-method button { flex: 1; border-color: transparent; background: transparent; }
.wm-method button[aria-pressed='true'] { background: #fff; color: #167a65; border-color: #dce5e0; }
.wm-root .wm-preview-action { margin-top: 18px; width: 100%; }
.wm-root .wm-preview-action + .wm-process { margin-top: 10px; }
.wm-estimate, .wm-runtime { margin-top: 9px; color: #71777f; font-size: 12px; line-height: 1.5; }
.wm-runtime { margin-bottom: 12px; }
.wm-root .wm-process:hover:not(:disabled), .wm-root .wm-save:hover:not(:disabled) { background: #116351; }
.wm-job { margin-top: 18px; padding-top: 16px; border-top: 1px solid #e0e4e8; }
.wm-job-head { justify-content: space-between; font-size: 13px; }
.wm-job-head strong { flex-shrink: 0; font-variant-numeric: tabular-nums; }
.wm-job-details { display: grid; gap: 5px; margin-bottom: 14px; color: #71777f; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.wm-job progress { display: block; width: 100%; height: 8px; margin: 10px 0 14px; appearance: none; border: 0; border-radius: 3px; overflow: hidden; }
.wm-job progress::-webkit-progress-bar { background: #e4e9e6; }
.wm-job progress::-webkit-progress-value { background: #167a65; }
.wm-root .wm-command { padding: 0 12px; }
.wm-error, .wm-success { margin: 12px 0; font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.wm-error { color: #b43a30; }
.wm-success { color: #167a65; }
.wm-empty { padding: 38px 0; color: #71777f; font-size: 14px; }
.wm-spin { animation: wm-spin 1s linear infinite; }
@keyframes wm-spin { to { transform: rotate(360deg); } }
@media (max-width: 760px) { .wm-layout { grid-template-columns: minmax(0, 1fr); gap: 20px; } .wm-settings { border-top: 1px solid #e0e4e8; padding-top: 14px; } .wm-file-row { gap: 8px; flex-wrap: wrap; } .wm-file-row label { width: 100%; } .wm-file-row select { flex: 1; width: 0; } .wm-canvas { min-height: 100px; } }
</style>
