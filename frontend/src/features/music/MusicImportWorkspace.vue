<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MusicListControls from './components/MusicListControls.vue'
import { useMusicList } from './useMusicList'
import { Download, Link, ImagePlus, ListMusic, LoaderCircle, Pause, Play, Plus, RefreshCw, Search, Square, Trash2, TriangleAlert, X } from 'lucide-vue-next'
import { createMusicBatch } from './musicBatch'
import { isMusicDownloaded } from './musicDownloads'
import { deviceTracks, hasDeviceFile, refreshDeviceLibrary } from './deviceLibrary'
import { account } from '../accounts/auth'
import { duration, libraryRevision, providers, trackKey, trackPayload, playlists, refreshLibrary, type MusicTrack } from './musicLibrary'
import { api, jsonRequest } from '../../shared/api/client'
import { isPlaybackTrack, playbackState, playTrack, togglePlayback } from './player/musicPlayer'

type Candidate = MusicTrack & { candidate_id: string; exact: boolean; score: number }
type Row = {
  row_id: string; title: string; artist: string; original: string; uncertain: boolean
  status: 'draft' | 'matched' | 'check' | 'not_found' | 'error'
  candidates: Candidate[]; selected_id: string | null; error: string | null; checked?: boolean; dirty?: boolean
}
type Job = {
  timing?: { total_seconds: number; read_seconds?: number; search_seconds?: number; queue_seconds?: number; recognize_seconds?: number; round_seconds?: number; active_since?: number | null; phase?: string | null }
  input_kind?: string; source?: string; range_required?: boolean; partial?: boolean; searched?: number; search_total?: number
  playlist?: { name: string; provider: string; total: number | null; read_count: number; complete: boolean; url: string; start: number; limit: number }
  import_id: string; status: 'reading' | 'draft' | 'recognizing' | 'searching' | 'finished' | 'failed' | 'cancelled'
  progress: number; rows: Row[]; duplicates: number; warnings: string[]; error: string | null
}
type Screenshot = { file: File; url: string }
const props = defineProps<{ active: boolean; downloadActive: boolean }>()
const emit = defineEmits<{ 'batch-created': [id: string] }>()
const mode = ref<'playlist' | 'screenshots' | 'text'>('playlist')
const shareText = ref('')
const explicitRange = ref(false)
const rangeStart = ref(1)
const rangeLimit = ref(500)
const page = ref(1)
const playlistName = ref('')
const destinationPlaylist = ref('')
const savedMessage = ref('')
const saveTarget = ref<{ importId: string; name: string; id: string } | null>(null)
const text = ref('')
const order = ref('title_artist')
const source = ref('all')
const format = ref<'original' | 'mp3' | 'm4a'>('mp3')
const screenshots = ref<Screenshot[]>([])
const imageInput = ref<HTMLInputElement | null>(null)
const dragActive = ref(false)
const job = ref<Job | null>(null)
const rows = ref<Row[]>([])
const busy = ref(false)
const downloading = ref(false)
const error = ref('')
const playbackError = ref('')
const skipDownloaded = ref(true)
const clock = ref(0)
let elapsedTimer: number | undefined
let receivedAt = 0
const filter = ref('all')
const listDirty = ref(false)
const storageKey = `videodream.music-import.${account.value?.id}`
let timer: number | undefined
let controller: AbortController | undefined
let revision = 0
let disposed = false

const processing = computed(() => ['reading', 'recognizing', 'searching'].includes(job.value?.status || ''))
const locked = computed(() => busy.value || processing.value || downloading.value)
const dirty = computed(() => listDirty.value || rows.value.some(row => row.dirty))
const canDownload = computed(() => job.value?.status === 'finished' && !dirty.value && !locked.value && !props.downloadActive)
const selected = computed(() => rows.value.filter(row => row.checked && candidate(row) && !(skipDownloaded.value && isMusicDownloaded(localFor(candidate(row)!) || candidate(row)!))))
const counts = computed(() => ({ matched: rows.value.filter(row => row.selected_id).length,
  pending: rows.value.filter(row => !row.selected_id && row.candidates.length).length,
  missing: rows.value.filter(row => ['not_found', 'error'].includes(row.status)).length }))
const { filters: listFilters, artists: listArtists, filtered: listRows, hasTime: listHasTime } = useMusicList(() => rows.value, () => `import:${job.value?.import_id || 'draft'}`)
const filteredRows = computed(() => listRows.value.filter(row => filter.value === 'all' || filter.value === 'pending' && !row.selected_id || filter.value === 'selected' && row.checked && row.selected_id))
const visibleRows = computed(() => filteredRows.value.slice((page.value - 1) * 50, page.value * 50))
watch([listFilters, filter], () => { page.value = 1 }, { deep: true })
watch(() => filteredRows.value.length, () => { page.value = Math.min(page.value, Math.max(1, Math.ceil(filteredRows.value.length / 50))) })
const pageCount = computed(() => Math.max(1, Math.ceil(filteredRows.value.length / 50)))
const rowLimit = computed(() => job.value?.input_kind === 'playlist' ? 500 : 100)
const checkedTracks = computed(() => rows.value.filter(row => row.checked && candidate(row)).map(row => candidate(row)!))
const canRetry = computed(() => rows.value.some(row => ['draft', 'error', 'not_found'].includes(row.status)))
const statusText: Record<Row['status'], string> = { draft: '待搜索', matched: '已匹配', check: '待确认', not_found: '未找到', error: '搜索失败' }
const heading = computed(() => ({ reading: '读取歌单中', recognizing: '截图识别中', searching: '批量搜索中', draft: '待搜索名单', finished: '匹配结果', failed: '导入失败', cancelled: '已取消' }[job.value?.status || 'draft']))

function candidate(row: Row) { return row.candidates.find(item => item.candidate_id === row.selected_id) }
function isRowPlaying(row: Row) {
  const track = candidate(row) || row.candidates[0]
  return Boolean(track && isPlaybackTrack(localFor(track) || track))
}
function localFor(track: MusicTrack) {
  return hasDeviceFile(track) ? deviceTracks.value.find(item => trackKey(item) === trackKey(track)) : undefined
}
async function loadLocal() {
  try { await refreshDeviceLibrary() } catch { /* Search remains usable without device storage. */ }
}
function onVisible() { if (!document.hidden && props.active) void loadLocal() }
function seconds(value: number) {
  const total = Math.max(0, Math.floor(value))
  return total < 60 ? `${total} 秒` : `${Math.floor(total / 60)} 分 ${total % 60} 秒`
}
const timingText = computed(() => {
  const t = job.value?.timing
  if (!t) return '耗时未记录'
  const delta = processing.value && t.active_since ? Math.max(0, (clock.value - receivedAt) / 1000) : 0
  const phaseSeconds = (phase: string) => Number(t[`${phase}_seconds` as keyof typeof t] || 0) + (t.phase === phase ? delta : 0)
  const parts = [`总耗时 ${seconds(t.total_seconds + delta)}`, `读取 ${seconds(phaseSeconds('read'))}`, `搜索 ${seconds(phaseSeconds('search'))}`]
  if (t.recognize_seconds) parts.push(`识别 ${seconds(phaseSeconds('recognize'))}`)
  if (t.queue_seconds) parts.push(`排队 ${seconds(phaseSeconds('queue'))}`)
  const done = job.value?.searched || 0, total = job.value?.search_total || 0
  if (job.value?.status === 'searching' && done >= 10 && total > done) {
    parts.push(`预计剩余 ${seconds(((t.round_seconds || 0) + delta) / done * (total - done))}（估算）`)
  }
  return parts.join(' · ')
})
function remember(id?: string) {
  try { if (id) localStorage.setItem(storageKey, id); else localStorage.removeItem(storageKey) } catch { /* Storage may be disabled. */ }
}
function saveDraft() {
  if (!job.value || processing.value) return
  try {
    localStorage.setItem(`${storageKey}.draft`, JSON.stringify({ id: job.value.import_id, listDirty: listDirty.value, rows: rows.value.map(row => ({
      row_id: row.row_id, title: row.title, artist: row.artist, original: row.original, uncertain: row.uncertain,
      selected_id: row.selected_id, checked: row.checked, dirty: row.dirty,
    })) }))
  } catch { /* The server retains the search results when local storage is full. */ }
}
function accept(data: Job, restore = false) {
  const finishedNow = data.status === 'finished' && job.value?.status !== 'finished'
  const changedJob = job.value?.import_id !== data.import_id
  listDirty.value = false
  receivedAt = performance.now()
  clock.value = receivedAt
  job.value = data
  if (changedJob) { page.value = 1; savedMessage.value = ''; saveTarget.value = null; playlistName.value = data.playlist?.name?.slice(0, 80) || '导入歌单' }
  if (data.playlist?.name && playlistName.value === '导入歌单') playlistName.value = data.playlist.name.slice(0, 80)
  if (data.source) source.value = data.source
  if (data.range_required) { explicitRange.value = true; shareText.value ||= data.playlist?.url || '' }
  const previousRows = new Map(rows.value.map(row => [row.row_id, row]))
  rows.value = data.rows.map(row => {
    const previous = previousRows.get(row.row_id)
    const keep = !changedJob && previous && !previous.dirty && row.status !== 'draft' && previous.status !== 'draft' && row.candidates.some(item => item.candidate_id === previous.selected_id)
    return { ...row, selected_id: keep ? previous.selected_id : row.selected_id, checked: keep ? previous.checked : Boolean(row.selected_id), dirty: false }
  })
  if (restore && !processing.value) {
    try {
      const saved = JSON.parse(localStorage.getItem(`${storageKey}.draft`) || 'null')
      if (saved?.id === data.import_id && Array.isArray(saved.rows)) {
        listDirty.value = Boolean(saved.listDirty)
        rows.value = saved.rows.map((draft: Partial<Row>) => {
          const base = data.rows.find(row => row.row_id === draft.row_id)
          return { ...draft, status: draft.dirty ? 'draft' : base?.status || 'draft', candidates: draft.dirty ? [] : base?.candidates || [],
            selected_id: draft.dirty ? null : draft.selected_id, error: base?.error || null } as Row
        })
      }
    } catch { /* Ignore malformed saved drafts. */ }
  }
  remember(data.import_id)
  if (finishedNow) void loadLocal()
}
function stopPolling() { window.clearTimeout(timer); timer = undefined; controller?.abort() }
async function poll(id: string, current = revision, restore = false) {
  controller = new AbortController()
  try {
    const data = await api<Job>(`/api/music/imports/${id}`, { signal: controller.signal })
    if (disposed || current !== revision) return
    accept(data, restore)
    error.value = ''
    if (processing.value) timer = window.setTimeout(() => { void poll(id, current) }, 1200)
  } catch (failure) {
    if (disposed || current !== revision || failure instanceof DOMException && failure.name === 'AbortError') return
    error.value = failure instanceof Error ? failure.message : '导入任务查询失败。'
    if (processing.value) timer = window.setTimeout(() => { void poll(id, current) }, 3000)
  }
}
function addImages(files: File[]) {
  if (locked.value) return
  error.value = ''
  for (const file of files) {
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) { error.value = '请选择 JPG、PNG 或 WebP 截图。'; continue }
    if (file.size > 10 * 1024 * 1024) { error.value = '单张截图不能超过 10 MB。'; continue }
    if (screenshots.value.length >= 8) { error.value = '一次最多选择 8 张截图。'; break }
    if (screenshots.value.reduce((sum, item) => sum + item.file.size, file.size) > 32 * 1024 * 1024) { error.value = '截图总大小不能超过 32 MB。'; break }
    screenshots.value.push({ file, url: URL.createObjectURL(file) })
  }
}
function chooseImages(event: Event) {
  const input = event.target as HTMLInputElement
  addImages(Array.from(input.files || []))
  input.value = ''
}
function removeImage(index: number) { const item = screenshots.value.splice(index, 1)[0]; if (item) URL.revokeObjectURL(item.url) }
function pasteImages(event: ClipboardEvent) {
  if (!props.active || mode.value !== 'screenshots' || locked.value) return
  const files = Array.from(event.clipboardData?.items || []).filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter((file): file is File => Boolean(file))
  if (files.length) { event.preventDefault(); addImages(files) }
}
function dropImages(event: DragEvent) { dragActive.value = false; addImages(Array.from(event.dataTransfer?.files || [])) }
async function importSongs() {
  if (locked.value) return
  busy.value = true
  error.value = ''
  stopPolling()
  const current = ++revision
  try {
    let data: Job
    if (mode.value === 'playlist') {
      data = await api<Job>('/api/music/imports/playlist', jsonRequest('POST', {
        share_text: shareText.value, source: source.value, auto_search: true,
        start: explicitRange.value ? rangeStart.value : 1, limit: explicitRange.value ? rangeLimit.value : 500, explicit_range: explicitRange.value,
      }))
    } else if (mode.value === 'text') {
      data = await api<Job>('/api/music/imports/text', jsonRequest('POST', { text: text.value, order: order.value }))
    } else {
      const form = new FormData()
      screenshots.value.forEach(item => form.append('files', item.file))
      data = await api<Job>('/api/music/imports/screenshots', { method: 'POST', body: form })
    }
    if (disposed || current !== revision) return
    accept(data)
    filter.value = 'all'
    if (processing.value) await poll(data.import_id, current)
  } catch (failure) { if (!disposed) error.value = failure instanceof Error ? failure.message : '歌曲导入失败。' }
  finally { busy.value = false }
}
function editRow(row: Row) { row.dirty = true; row.selected_id = null; row.candidates = []; row.status = 'draft'; row.checked = false; row.uncertain = false; row.error = null }
function addRow() {
  if (rows.value.length >= rowLimit.value) return
  listDirty.value = true
  rows.value.push({ row_id: crypto.randomUUID(), title: '', artist: '', original: '', uncertain: false,
    status: 'draft', candidates: [], selected_id: null, error: null, dirty: true, checked: false })
}
function chooseCandidate(row: Row) { row.checked = Boolean(row.selected_id); row.status = row.selected_id ? 'matched' : 'check' }
async function searchSongs(retryFailed = false) {
  if (!job.value || locked.value || !rows.value.length) return
  if (retryFailed && dirty.value) { error.value = '请先重新匹配修改后的名单。'; return }
  if (rows.value.some(row => !row.title.trim())) { error.value = '请补全歌曲名称或删除空行。'; return }
  busy.value = true
  error.value = ''
  stopPolling()
  const current = ++revision
  try {
    const data = await api<Job>(`/api/music/imports/${job.value.import_id}/search`, jsonRequest('POST', {
      ...(retryFailed ? { retry_failed: true } : { songs: rows.value.map(row => ({ row_id: row.row_id, title: row.title, artist: row.artist, original: row.original, uncertain: row.uncertain })) }), source: source.value,
    }))
    if (disposed || current !== revision) return
    accept(data)
    await poll(data.import_id, current)
  } catch (failure) { if (!disposed) error.value = failure instanceof Error ? failure.message : '批量搜索失败。' }
  finally { busy.value = false }
}
async function cancel() {
  if (!job.value || busy.value) return
  busy.value = true
  error.value = ''
  stopPolling()
  revision++
  try { accept(await api<Job>(`/api/music/imports/${job.value.import_id}/cancel`, { method: 'POST' })) }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '取消失败。'; if (processing.value) void poll(job.value.import_id) }
  finally { busy.value = false }
}
async function listen(row: Row) {
  const track = candidate(row) || row.candidates[0]
  if (!track) return
  playbackError.value = ''
  try {
    await loadLocal()
    const local = localFor(track)
    if (isPlaybackTrack(local || track)) togglePlayback()
    else await playTrack(local || track, rows.value.map(candidate).filter((item): item is Candidate => Boolean(item)))
  } catch (failure) { playbackError.value = failure instanceof Error ? failure.message : '歌曲播放失败。' }
}
async function download() {
  if (!job.value || !canDownload.value || !selected.value.length) return
  downloading.value = true
  error.value = ''
  try {
    await loadLocal()
    if (!selected.value.length) return
    const data = await createMusicBatch(selected.value.map(row => candidate(row)!), format.value)
    if (!disposed) emit('batch-created', data.batch_id)
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '批量下载创建失败。' }
  finally { downloading.value = false }
}
async function savePlaylist() {
  if (locked.value || dirty.value || !checkedTracks.value.length) return
  const selectedTracks = [...checkedTracks.value]
  const uid = account.value?.id
  busy.value = true; error.value = ''; savedMessage.value = ''
  let saved = 0
  try {
    let id = destinationPlaylist.value
    if (!id) {
      const name = playlistName.value.trim()
      if (!name) throw new Error('请输入歌单名称。')
      if (saveTarget.value?.importId === job.value?.import_id && saveTarget.value?.name === name) id = saveTarget.value.id
      else {
        const created = await api<{ id: string }>('/api/library/playlists', jsonRequest('POST', { name }))
        id = created.id
        saveTarget.value = { importId: job.value!.import_id, name, id }
        destinationPlaylist.value = id
      }
    }
    for (const track of selectedTracks) {
      if (disposed || uid !== account.value?.id) throw new Error('页面或账号已切换，保存已停止。')
      await api(`/api/library/playlists/${id}/tracks?bulk_import=true`, jsonRequest('POST', trackPayload(track)))
      saved++
      savedMessage.value = `已保存 ${saved} / ${selectedTracks.length} 首到本站歌单`
    }
    await refreshLibrary()
  } catch (failure) {
    error.value = `${failure instanceof Error ? failure.message : '保存失败。'} 已成功保存 ${saved} 首，可重试；已有歌曲不会重复添加。`
    await refreshLibrary()
  } finally { busy.value = false }
}
function selectAll(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  filteredRows.value.forEach(row => { if (candidate(row)) row.checked = checked })
}
watch(rows, saveDraft, { deep: true })
watch(filter, () => { page.value = 1 })
watch(pageCount, value => { page.value = Math.min(page.value, value) })
watch(() => props.active, active => { if (active) void loadLocal() })
watch(processing, active => {
  window.clearInterval(elapsedTimer)
  if (active) elapsedTimer = window.setInterval(() => { clock.value = performance.now() }, 1000)
})
watch(libraryRevision, () => { void loadLocal() })
onMounted(() => {
  document.addEventListener('paste', pasteImages)
  document.addEventListener('visibilitychange', onVisible)
  window.addEventListener('focus', onVisible)
  void loadLocal()
  try { const id = localStorage.getItem(storageKey); if (id && /^[a-f0-9]{12}$/.test(id)) void poll(id, revision, true) } catch { /* Optional resume. */ }
})
onBeforeUnmount(() => { saveDraft(); disposed = true; revision++; window.clearInterval(elapsedTimer); document.removeEventListener('visibilitychange', onVisible); window.removeEventListener('focus', onVisible); stopPolling(); document.removeEventListener('paste', pasteImages); screenshots.value.forEach(item => URL.revokeObjectURL(item.url)) })
</script>

<template>
  <div class="mi-workspace">
    <div class="mi-tabs" role="tablist" aria-label="名单来源">
      <button id="mi-playlist-tab" role="tab" :aria-selected="mode === 'playlist'" aria-controls="mi-input" :disabled="locked" @click="mode = 'playlist'"><Link :size="18" />歌单链接</button>
      <button id="mi-images-tab" role="tab" :aria-selected="mode === 'screenshots'" aria-controls="mi-input" :disabled="locked" @click="mode = 'screenshots'"><ImagePlus :size="18" />截图</button>
      <button id="mi-text-tab" role="tab" :aria-selected="mode === 'text'" aria-controls="mi-input" :disabled="locked" @click="mode = 'text'"><ListMusic :size="18" />多行文本</button>
    </div>
    <section id="mi-input" role="tabpanel" :aria-labelledby="mode === 'playlist' ? 'mi-playlist-tab' : mode === 'screenshots' ? 'mi-images-tab' : 'mi-text-tab'" class="mi-input">
      <template v-if="mode === 'playlist'">
        <textarea v-model="shareText" :disabled="locked" aria-label="歌单分享链接" rows="3" maxlength="4096" placeholder="粘贴 QQ 音乐、网易云音乐、酷狗音乐的公开歌单链接或整段分享文字" />
        <div class="mi-input-actions"><label>搜索平台<select v-model="source" :disabled="locked" aria-label="歌单搜索平台"><option value="all">全部平台</option><option value="qq">QQ 音乐</option><option value="netease">网易云音乐</option><option value="kugou">酷狗音乐</option></select></label><label><input v-model="explicitRange" type="checkbox" :disabled="locked" />选择导入范围</label><button class="mc-command primary" :disabled="locked || !shareText.trim()" @click="importSongs"><LoaderCircle v-if="busy || processing" :size="17" class="spin" /><Search v-else :size="17" />一键导入并搜索</button></div>
        <div v-if="explicitRange" class="mi-input-actions"><label>从第<input v-model.number="rangeStart" :disabled="locked" type="number" min="1" max="100000" aria-label="导入起始位置" />首开始</label><label>读取<input v-model.number="rangeLimit" :disabled="locked" type="number" min="1" max="500" aria-label="导入歌曲数量" />首</label></div>
        <p class="mi-warning">每次最多 500 首。只读取歌曲信息并搜索，不自动下载。酷狗支持含 specialid 的公开歌单链接，暂不支持仅含 gcid 的链接或口令。</p>
      </template>
      <template v-else-if="mode === 'screenshots'">
        <div class="mi-upload" :class="{ dragging: dragActive }" @dragover.prevent="dragActive = true" @dragleave.prevent="dragActive = false" @drop.prevent="dropImages">
          <button class="mc-command" :disabled="locked" @click="imageInput?.click()"><ImagePlus :size="18" />添加截图</button><span>{{ screenshots.length }} / 8 张</span>
          <div v-if="screenshots.length" class="mi-images"><figure v-for="(item, index) in screenshots" :key="item.url"><img :src="item.url" :alt="`歌单截图 ${index + 1}`" /><figcaption>{{ item.file.name }}</figcaption><button class="mc-icon" :disabled="locked" :title="`移除截图 ${index + 1}`" :aria-label="`移除截图 ${index + 1}`" @click="removeImage(index)"><X :size="16" /></button></figure></div>
        </div>
        <input ref="imageInput" hidden type="file" multiple accept="image/png,image/jpeg,image/webp" aria-label="选择歌单截图" @change="chooseImages" />
        <div class="mi-input-actions"><small>截图将发送至已配置的 AI 服务</small><button class="mc-command primary" :disabled="locked || !screenshots.length" @click="importSongs"><LoaderCircle v-if="busy || job?.status === 'recognizing'" :size="17" class="spin" /><Search v-else :size="17" />识别歌曲</button></div>
      </template>
      <template v-else>
        <textarea v-model="text" :disabled="locked" aria-label="待下载歌曲名单" rows="6" maxlength="30000" placeholder="晴天 - 周杰伦&#10;Hello - Adele" />
        <div class="mi-input-actions"><label>文本顺序<select v-model="order" :disabled="locked" aria-label="文本顺序"><option value="title_artist">歌名 - 歌手</option><option value="artist_title">歌手 - 歌名</option></select></label><button class="mc-command primary" :disabled="locked || !text.trim()" @click="importSongs"><LoaderCircle v-if="busy" :size="17" class="spin" /><ListMusic v-else :size="17" />导入名单</button></div>
      </template>
    </section>
    <p v-if="error || playbackError" class="mi-error" role="alert">{{ error || playbackError }}</p>
    <template v-if="job">
      <p v-if="job.playlist" class="mi-source"><strong>{{ job.playlist.name || '来源歌单' }}</strong> · {{ providers[job.playlist.provider] }} · 已读取 {{ job.playlist.read_count }} / {{ job.playlist.total ?? '未知' }} 首 · {{ job.playlist.complete ? '完整读取' : '未完整导入' }}</p>
      <header class="mi-heading"><h2>{{ heading }}</h2><span v-if="rows.length">{{ rows.length }} 首<span v-if="job.duplicates"> · 已合并 {{ job.duplicates }} 条重复记录</span></span><button v-if="processing" class="mc-command" :disabled="busy" @click="cancel"><Square :size="15" />取消</button></header>
      <p class="mi-timing">{{ timingText }}</p>
      <div v-if="processing" class="mi-progress" role="status"><progress :value="job.progress" max="100" :aria-label="heading" /><span>{{ job.status === 'searching' ? `${job.searched || 0} / ${job.search_total || rows.length}` : `${Math.round(job.progress)}%` }}</span></div>
      <p v-if="job.error" class="mi-error" role="alert">{{ job.error }}</p>
      <p v-for="warning in job.warnings" :key="warning" class="mi-warning" role="status">{{ warning }}</p>
      <template v-if="rows.length">
        <div class="mi-toolbar"><label>搜索平台<select v-model="source" :disabled="locked" aria-label="批量搜索平台"><option value="all">全部平台</option><option value="qq">QQ 音乐</option><option value="netease">网易云音乐</option><option value="kugou">酷狗音乐</option></select></label><button class="mc-command primary" :disabled="locked" @click="searchSongs(false)"><LoaderCircle v-if="job.status === 'searching'" :size="17" class="spin" /><RefreshCw v-else-if="job.status === 'finished'" :size="17" /><Search v-else :size="17" />{{ job.status === 'finished' ? '重新匹配' : '批量搜索' }}</button><button v-if="canRetry" class="mc-command" :disabled="locked || dirty" @click="searchSongs(true)">重试失败及未完成项</button><button class="mc-icon" :disabled="locked || rows.length >= rowLimit" title="添加歌曲" aria-label="添加歌曲" @click="addRow"><Plus :size="18" /></button></div>
        <div v-if="job.status === 'finished'" class="mi-results-summary"><span>已确认 {{ counts.matched }}</span><span>待确认 {{ counts.pending }}</span><span>未找到或失败 {{ counts.missing }}</span><select v-model="filter" aria-label="筛选匹配结果"><option value="all">全部歌曲</option><option value="pending">未确认</option><option value="selected">已勾选</option></select></div>
        <MusicListControls v-model="listFilters" :artists="listArtists" :total="rows.length" :count="filteredRows.length" :has-time="listHasTime" />
        <div class="mi-table" aria-label="待下载歌曲">
          <div class="mi-table-head"><input type="checkbox" :disabled="locked" :checked="filteredRows.some(row => row.selected_id) && filteredRows.filter(row => row.selected_id).every(row => row.checked)" aria-label="全选已确认歌曲" @change="selectAll" /><div class="mi-song"><span>歌名</span><span>歌手</span></div><span>状态</span><span>匹配歌曲</span><span /></div>
          <div v-for="(row, index) in visibleRows" :key="row.row_id" class="mi-row">
            <input v-model="row.checked" type="checkbox" :disabled="locked || !row.selected_id" :aria-label="`勾选 ${row.title}`" />
            <div class="mi-song" :title="row.original || undefined"><input v-model="row.title" :disabled="locked" :title="row.title" :aria-label="`第 ${index + 1} 首歌名`" maxlength="100" @input="editRow(row)" /><input v-model="row.artist" :disabled="locked" :title="row.artist" :aria-label="`第 ${index + 1} 首歌手`" maxlength="100" placeholder="歌手" @input="editRow(row)" /></div>
            <div class="mi-status" :class="row.status"><span>{{ row.selected_id ? '已确认' : statusText[row.status] }}</span><span v-if="row.uncertain" class="mi-status-detail" title="识别待核对" aria-label="识别待核对"><TriangleAlert :size="14" aria-hidden="true" /></span><small v-if="candidate(row) && localFor(candidate(row)!)">已下载</small></div>
            <div class="mi-match" :title="candidate(row) ? `${candidate(row)!.title} · ${candidate(row)!.artist || '未知歌手'} · ${providers[candidate(row)!.provider || '']} · ${duration(candidate(row)!.duration)}` : row.error || undefined"><select v-if="row.candidates.length" v-model="row.selected_id" :disabled="locked" :aria-label="`确认 ${row.title} 的匹配歌曲`" @change="chooseCandidate(row)"><option :value="null">请选择歌曲</option><option v-for="item in row.candidates" :key="item.candidate_id" :value="item.candidate_id">{{ item.title }} · {{ item.artist || '未知歌手' }} · {{ providers[item.provider || ''] }} · {{ duration(item.duration) }}</option></select><span v-else>{{ row.error || (row.status === 'draft' ? '—' : '暂无候选') }}</span></div>
            <div class="mi-row-actions"><button class="mc-icon" :disabled="!row.candidates.length" :title="`试听 ${row.title}`" :aria-label="`试听 ${row.title}`" @click="listen(row)"><LoaderCircle v-if="isRowPlaying(row) && playbackState === 'loading'" :size="17" class="spin" /><Pause v-else-if="isRowPlaying(row) && playbackState === 'playing'" :size="17" /><Play v-else :size="17" /></button><button class="mc-icon" :disabled="locked" :title="`删除 ${row.title}`" :aria-label="`删除 ${row.title}`" @click="rows = rows.filter(item => item !== row); listDirty = true"><Trash2 :size="17" /></button></div>
          </div>
        </div>
        <div v-if="pageCount > 1" class="mi-input-actions"><button class="mc-command" :disabled="page <= 1" @click="page--">上一页</button><span>第 {{ page }} / {{ pageCount }} 页 · 共 {{ filteredRows.length }} 首</span><button class="mc-command" :disabled="page >= pageCount" @click="page++">下一页</button></div>
        <div class="mi-input-actions"><label>保存到<select v-model="destinationPlaylist" :disabled="locked" aria-label="目标歌单"><option value="">新建歌单</option><option v-for="item in playlists" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><input v-if="!destinationPlaylist" v-model="playlistName" :disabled="locked" maxlength="80" aria-label="新歌单名称" placeholder="歌单名称" /><button class="mc-command" :disabled="locked || dirty || !checkedTracks.length" @click="savePlaylist">保存选中 {{ checkedTracks.length }} 首到歌单</button></div>
        <p v-if="savedMessage" role="status">{{ savedMessage }}</p>
        <p v-if="!visibleRows.length" class="mi-empty">暂无符合条件的歌曲</p>
        <footer class="mi-download"><label><input v-model="skipDownloaded" type="checkbox" :disabled="downloading" />跳过已下载</label><label>格式<select v-model="format" :disabled="downloading" aria-label="批量下载格式"><option value="mp3">MP3</option><option value="m4a">M4A</option></select></label><span>已选 {{ selected.length }} 首</span><button class="mc-command primary" :disabled="!canDownload || !selected.length" @click="download"><LoaderCircle v-if="downloading" :size="17" class="spin" /><Download v-else :size="17" />批量下载</button></footer>
      </template>
    </template>
  </div>
</template>

<style scoped>
.mi-workspace { color: var(--mc-ink); --mi-warning: #987129; --mi-field-bg: var(--music-surface, #fff); --mi-field-line: color-mix(in srgb, var(--music-line, #cfd9d2) 65%, transparent); }
html[data-music-theme="dark"] .mi-workspace { --mi-warning: #e8be63; }
html[data-music-background] .mi-workspace { --mi-field-bg: color-mix(in srgb, var(--music-surface, #fff) 88%, transparent); }
.mi-source { font-size: 13px; overflow-wrap: anywhere; line-height: 1.8; }
.mi-input-actions input[type="number"] { width: 90px; padding: 7px; }
.mi-tabs { display: flex; gap: 20px; border-bottom: 1px solid var(--music-line, #e3e8e2); }
.mi-tabs button { display: flex; align-items: center; gap: 8px; padding: 12px 2px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--mc-muted); font-size: 14px; }
.mi-tabs button[aria-selected="true"] { color: var(--music-highlight, var(--mc-green)); border-bottom-color: var(--mc-green); }
.mi-input { padding: 20px 0; border-bottom: 1px solid var(--music-line, #e3e8e2); }
.mi-upload { padding: 20px; border: 1px dashed var(--music-line, #b9c4be); border-radius: 6px; min-height: 100px; }
.mi-upload.dragging { border-color: var(--mc-green); background: #109b6d0c; }
.mi-upload > span { margin-left: 16px; font-size: 12px; color: var(--mc-muted); }
.mi-images { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 16px; margin-top: 20px; }
.mi-images figure { position: relative; margin: 0; min-width: 0; }
.mi-images img { display: block; width: 100%; height: 140px; object-fit: contain; background: var(--music-soft, #e9edeb); border-radius: 4px; }
.mi-images figcaption { margin-top: 8px; color: var(--mc-muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mi-images .mc-icon { position: absolute; top: 4px; right: 4px; background: #fff; color: #39443e; width: 26px; height: 26px; }
.mi-input textarea { display: block; width: 100%; min-height: 144px; resize: vertical; padding: 14px; font: inherit; font-size: 14px; line-height: 1.8; }
.mi-workspace input:not([type="checkbox"]), .mi-workspace select, .mi-input textarea { color: var(--mc-ink); border: 1px solid var(--music-line, #cfd9d2); border-radius: 4px; background: var(--music-surface, #ffffffaa); }
.mi-workspace input::placeholder, .mi-input textarea::placeholder { color: var(--mc-muted); opacity: .8; }
.mi-input textarea:focus-visible { outline: 2px solid var(--mc-green); outline-offset: 3px; }
.mi-workspace select { padding: 7px 9px; min-width: 0; max-width: 100%; font-size: 12px; color-scheme: light; }
html[data-music-theme="dark"] .mi-workspace select { color-scheme: dark; }
.mi-workspace input[type="checkbox"] { accent-color: var(--mc-green); width: 16px; height: 16px; margin: 0; flex-shrink: 0; }
.mi-input-actions, .mi-toolbar, .mi-download { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.mi-input-actions { justify-content: space-between; margin-top: 14px; }
.mi-workspace label { display: flex; align-items: center; gap: 9px; font-size: 12px; color: var(--mc-muted); }
.mi-input-actions small { color: var(--mc-muted); font-size: 11px; }
.mi-heading { display: flex; align-items: center; gap: 14px; padding-top: 24px; margin-bottom: 18px; flex-wrap: wrap; }
.mi-heading h2 { font-size: 18px; font-weight: 600; margin: 0; }
.mi-heading > span { color: var(--mc-muted); font-size: 12px; }
.mi-heading > button { margin-left: auto; }
.mi-progress { display: flex; align-items: center; gap: 12px; font-size: 12px; }
.mi-progress progress { width: 100%; height: 7px; accent-color: var(--mc-green); }
.mi-error, .mi-warning { margin: 14px 0; font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.mi-error { color: #cc5060; }
.mi-warning { color: var(--mi-warning); }
.mi-toolbar { margin: 18px 0; }
.mi-results-summary { display: flex; align-items: center; gap: 16px; font-size: 12px; color: var(--mc-muted); margin: 18px 0; flex-wrap: wrap; }
.mi-results-summary select { margin-left: auto; }
.mi-table { max-width: 100%; overflow-x: auto; }
.mi-table-head, .mi-row { display: grid; grid-template-columns: 20px minmax(280px, 1.2fr) 106px minmax(200px, 1fr) 68px; min-width: 740px; gap: 12px; align-items: center; }
.mi-table-head { padding: 10px 8px; font-size: 11px; color: var(--mc-muted); border-bottom: 1px solid var(--music-line, #cfd9d2); }
.mi-table-head .mi-song span { padding-inline: 8px; }
.mi-row { min-height: 52px; padding: 8px; border-bottom: 1px solid var(--music-line, #e3e8e2); }
.mi-song { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr); gap: 8px; align-items: center; min-width: 0; }
.mi-workspace .mi-song input, .mi-workspace .mi-match select { height: 34px; border-radius: 6px; border-color: var(--mi-field-line); background: transparent; transition: background-color .15s, border-color .15s; }
.mi-song input { width: 100%; min-width: 0; font-size: 13px; font-weight: 500; padding: 6px 9px; text-overflow: ellipsis; }
.mi-song input:nth-child(2) { color: var(--mc-muted); font-weight: 400; }
.mi-workspace .mi-song input:hover:not(:disabled), .mi-workspace .mi-match select:hover:not(:disabled) { background: transparent; border-color: color-mix(in srgb, var(--music-accent, var(--mc-green)) 35%, var(--music-line, #cfd9d2)); }
.mi-workspace .mi-song input:focus, .mi-workspace .mi-match select:focus { background: transparent; border-color: var(--music-accent, var(--mc-green)); outline: 2px solid color-mix(in srgb, var(--music-accent, var(--mc-green)) 25%, transparent); outline-offset: 1px; }
.mi-timing { color: var(--mc-muted); font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; }
.mi-match option { background: var(--music-surface, #fff); color: var(--mc-ink); }
.mi-match { min-width: 0; }
.mi-match select { width: 100%; text-overflow: ellipsis; }
.mi-match > span { display: block; font-size: 12px; color: var(--mc-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mi-status { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--mc-muted); white-space: nowrap; }
.mi-status.matched { color: var(--music-highlight, var(--mc-green)); }
.mi-status.check { color: var(--mi-warning); }
.mi-status.error { color: #cc5060; }
.mi-status small { color: var(--mc-muted); font-size: 10px; }
.mi-status-detail { display: inline-flex; color: var(--mi-warning); }
.mi-row-actions { display: flex; }
.mi-download { padding: 20px 0; font-size: 12px; color: var(--mc-muted); }
.mi-download > span { margin-left: auto; }
.mi-empty { color: var(--mc-muted); font-size: 13px; padding: 20px 0; }
@media (max-width: 700px) {
  .mi-download > span { margin-left: 0; }
  .mi-download > button { margin-left: auto; }
  .mi-heading { gap: 10px; }
  .mi-upload { padding: 14px; }
}
</style>
