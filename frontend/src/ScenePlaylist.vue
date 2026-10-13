<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { AlignLeft, Download, ChevronDown, ChevronLeft, ChevronRight, CircleStop, History, LoaderCircle, Play, RefreshCw, Save, Settings2, Sparkles, Trash2, X } from 'lucide-vue-next'
import TrackCover from './TrackCover.vue'
import { api, duration, jsonRequest, playlists, providers, refreshLibrary, type MusicTrack } from './musicLibrary'
import { playTrack, currentTrack, elapsed, seek } from './musicPlayer'
import LyricsView from './LyricsView.vue'
import { createMusicBatch } from './musicBatch'

type FeatureEvidence = { source?: string; confidence?: number; coverage?: string }
type RecommendedTrack = MusicTrack & { key: string; reason: string; source: { label: string }; unknown_features: string[];
  match?: { score: number | null; scoring_version?: string; breakdown?: Record<string, number> };
  validation: { checked_at: number; reason: string; full: boolean; duration?: number }; analysis: { features: Record<string, unknown>;
    evidence: { fields?: Record<string, FeatureEvidence>; lyrics?: FeatureEvidence; audio?: FeatureEvidence } } }
type Run = { run_id: string; status: string; request: { need: string; preferences: Record<string, unknown>; parsed?: Record<string, any> };
  result_version: number; saved_playlist_id: string | null; saved_at: number | null; created_at: number;
  progress: { message: string; waiting_reason?: string; budget: Record<string, number> };
  steps: Array<{ tool: string; count: number; at: number; error?: { message: string } }>;
  result: { title?: string; tracks?: RecommendedTrack[]; duration_seconds?: number; gap?: { primary: string; count: number; seconds: number };
    coverage?: { personal_total?: number; personal_checked_until?: number; online_playlists?: Record<string, { name: string; checked_until: number; playlist_total?: number; returned_by_provider?: number; truncated_by_provider?: boolean }> };
    rejected?: Array<{ key: string; title: string; artist: string; reasons: string[] }>; notice?: string } }
const props = defineProps<{ active: boolean; downloadActive?: boolean }>()
const emit = defineEmits<{ openPlaylist: [id: string]; batchCreated: [id: string] }>()
const need = ref('')
const preferencesOpen = ref(false)
const defaultPreferences = { vocals: 'allowed', language: '', exclusions: '', target_count: 20, target_duration_minutes: 60,
  source_mode: 'personal_first', network_allowed: true, goal: 'count', full_playback_required: true }
const values = ref({ ...defaultPreferences })
const changed = ref(new Set<string>())
const run = ref<Run | null>(null)
const history = ref<Run[]>([])
const historyOpen = ref(false)
const page = ref(1)
const total = ref(0)
const busy = ref(false)
const error = ref('')
const capability = ref<{ enabled: boolean; network_enabled: boolean } | null>(null)
const saveDialog = ref<{ version: number; id: string; name: string } | null>(null)
const detail = ref<RecommendedTrack | null>(null)
const historyLoading = ref(false)
let timer: number | undefined
let disposed = false
let polling = false
let selection = 0
let submission: { signature: string; key: string } | null = null
const active = computed(() => !!run.value && ['pending', 'running'].includes(run.value.status))
const resumable = computed(() => !!run.value && ['waiting_input', 'waiting_auth', 'interrupted'].includes(run.value.status))
const tracks = computed(() => run.value?.result.tracks || [])
const statuses: Record<string, string> = { pending: '等待执行', running: '生成中', waiting_input: '待补充', waiting_auth: '待认证',
  interrupted: '已中断', finished: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消' }
const tools: Record<string, string> = { search_my_music: '检索个人音乐库', list_music_categories: '查询分类', search_online_playlists: '检索平台歌单',
  get_playlist_tracks: '获取歌单歌曲', search_online_tracks: '搜索歌曲', get_track_features: '分析歌曲特征', validate_tracks: '校验播放' }
const fields: Record<string, string> = { vocal_role: '演唱类型（歌词判断）', vocals: '人声', language: '语言', themes: '主题', moods: '情绪', scene_scores: '场景匹配', tempo: '节拍', energy: '能量', variation: '变化', rhythm_stability: '节奏稳定性' }
const coverageLabels: Record<string, string> = { full: '整曲', partial: '部分内容', full_document: '完整歌词文档',
  lyric_document: '歌词文档', partial_lyrics: '部分歌词', provider_track_declaration: '平台歌曲声明', none: '无证据' }
function featureValue(value: unknown): string {
  if (value === null || value === undefined) return '未知'
  if (typeof value === 'string' && value in vocalLabels) return vocalLabels[value]!
  if (typeof value === 'boolean') return value ? '有人声' : '无人声'
  if (Array.isArray(value)) return value.join('、') || '无'
  if (typeof value === 'object') return Object.entries(value).map(([key, score]) => `${key} ${Math.round(Number(score) * 100)}%`).join('、') || '未知'
  return String(value)
}
const vocalLabels: Record<string, string> = { lead: '明确演唱', background: '器乐为主、少量背景人声', instrumental: '纯音乐', unknown: '未知' }
const scoreLabels: Record<string, string> = { scene: '场景适配', vocals: '明确演唱', language: '游戏语言偏好', personal: '个人库' }
const checked = ref(new Set<string>())
const downloadBusy = ref(false)
const downloadSelection = ref<RecommendedTrack[] | null>(null)
const downloadTarget = ref('')
const downloadName = ref('')
function openDownload(all = false) {
  downloadSelection.value = downloadable.value.filter(t => all || checked.value.has(t.key))
  downloadTarget.value = ''; downloadName.value = ''
  void refreshLibrary()
}
const audioFormat = ref<'mp3' | 'm4a'>('mp3')
const lyricTrack = ref<RecommendedTrack | null>(null)
const lyricRun = ref<{ id: string; version: number } | null>(null)
const downloadable = computed(() => tracks.value.filter(t => !t.filename && !t.local_url && t.provider !== 'local'))
const lyricTime = computed(() => lyricTrack.value?.webpage_url === currentTrack.value?.webpage_url ? elapsed.value : 0)
function showLyrics(track: RecommendedTrack) {
  if (!run.value) return
  lyricRun.value = { id: run.value.run_id, version: run.value.result_version }
  lyricTrack.value = track
}
function toggleAll() {
  checked.value = checked.value.size === downloadable.value.length ? new Set() : new Set(downloadable.value.map(t => t.key))
}
async function download() {
  if (downloadBusy.value || props.downloadActive) return
  const selected = downloadSelection.value || []
  if (!selected.length) return
  downloadBusy.value = true; error.value = ''
  try {
    const result = await createMusicBatch(selected, audioFormat.value, undefined, downloadTarget.value === 'new' ? { playlist_name: downloadName.value.trim() } : downloadTarget.value ? { playlist_id: downloadTarget.value } : {})
    downloadSelection.value = null
    void refreshLibrary()
    emit('batchCreated', result.batch_id)
    checked.value.clear()
  } catch (failure) { fail(failure) }
  finally { downloadBusy.value = false }
}
function seekLyric(seconds: number) {
  if (lyricTrack.value?.webpage_url === currentTrack.value?.webpage_url) seek(seconds)
}
watch(() => run.value?.run_id, () => { checked.value.clear(); lyricTrack.value = null; downloadSelection.value = null })

function preferencePayload() {
  const result: Record<string, unknown> = {}
  for (const key of changed.value) {
    if (key === 'language') result.languages = values.value.language ? [values.value.language] : []
    else if (key === 'exclusions') result.exclusions = values.value.exclusions.split(/[,，;；\n]/).map(s => s.trim()).filter(Boolean)
    else result[key] = values.value[key as keyof typeof values.value]
  }
  return result
}
function reflectParsedPreferences(parsed: Record<string, any>) {
  for (const key of Object.keys(values.value)) {
    if (changed.value.has(key)) continue
    if (key === 'language') values.value.language = parsed.languages?.[0] || ''
    else if (key === 'exclusions') values.value.exclusions = (parsed.exclusions || []).join('，')
    else if (key in parsed) (values.value as Record<string, unknown>)[key] = parsed[key]
  }
}
function fail(failure: unknown) { error.value = failure instanceof Error ? failure.message : '操作失败，请重试。' }
async function loadHistory() {
  historyLoading.value = true
  try { const result = await api<{ items: Run[]; total: number }>(`/api/music/recommendations?page=${page.value}`); if (!disposed) { history.value = result.items; total.value = result.total } }
  catch (failure) { fail(failure) }
  finally { historyLoading.value = false }
}
async function selectRun(item: Run) {
  selection++
  run.value = item
  need.value = item.request.need
  values.value = { ...defaultPreferences }
  changed.value.clear()
  const saved = item.request.preferences
  for (const key of Object.keys(saved)) {
    if (key === 'languages') { values.value.language = (saved.languages as string[])[0] || ''; changed.value.add('language') }
    else if (key === 'exclusions') { values.value.exclusions = (saved.exclusions as string[]).join('，'); changed.value.add('exclusions') }
    else if (key in values.value) { (values.value as Record<string, unknown>)[key] = saved[key]; changed.value.add(key) }
  }
  historyOpen.value = false
  try { const revision = selection; const latest = await api<Run>(`/api/music/recommendations/${item.run_id}`); if (revision === selection && !disposed) run.value = latest }
  catch (failure) { fail(failure) }
}
async function create(retry = false) {
  if (busy.value || active.value || !need.value.trim()) return
  busy.value = true; error.value = ''
  const payload = { need: need.value.trim(), preferences: preferencePayload() }
  const signature = JSON.stringify({ payload, parent: retry ? run.value?.run_id : null })
  if (!submission || submission.signature !== signature) submission = { signature, key: crypto.randomUUID() }
  try {
    const url = retry && run.value ? `/api/music/recommendations/${run.value.run_id}/retry` : '/api/music/recommendations'
    const options = jsonRequest('POST', payload)
    options.headers = { ...options.headers as Record<string, string>, 'Idempotency-Key': submission.key }
    selection++; run.value = await api<Run>(url, options); submission = null
    await loadHistory()
  } catch (failure) { fail(failure) }
  finally { busy.value = false }
}
async function action(command: 'cancel' | 'continue') {
  if (!run.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const payload = command === 'continue' && run.value.status === 'waiting_input' ? { need: need.value, preferences: preferencePayload() } : {}
    run.value = await api<Run>(`/api/music/recommendations/${run.value.run_id}/${command}`, jsonRequest('POST', payload))
    await loadHistory()
  } catch (failure) { fail(failure) }
  finally { busy.value = false }
}
async function remove(item: Run) {
  if (!confirm(`删除任务“${item.request.need}”？`)) return
  try { await api(`/api/music/recommendations/${item.run_id}`, { method: 'DELETE' }); if (run.value?.run_id === item.run_id) { selection++; run.value = null }; await loadHistory() }
  catch (failure) { fail(failure) }
}
async function save() {
  if (!saveDialog.value || busy.value) return
  const confirmation = { ...saveDialog.value }
  busy.value = true; error.value = ''
  try {
    const result = await api<{ playlist_id: string }>(`/api/music/recommendations/${confirmation.id}/save`, jsonRequest('POST', { result_version: confirmation.version, name: confirmation.name.trim() }))
    saveDialog.value = null; await refreshLibrary(); await loadHistory()
    if (run.value) run.value = await api<Run>(`/api/music/recommendations/${run.value.run_id}`)
    emit('openPlaylist', result.playlist_id)
  } catch (failure) { fail(failure) }
  finally { busy.value = false }
}
async function play(track: RecommendedTrack) {
  try { await playTrack(track, tracks.value) } catch (failure) { fail(failure) }
}
async function poll() {
  if (disposed || polling || !props.active || !active.value || !run.value) return
  polling = true
  const id = run.value.run_id, revision = selection
  try { const latest = await api<Run>(`/api/music/recommendations/${id}`); if (!disposed && revision === selection) { run.value = latest; if (!['pending', 'running'].includes(latest.status)) await loadHistory() } }
  catch (failure) { fail(failure) }
  finally { polling = false }
}
async function initialize() {
  try { capability.value = await api('/api/music/recommendations/capabilities'); await loadHistory(); if (!run.value && history.value[0]) await selectRun(history.value[0]) }
  catch (failure) { fail(failure) }
}
watch(() => props.active, value => { if (value) { void initialize(); void poll() } })
watch(() => run.value?.request.parsed, parsed => { if (parsed) reflectParsedPreferences(parsed) })
onMounted(() => { if (props.active) void initialize(); timer = window.setInterval(() => { void poll() }, 2500) })
onBeforeUnmount(() => { disposed = true; selection++; clearInterval(timer) })
</script>

<template>
  <section class="scene-playlist">
    <div class="scene-toolbar"><span v-if="capability && !capability.enabled" class="scene-state">场景歌单已关闭</span><button class="mc-command" @click="historyOpen = !historyOpen; loadHistory()"><History :size="17" />历史记录</button></div>
    <p v-if="error" class="mc-error" role="alert">{{ error }}</p>
    <form class="scene-form" @submit.prevent="create(!!run)">
      <label class="scene-need">听歌需求<textarea v-model="need" required maxlength="1000" rows="2" placeholder="工作时听" :disabled="active" /></label>
      <div class="scene-form-actions"><button class="mc-command" type="button" :aria-expanded="preferencesOpen" @click="preferencesOpen = !preferencesOpen"><Settings2 :size="17" />偏好设置<ChevronDown :size="15" /></button><button class="mc-command primary" :disabled="busy || active || !need.trim() || !capability?.enabled"><LoaderCircle v-if="busy || active" class="spin" :size="17" /><Sparkles v-else :size="17" />{{ run ? '重新生成' : '生成歌单' }}</button></div>
      <div v-if="preferencesOpen" class="scene-preferences">
        <label>人声<select v-model="values.vocals" :disabled="active" @change="changed.add('vocals')"><option value="allowed">允许人声</option><option value="instrumental">纯音乐</option></select></label>
        <label>语言<select v-model="values.language" :disabled="active" @change="changed.add('language')"><option>中文</option><option>英文</option><option>日文</option><option>韩文</option><option value="">不限</option></select></label>
        <label>歌曲数量<input v-model.number="values.target_count" type="number" min="1" max="50" :disabled="active" @input="changed.add('target_count')" /></label>
        <label>目标时长（分钟）<input v-model.number="values.target_duration_minutes" type="number" min="1" max="300" :disabled="active" @input="changed.add('target_duration_minutes')" /></label>
        <label>主要目标<select v-model="values.goal" :disabled="active" @change="changed.add('goal')"><option value="count">歌曲数量</option><option value="duration">目标时长</option></select></label>
        <label>检索范围<select v-model="values.source_mode" :disabled="active" @change="changed.add('source_mode')"><option value="personal_first">个人库优先</option><option value="personal_only">仅个人库</option><option value="discover">探索新歌</option></select></label>
        <label class="scene-exclusions">排除歌曲、歌手或主题<input v-model="values.exclusions" maxlength="1000" :disabled="active" @input="changed.add('exclusions')" /></label>
        <label class="scene-checkbox"><input v-model="values.network_allowed" type="checkbox" :disabled="active || !capability?.network_enabled" @change="changed.add('network_allowed')" />允许新增网络请求</label>
        <label class="scene-checkbox"><input v-model="values.full_playback_required" type="checkbox" :disabled="active" @change="changed.add('full_playback_required')" />要求完整播放</label>
      </div>
    </form>
    <section v-if="run" class="scene-run">
      <header class="scene-run-header"><div><span class="scene-state" :data-status="run.status">{{ statuses[run.status] }}</span><span class="scene-date">{{ new Date(run.created_at * 1000).toLocaleString() }}</span></div><div class="scene-actions"><button v-if="active" class="mc-command" :disabled="busy" @click="action('cancel')"><CircleStop :size="17" />取消</button><button v-if="resumable" class="mc-command" :disabled="busy || !capability?.enabled" @click="action('continue')"><RefreshCw :size="17" />继续任务</button><button class="mc-icon" title="删除任务" aria-label="删除任务" :disabled="busy" @click="remove(run)"><Trash2 :size="17" /></button></div></header>
      <p class="scene-message" role="status"><LoaderCircle v-if="active" class="spin" :size="16" />{{ run.progress.message }}</p>
      <p v-if="run.progress.budget" class="scene-budget">已用 {{ duration(run.progress.budget.seconds) }} · {{ run.progress.budget.tools || 0 }} 次操作 · {{ run.progress.budget.network_requests || 0 }} 次网络请求 · {{ run.progress.budget.tokens || 0 }} 模型 tokens</p>
      <div v-if="run.request.parsed" class="scene-parsed"><span>{{ run.request.parsed.scene }}</span><span>{{ run.request.parsed.vocals === 'instrumental' ? '纯音乐' : '允许人声' }}</span><span>{{ run.request.parsed.languages.join('、') || '语言不限' }}</span><span>{{ run.request.parsed.target_count }} 首 / {{ run.request.parsed.target_duration_minutes }} 分钟</span><span>{{ ({ personal_first: '个人库优先', personal_only: '仅个人库', discover: '探索新歌' } as Record<string, string>)[run.request.parsed.source_mode] }}</span><span v-if="!run.request.parsed.network_allowed">禁止新增网络请求</span><span v-if="run.request.parsed.exclusions.length">排除：{{ run.request.parsed.exclusions.join('、') }}</span><span v-if="run.request.parsed.excluded_themes?.length">排除主题：{{ run.request.parsed.excluded_themes.join('、') }}</span></div>
      <details v-if="run.steps.length" class="scene-steps"><summary>执行记录 · {{ run.steps.length }}</summary><ol><li v-for="(step, index) in run.steps" :key="index"><time>{{ new Date(step.at * 1000).toLocaleTimeString() }}</time><span>{{ tools[step.tool] || step.tool }} · {{ step.count }} 项</span><small v-if="step.error">{{ step.error.message }}</small></li></ol></details>
      <template v-if="run.result.title">
        <div class="scene-result-heading"><div><h2>{{ run.result.title }}</h2><span>{{ tracks.length }} 首 · {{ duration(run.result.duration_seconds) }}</span></div><div class="scene-actions"><button class="mc-command" :disabled="!tracks.length" @click="play(tracks[0]!)"><Play :size="17" />播放全部</button><button v-if="run.saved_playlist_id" class="mc-command" @click="emit('openPlaylist', run.saved_playlist_id)">查看已保存歌单</button><button v-else class="mc-command primary" :disabled="!tracks.length || !!run.saved_at || busy" @click="saveDialog = { id: run.run_id, version: run.result_version, name: run.result.title || '' }"><Save :size="17" />确认保存</button></div></div>
        <p v-if="run.status === 'partial' && run.result.gap" class="scene-gap">{{ run.result.gap.primary === 'duration' ? `距离目标时长还差 ${duration(run.result.gap.seconds)}` : `距离目标还差 ${run.result.gap.count} 首` }}</p>
        <p v-if="run.saved_at && !run.saved_playlist_id" class="scene-gap">已保存的歌单已删除</p>
        <div class="scene-actions scene-downloads">
          <button class="mc-command" :disabled="!downloadable.length" @click="toggleAll">{{ checked.size === downloadable.length && downloadable.length ? '取消选择' : '全选在线歌曲' }}</button>
          <select v-model="audioFormat" aria-label="下载格式" :disabled="downloadBusy"><option value="mp3">MP3</option><option value="m4a">M4A</option></select>
          <button class="mc-command" :disabled="!checked.size || downloadBusy || downloadActive" @click="openDownload()"><Download :size="16" />下载选中（{{ checked.size }}）</button>
          <button class="mc-command" :disabled="!downloadable.length || downloadBusy || downloadActive" @click="openDownload(true)"><LoaderCircle v-if="downloadBusy" class="spin" :size="16" />下载全部在线歌曲</button>
          <small v-if="downloadActive">下载任务进行中，进度显示在歌单下方</small>
        </div>
        <form v-if="downloadSelection" class="scene-download-panel" @submit.prevent="download" @keydown.esc="!downloadBusy && (downloadSelection = null)">
          <strong>下载 {{ downloadSelection.length }} 首歌曲</strong>
          <label>保存位置<select v-model="downloadTarget" :disabled="downloadBusy" aria-label="保存位置"><option value="">本地和下载（默认）</option><option v-for="playlist in playlists" :key="playlist.id" :value="playlist.id">{{ playlist.name }}</option><option value="new">＋新建歌单</option></select></label>
          <label v-if="downloadTarget === 'new'">歌单名称<input v-model="downloadName" required maxlength="80" :disabled="downloadBusy" placeholder="输入自定义歌单名称" /></label>
          <small>文件保存到本地和下载；选择歌单后，下载成功的歌曲同时加入该歌单。</small>
          <div class="scene-actions"><button type="button" class="mc-command" :disabled="downloadBusy" @click="downloadSelection = null">取消</button><button type="submit" class="mc-command primary" :disabled="downloadBusy || downloadActive || !downloadSelection.length || (downloadTarget === 'new' && !downloadName.trim())">{{ downloadBusy ? '正在创建任务…' : '开始下载' }}</button></div>
        </form>
        <div class="scene-tracks"><div v-for="(track, index) in tracks" :key="track.key" class="scene-track"><input :disabled="!!track.filename || !!track.local_url || track.provider === 'local'" type="checkbox" :aria-label="`选择 ${track.title}`" :checked="checked.has(track.key)" @change="checked.has(track.key) ? checked.delete(track.key) : checked.add(track.key)" /><span class="scene-order">{{ index + 1 }}</span><button class="scene-track-play" :title="`播放 ${track.title}`" :aria-label="`播放 ${track.title}`" @click="play(track)"><TrackCover :src="track.thumbnail" :title="track.title" /><Play :size="16" /></button><div class="scene-track-name"><strong>{{ track.title }}</strong><span>{{ track.artist || '未知歌手' }}</span><small>{{ track.reason }}<span v-if="!track.match?.scoring_version || track.match.scoring_version === 'legacy'"> · 旧版评分，重新生成可应用新规则</span></small></div><span class="scene-provider">{{ providers[track.provider || ''] || track.source.label }}</span><span class="scene-duration">{{ duration(track.validation.duration ?? track.duration) }}</span><span v-if="!track.local_url && !track.filename"></span><a v-if="track.local_url || track.filename" :href="track.local_url || `/api/files/${encodeURIComponent(track.filename!)}`" download class="mc-command">已有文件</a><button class="mc-icon" title="搜索歌词" aria-label="搜索歌词" @click="showLyrics(track)"><AlignLeft :size="18" /></button><button class="mc-icon" title="匹配证据" aria-label="匹配证据" @click="detail = track"><ChevronRight :size="18" /></button></div><p v-if="!tracks.length" class="scene-empty">暂无合格曲目</p></div>
        <details v-if="run.result.coverage" class="scene-steps"><summary>检索范围</summary><p v-if="run.result.coverage.personal_total !== undefined">个人库：{{ Math.min(run.result.coverage.personal_checked_until || 0, run.result.coverage.personal_total) }} / {{ run.result.coverage.personal_total }} 首</p><p v-for="(item, key) in run.result.coverage.online_playlists" :key="key">{{ item.name }}：已读取 {{ item.checked_until }} 首<span v-if="item.playlist_total"> / {{ item.playlist_total }} 首</span><span v-if="item.truncated_by_provider"> · 平台仅返回部分曲目</span></p></details>
        <details v-if="run.result.rejected?.length" class="scene-steps"><summary>未通过曲目 · {{ run.result.rejected.length }}</summary><ul><li v-for="item in run.result.rejected" :key="item.key"><span>{{ item.title }} · {{ item.artist }}</span><small>{{ item.reasons.join('；') }}</small></li></ul></details>
        <p class="scene-result-notice">{{ run.result.notice }}</p>
      </template>
    </section>
    <section v-if="historyOpen" class="scene-history"><header><h2>任务历史</h2><button class="mc-icon" title="关闭历史" aria-label="关闭历史" @click="historyOpen = false"><X :size="18" /></button></header><LoaderCircle v-if="historyLoading" class="spin" :size="22" /><div v-for="item in history" :key="item.run_id" class="scene-history-row"><button @click="selectRun(item)"><strong>{{ item.request.need }}</strong><small>{{ new Date(item.created_at * 1000).toLocaleString() }}</small></button><span>{{ statuses[item.status] }}</span><button class="mc-icon" title="删除任务" aria-label="删除任务" @click="remove(item)"><Trash2 :size="16" /></button></div><p v-if="!history.length && !historyLoading">暂无任务</p><footer><button class="mc-icon" title="上一页" aria-label="上一页" :disabled="page === 1" @click="page--; loadHistory()"><ChevronLeft :size="18" /></button><span>{{ page }} / {{ Math.max(1, Math.ceil(total / 20)) }}</span><button class="mc-icon" title="下一页" aria-label="下一页" :disabled="page * 20 >= total" @click="page++; loadHistory()"><ChevronRight :size="18" /></button></footer></section>
    <LyricsView v-if="lyricTrack && lyricRun" :track="lyricTrack" :current-time="lyricTime" :lyrics-endpoint="`/api/music/recommendations/${lyricRun.id}/tracks/${lyricTrack.key}/lyrics`" :result-version="lyricRun.version" @close="lyricTrack = null" @seek="seekLyric"><template #controls><button class="mc-command" @click="play(lyricTrack!)"><Play :size="18" />播放这首歌</button></template></LyricsView>
    <Teleport to="body"><div v-if="saveDialog || detail" class="mc-modal-backdrop" @click.self="!busy && (saveDialog = null, detail = null)" @keydown.esc="!busy && (saveDialog = null, detail = null)"><section class="mc-modal scene-dialog" role="dialog" aria-modal="true" aria-labelledby="scene-dialog-title"><header><h2 id="scene-dialog-title">{{ detail ? detail.title : '确认保存歌单' }}</h2><button class="mc-icon" title="关闭" aria-label="关闭" :disabled="busy" @click="saveDialog = null; detail = null"><X :size="20" /></button></header><p v-if="error" class="mc-error" role="alert">{{ error }}</p><form v-if="saveDialog" @submit.prevent="save"><label>歌单名称<input v-model="saveDialog.name" maxlength="80" required autofocus /></label><p>{{ tracks.length }} 首 · 结果版本 {{ saveDialog.version }}</p><footer><button class="mc-command primary" :disabled="busy || !saveDialog.name.trim()"><LoaderCircle v-if="busy" class="spin" :size="17" /><Save v-else :size="17" />保存</button></footer></form><div v-if="detail" class="scene-evidence"><p>{{ detail.reason }}</p><p>{{ detail.validation.reason }}</p><p>播放校验：{{ new Date(detail.validation.checked_at * 1000).toLocaleString() }}</p><dl v-if="detail.match?.scoring_version === 'scene-score-v2'"><dt>总分（排序分）</dt><dd>{{ detail.match.score ?? '待判断' }}</dd><template v-for="(value, key) in detail.match.breakdown" :key="key"><dt>{{ scoreLabels[key] || key }}</dt><dd>{{ value }} 分</dd></template></dl><dl><template v-for="(value, key) in detail.analysis.features" :key="key"><dt>{{ fields[key] || key }}</dt><dd>{{ featureValue(value) }}</dd></template></dl><details><summary>证据来源与覆盖</summary><dl><template v-for="(item, key) in detail.analysis.evidence.fields" :key="key"><dt>{{ fields[key] || key }}</dt><dd>{{ item.source || '未知来源' }}<small>{{ coverageLabels[item.coverage || 'none'] || item.coverage }} · 置信度 {{ Math.round((item.confidence || 0) * 100) }}%</small></dd></template></dl><p>歌词：{{ coverageLabels[detail.analysis.evidence.lyrics?.coverage || 'none'] }}</p><p>音频：{{ coverageLabels[detail.analysis.evidence.audio?.coverage || 'none'] }}</p></details></div></section></div></Teleport>
  </section>
</template>

<style scoped>
.scene-playlist { max-width: 1120px; --mc-border: var(--music-line, #dce1df); --mc-surface: var(--music-surface, #fff); }
.scene-toolbar,.scene-form-actions,.scene-run-header,.scene-result-heading,.scene-actions,.scene-history header,.scene-history footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.scene-toolbar { justify-content: flex-end; margin-bottom: 12px; }
.scene-form { padding-bottom: 24px; border-bottom: 1px solid var(--mc-border, #dce1df); }
.scene-need { display: grid; gap: 9px; font-size: 14px; font-weight: 600; }
.scene-need textarea { box-sizing: border-box; resize: vertical; width: 100%; min-height: 92px; max-height: 250px; border: 1px solid var(--mc-border, #dce1df); border-radius: 6px; padding: 14px; font: inherit; background: var(--mc-surface, #fff); color: inherit; }
.scene-form-actions { margin-top: 12px; }
.scene-preferences { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 16px; margin-top: 22px; }
.scene-preferences label { display: grid; gap: 7px; font-size: 13px; }
.scene-preferences input:not([type=checkbox]),.scene-preferences select { box-sizing: border-box; width: 100%; min-width: 0; padding: 9px; border: 1px solid var(--mc-border, #dce1df); border-radius: 4px; font: inherit; background: var(--mc-surface, #fff); color: inherit; }
.scene-preferences .scene-exclusions { grid-column: 1 / -1; }
.scene-preferences .scene-checkbox { display: flex; align-items: center; gap: 8px; }
.scene-download-panel { display: grid; gap: 12px; padding: 16px; margin-bottom: 16px; border: 1px solid var(--mc-border); background: var(--mc-surface); border-radius: 8px; }
.scene-download-panel label { display: grid; gap: 6px; }
.scene-download-panel select, .scene-download-panel input { width: 100%; min-width: 0; padding: 8px; color: inherit; background: var(--mc-surface); border: 1px solid var(--mc-border); }
.scene-downloads { justify-content: flex-start; margin: 16px 0; }
.scene-downloads select { background: var(--mc-surface); color: inherit; border: 1px solid var(--mc-border); padding: 8px; border-radius: 4px; }
.scene-run { padding: 24px 0; }
.scene-state { font-size: 13px; color: #36755a; font-weight: 600; }
.scene-state[data-status=failed],.scene-gap { color: #ac542f; }
.scene-date,.scene-result-heading span,.scene-result-notice { font-size: 12px; opacity: .65; margin-left: 12px; }
.scene-message { display: flex; align-items: center; gap: 8px; font-size: 14px; overflow-wrap: anywhere; }
.scene-budget { font-size: 12px; opacity: .65; overflow-wrap: anywhere; }
.scene-parsed { display: flex; gap: 8px 18px; flex-wrap: wrap; font-size: 12px; opacity: .75; line-height: 1.8; }
.scene-steps { margin: 18px 0; font-size: 13px; }
.scene-steps summary { cursor: pointer; }
.scene-steps ol,.scene-steps ul { padding: 0; list-style: none; max-height: 240px; overflow: auto; }
.scene-steps li { display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 9px 0; border-bottom: 1px solid var(--mc-border, #dce1df); }
.scene-steps li small { flex-basis: 100%; opacity: .7; }
.scene-result-heading { margin: 26px 0 18px; }
.scene-result-heading h2,.scene-history h2 { font-size: 20px; margin: 0 0 7px; }
.scene-result-heading span { margin-left: 0; }
.scene-actions { justify-content: flex-end; }
.scene-gap { font-size: 13px; }
.scene-track { display: grid; grid-template-columns: 18px 28px 46px minmax(0,1fr) 105px 48px auto 32px 32px; align-items: center; gap: 12px; min-height: 88px; border-bottom: 1px solid var(--mc-border, #dce1df); }
.scene-order,.scene-duration,.scene-provider { font-size: 12px; opacity: .65; }
.scene-track-play { padding: 0; width: 46px; height: 46px; position: relative; border: 0; background: transparent; border-radius: 4px; cursor: pointer; overflow: hidden; }
.scene-track-play > svg { position: absolute; left: 15px; top: 15px; color: white; filter: drop-shadow(0 1px 2px black); }
.scene-track-name { min-width: 0; display: grid; gap: 4px; padding: 10px 0; }
.scene-track-name strong { font-size: 14px; overflow-wrap: anywhere; }
.scene-track-name span { font-size: 12px; opacity: .7; }
.scene-track-name small { font-size: 11px; opacity: .6; overflow-wrap: anywhere; }
.scene-empty { text-align: center; padding: 32px; opacity: .6; }
.scene-result-notice { margin: 16px 0; }
.scene-history { border-top: 1px solid var(--mc-border, #dce1df); padding-top: 20px; }
.scene-history-row { display: flex; gap: 12px; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--mc-border, #dce1df); font-size: 13px; }
.scene-history-row > button:first-child { flex: 1; min-width: 0; display: grid; gap: 6px; text-align: left; border: none; background: transparent; color: inherit; cursor: pointer; }
.scene-history-row strong { overflow-wrap: anywhere; font-weight: 500; }
.scene-history-row small { opacity: .6; }
.scene-history footer { justify-content: flex-end; padding: 14px 0; font-size: 13px; }
.scene-dialog { max-height: 85vh; overflow: auto; }
.scene-evidence { font-size: 13px; }
.scene-evidence dl { display: grid; grid-template-columns: 100px minmax(0,1fr); gap: 9px; }
.scene-evidence dd { margin: 0; overflow-wrap: anywhere; }
.scene-evidence dd small { display: block; margin-top: 4px; opacity: .65; }
@media(max-width: 700px) { .scene-preferences { grid-template-columns: repeat(2,minmax(0,1fr)); } .scene-track { grid-template-columns: 18px 20px 40px minmax(0,1fr) auto 28px 28px; gap: 8px; } .scene-track-play { width: 40px; height: 40px; } .scene-provider,.scene-duration { display: none; } .scene-date { display: block; margin: 6px 0 0; } .scene-result-heading h2 { font-size: 18px; } }
@media(max-width: 380px) { .scene-preferences { grid-template-columns: 1fr; } }
</style>
