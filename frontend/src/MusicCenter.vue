<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowLeft, ArrowRight, ChevronRight, Clock3, Compass, Download, Heart, Headphones, Home, ListMusic, LoaderCircle, Plus, Radio, Search, Settings2, Trash2, Upload, UserRound, Video, X, Pencil, Play, Eraser, FolderOpen, Palette, RefreshCw } from 'lucide-vue-next'
import MusicWorkspace from './MusicWorkspace.vue'
import MusicImportWorkspace from './MusicImportWorkspace.vue'
import ScenePlaylist from './ScenePlaylist.vue'
import MusicTrackList from './MusicTrackList.vue'
import TrackCover from './TrackCover.vue'
import AppearanceSettings from './AppearanceSettings.vue'
import AccountSettings from './AccountSettings.vue'
import WorkHistory from './WorkHistory.vue'
import { account, logout } from './auth'
import { LogOut, ShieldCheck } from 'lucide-vue-next'
import { api, counts, duration, getTracks, jsonRequest, libraryError, libraryRevision, playlists, profile,
  refreshLibrary, trackArtwork, trackKey, trackPayload, type MusicTrack, type Playlist } from './musicLibrary'
import { loadMusicTheme, themeError } from './musicTheme'
import { addToQueue, playMode, playTrack } from './musicPlayer'
import './music-center.css'
import './music-theme.css'

const emit = defineEmits<{ openTools: [view: 'video' | 'watermark']; openAdmin: [] }>()
const props = withDefaults(defineProps<{ active?: boolean }>(), { active: true })
type View = 'home' | 'search' | 'bulk' | 'scene' | 'favorites' | 'history' | 'downloads' | 'trials' | 'playlist' | 'profile' | 'appearance' | 'resources'
const view = ref<View>('home')
const playlistId = ref('')
const query = ref('')
const source = ref('all')
const tracks = ref<MusicTrack[]>([])
const recent = ref<MusicTrack[]>([])
const local = ref<MusicTrack[]>([])
const loading = ref(false)
const importing = ref(false)
const error = ref('')
const toast = ref('')
const searchWorkspace = ref<{ search: (query: string, source?: string) => Promise<void>; downloadTrack: (track: MusicTrack) => Promise<void>; showBatch: (id: string) => Promise<void>; activeTask: boolean } | null>(null)
const importInput = ref<HTMLInputElement | null>(null)
const appearanceTrack = ref<MusicTrack | null>(null)
const appearancePlaylist = ref<Playlist | null>(null)
const modal = ref<'playlist' | 'add' | null>(null)
const modalBusy = ref(false)
const modalError = ref('')
const modalName = ref('')
const modalDescription = ref('')
const editingPlaylist = ref<Playlist | null>(null)
const targetTrack = ref<MusicTrack | null>(null)
const menuTrack = ref<MusicTrack | null>(null)
const menuPosition = ref({ top: 0, left: 0 })
const menuElement = ref<HTMLElement | null>(null)
const dialogElement = ref<HTMLElement | null>(null)
const coverProgress = ref({ running: false, completed: 0, updated: 0, total: 0, failed: 0 })
let coverTimer: number | undefined
let coverPolling = false
let disposed = false
let previousFocus: HTMLElement | null = null
let generation = 0
let toastTimer: number | undefined
const navigation: Array<{ view: View; playlistId: string }> = []
const activePlaylist = computed(() => playlists.value.find(item => item.id === playlistId.value))
const titles: Record<View, string> = { home: '音乐中心', search: '搜索歌曲', bulk: '批量找歌', scene: '场景歌单', favorites: '我喜欢的音乐', history: '最近播放', downloads: '本地和下载', trials: '试听列表', playlist: '我的歌单', profile: '账号安全', appearance: '外观设置', resources: '任务与文件' }
const title = computed(() => view.value === 'playlist' ? activePlaylist.value?.name || '我的歌单' : titles[view.value])
const greeting = new Date().getHours() < 12 ? '上午好' : new Date().getHours() < 18 ? '下午好' : '晚上好'
const themes = [
  { title: '轻松时刻', subtitle: '纯音乐', query: '轻音乐', image: '/music/quiet.jpg', className: 'quiet' },
  { title: '华语精选', subtitle: '华语流行', query: '周杰伦', image: '/music/studio.jpg', className: 'pop' },
  { title: '原声与吉他', subtitle: '民谣', query: '民谣', image: '/music/guitar.jpg', className: 'acoustic' },
]

function message(value: string) {
  toast.value = value
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toast.value = '' }, 3500)
}
async function logoutAccount() {
  try { await logout() }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '退出失败，请重试。' }
}
function go(next: View, id = '', remember = true) {
  if (remember && (next !== view.value || id !== playlistId.value)) navigation.push({ view: view.value, playlistId: playlistId.value })
  view.value = next
  playlistId.value = id
  menuTrack.value = null
  error.value = ''
  window.scrollTo({ top: 0 })
}
function openAppearance(track: MusicTrack | null = null, playlist: Playlist | null = null) {
  go('appearance')
  appearanceTrack.value = track
  appearancePlaylist.value = playlist
}
function back() { const previous = navigation.pop(); if (previous) go(previous.view, previous.playlistId, false) }
async function loadContent() {
  const revision = ++generation
  if (['search', 'bulk', 'scene', 'profile', 'appearance', 'resources'].includes(view.value)) return
  loading.value = true
  try {
    if (view.value === 'home') {
      const [history, downloaded] = await Promise.all([getTracks('history'), getTracks('downloads')])
      if (revision !== generation) return
      recent.value = history.slice(0, 6)
      local.value = downloaded.slice(0, 6)
    } else {
      const result = await getTracks(view.value, playlistId.value)
      if (revision === generation) tracks.value = result
    }
    if (revision === generation) error.value = ''
  } catch (failure) {
    if (revision === generation) error.value = failure instanceof Error ? failure.message : '歌曲加载失败。'
  } finally { if (revision === generation) loading.value = false }
}
async function search(text = query.value, provider = source.value) {
  const value = text.trim()
  if (!value) { go('search'); return }
  query.value = value
  source.value = provider
  go('search')
  await nextTick()
  await searchWorkspace.value?.search(value, source.value)
}
async function download(track: MusicTrack) {
  if (track.local_url) {
    const anchor = document.createElement('a')
    anchor.href = track.local_url
    anchor.download = track.filename || track.title
    anchor.click()
    return
  }
  go('search')
  await nextTick()
  await searchWorkspace.value?.downloadTrack(track)
}
async function radio() {
  try {
    const saved = await getTracks('favorites')
    const list = saved.length ? saved : await getTracks('downloads')
    if (!list.length) { await search('轻音乐', 'all'); return }
    playMode.value = 'shuffle'
    await playTrack(list[Math.floor(Math.random() * list.length)]!, list)
    message('私人电台已开始播放')
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '私人电台加载失败。' }
}
function openModal(next: 'playlist' | 'add', playlist: Playlist | null = null) {
  previousFocus = document.activeElement as HTMLElement
  modal.value = next
  modalError.value = ''
  editingPlaylist.value = playlist
  modalName.value = playlist?.name || ''
  modalDescription.value = playlist?.description || ''
  void nextTick(() => dialogElement.value?.querySelector<HTMLElement>('input, button')?.focus())
}
function closeModal() {
  if (modalBusy.value) return
  modal.value = null
  previousFocus?.focus()
}
function modalKey(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); closeModal() }
  if (event.key !== 'Tab') return
  const elements = Array.from(dialogElement.value?.querySelectorAll<HTMLElement>('button:not(:disabled), input, textarea, select') || [])
  const first = elements[0], last = elements[elements.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
async function savePlaylist() {
  if (modalBusy.value || !modalName.value.trim()) return
  modalBusy.value = true
  modalError.value = ''
  try {
    const editing = editingPlaylist.value
    const result = await api(editing ? `/api/library/playlists/${editing.id}` : '/api/library/playlists',
      jsonRequest(editing ? 'PATCH' : 'POST', { name: modalName.value.trim(), description: modalDescription.value }))
    await refreshLibrary()
    const id = editing?.id || result.id
    if (targetTrack.value && !editing) await api(`/api/library/playlists/${id}/tracks`, jsonRequest('POST', trackPayload(targetTrack.value)))
    if (targetTrack.value && !editing) { targetTrack.value = null; await refreshLibrary() }
    modal.value = null
    go('playlist', id)
    message(editing ? '歌单已更新' : '歌单已创建')
  } catch (failure) { modalError.value = failure instanceof Error ? failure.message : '歌单保存失败。' }
  finally { modalBusy.value = false }
}
async function addToPlaylist(id: string) {
  if (!targetTrack.value || modalBusy.value) return
  modalBusy.value = true
  modalError.value = ''
  try {
    await api(`/api/library/playlists/${id}/tracks`, jsonRequest('POST', trackPayload(targetTrack.value)))
    await refreshLibrary()
    modal.value = null
    targetTrack.value = null
    message('已加入歌单')
  } catch (failure) { modalError.value = failure instanceof Error ? failure.message : '添加失败。' }
  finally { modalBusy.value = false }
}
async function deletePlaylist() {
  if (!activePlaylist.value || !window.confirm(`删除歌单“${activePlaylist.value.name}”？`)) return
  try {
    await api(`/api/library/playlists/${playlistId.value}`, { method: 'DELETE' })
    go('home')
    await refreshLibrary()
    message('歌单已删除')
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '删除失败。' }
}
async function clearHistory() {
  if (!window.confirm('清空全部播放历史？')) return
  try { await api('/api/library/history', { method: 'DELETE' }); await refreshLibrary(); message('播放历史已清空') }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '清空失败。' }
}
async function backfill() {
  if (coverProgress.value.running) return
  try {
    coverProgress.value = await api('/api/library/covers/backfill', { method: 'POST' })
    window.clearTimeout(coverTimer)
    coverTimer = window.setTimeout(() => { void pollCovers() }, 300)
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '封面补全失败。' }
}
async function pollCovers() {
  window.clearTimeout(coverTimer)
  coverTimer = undefined
  if (disposed || coverPolling) return
  coverPolling = true
  try {
    const previous = coverProgress.value.updated
    coverProgress.value = await api('/api/library/covers/backfill')
    if (disposed) return
    if (coverProgress.value.updated !== previous) await refreshLibrary()
  } catch { if (!disposed) coverTimer = window.setTimeout(() => { void pollCovers() }, 5000) }
  finally {
    coverPolling = false
    if (!disposed && coverProgress.value.running && !coverTimer) coverTimer = window.setTimeout(() => { void pollCovers() }, 2000)
  }
}
async function importFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (!files.length) return
  importing.value = true
  error.value = ''
  let completed = 0
  try {
    for (const file of files) {
      const form = new FormData()
      form.append('file', file)
      await api('/api/library/import', { method: 'POST', body: form })
      completed++
    }
    message(`已导入 ${completed} 首歌曲`)
  } catch (failure) { error.value = `${completed ? `已导入 ${completed} 首。` : ''}${failure instanceof Error ? failure.message : '导入失败。'}` }
  finally { importing.value = false; go('downloads'); await refreshLibrary() }
}
function showMenu(track: MusicTrack, event: MouseEvent) {
  menuTrack.value = track
  menuPosition.value = { top: Math.max(8, Math.min(event.clientY, window.innerHeight - 330)), left: Math.max(8, Math.min(event.clientX - 170, window.innerWidth - 208)) }
  void nextTick(() => menuElement.value?.querySelector<HTMLElement>('button')?.focus())
}
function queueSong() { if (menuTrack.value) { addToQueue(menuTrack.value); message('已加入播放队列') }; menuTrack.value = null }
function selectPlaylist() { targetTrack.value = menuTrack.value; menuTrack.value = null; openModal('add') }
async function removeSong() {
  const track = menuTrack.value
  menuTrack.value = null
  if (!track?.id) return
  const url = view.value === 'playlist' ? `/api/library/playlists/${playlistId.value}/tracks/${track.id}` : `/api/library/history/${track.id}`
  try { await api(url, { method: 'DELETE' }); await refreshLibrary() }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '移除失败。' }
}
watch([view, playlistId], () => { tracks.value = []; void loadContent() })
watch(libraryRevision, () => {
  void loadContent()
  if (!disposed && !coverTimer && !coverPolling) coverTimer = window.setTimeout(() => { void pollCovers() }, 300)
})
onMounted(async () => { await Promise.all([refreshLibrary(), loadMusicTheme()]); if (!libraryRevision.value) await loadContent(); if (!disposed && !libraryError.value) void backfill() })
onBeforeUnmount(() => { disposed = true; generation++; window.clearTimeout(toastTimer); window.clearTimeout(coverTimer) })
defineExpose({ download, search })
</script>

<template>
  <div class="music-center">
    <aside class="mc-sidebar" aria-label="音乐中心导航">
      <button class="mc-brand" @click="go('home')"><Headphones :size="25" /><span>VideoDream</span></button>
      <button class="mc-profile" title="外观设置" aria-label="外观设置" @click="openAppearance()"><span class="mc-avatar"><img v-if="profile.avatar" :src="profile.avatar" alt="头像" /><UserRound v-else :size="23" /></span><span><strong>{{ profile.nickname }}</strong><small>我的音乐空间</small></span><ChevronRight :size="16" /></button>
      <nav class="mc-discover" aria-label="发现音乐"><button :class="{ active: view === 'home' }" @click="go('home')"><Home :size="19" />推荐</button><button :class="{ active: view === 'search' }" @click="go('search')"><Compass :size="19" />发现</button><button :class="{ active: view === 'bulk' }" @click="go('bulk')"><ListMusic :size="19" />批量找歌</button><button @click="radio"><Radio :size="19" />私人电台</button></nav>
      <nav class="mc-discover" aria-label="场景歌单"><button :class="{ active: view === 'scene' }" @click="go('scene')"><Headphones :size="19" />场景歌单</button></nav>
      <div class="mc-nav-label">我的音乐</div>
      <nav class="mc-library-nav" aria-label="我的音乐"><button :class="{ active: view === 'favorites' }" @click="go('favorites')"><Heart :size="19" /><span>我喜欢</span><small>{{ counts.favorites }}</small></button><button :class="{ active: view === 'history' }" @click="go('history')"><Clock3 :size="19" /><span>最近播放</span><small>{{ counts.history }}</small></button><button :class="{ active: view === 'downloads' }" @click="go('downloads')"><FolderOpen :size="19" /><span>本地和下载</span><small>{{ counts.downloads }}</small></button><button :class="{ active: view === 'trials' }" @click="go('trials')"><Headphones :size="19" /><span>试听列表</span><small>{{ counts.trials }}</small></button></nav>
      <div class="mc-playlist-label"><span>自建歌单</span><button class="mc-icon" title="创建歌单" aria-label="创建歌单" @click="targetTrack = null; openModal('playlist')"><Plus :size="18" /></button></div>
      <nav class="mc-playlist-nav" aria-label="自建歌单"><button v-for="playlist in playlists" :key="playlist.id" :class="{ active: view === 'playlist' && playlistId === playlist.id }" @click="go('playlist', playlist.id)"><img v-if="playlist.thumbnail" class="playlist-nav-image" :src="playlist.thumbnail" alt="" /><ListMusic v-else :size="17" /><span>{{ playlist.name }}</span><small>{{ playlist.count }}</small></button><p v-if="!playlists.length" class="mc-sidebar-empty">暂无歌单</p></nav>
      <div class="mc-tools-nav"><button @click="go('resources')"><FolderOpen :size="18" />任务与文件<ArrowRight :size="15" /></button><button @click="emit('openTools', 'video')"><Video :size="18" />视频下载<ArrowRight :size="15" /></button><button @click="emit('openTools', 'watermark')"><Eraser :size="18" />视频去水印<ArrowRight :size="15" /></button></div>
    </aside>
    <div class="mc-body">
      <header class="mc-topbar"><button class="mc-icon" title="返回" aria-label="返回" :disabled="!navigation.length" @click="back"><ArrowLeft :size="21" /></button><form class="mc-search" @submit.prevent="search()"><Search :size="19" /><input v-model="query" aria-label="搜索音乐" placeholder="搜索歌曲、歌手或粘贴链接" /><button v-if="query" type="button" class="mc-icon" title="清空搜索" aria-label="清空搜索" @click="query = ''"><X :size="15" /></button></form><select v-model="source" aria-label="音乐搜索来源" class="mc-source"><option value="all">全部平台</option><option value="qq">QQ 音乐</option><option value="netease">网易云音乐</option><option value="kugou">酷狗音乐</option><option value="video">视频音轨</option></select><div class="mc-account-actions"><button class="mc-icon mc-settings" title="个人中心" aria-label="个人中心" @click="openAppearance()"><Settings2 :size="20" /></button><button v-if="account?.role === 'super_admin'" class="mc-icon" title="管理后台" aria-label="管理后台" @click="emit('openAdmin')"><ShieldCheck :size="20" /></button><button class="mc-icon" title="退出登录" aria-label="退出登录" @click="logoutAccount"><LogOut :size="20" /></button></div></header>
      <main class="mc-content">
        <div v-if="libraryError || error" class="mc-error" role="alert"><span>{{ error || libraryError }}</span><button class="mc-icon" title="重新加载" aria-label="重新加载" @click="refreshLibrary().then(loadContent)"><LoaderCircle v-if="loading" :size="18" class="spin" /><ArrowRight v-else :size="18" /></button></div>
        <div class="mc-page-heading"><div class="mc-heading-title"><span v-if="view === 'playlist'" class="playlist-heading-cover"><TrackCover :src="activePlaylist?.thumbnail" :title="activePlaylist?.name" /></span><div><p v-if="view === 'home'">{{ greeting }}，{{ profile.nickname }}</p><p v-else-if="!['profile', 'appearance', 'search'].includes(view)">{{ view === 'playlist' ? '自建歌单' : '我的音乐' }}</p><h1>{{ title }}</h1><p v-if="view === 'playlist' && activePlaylist?.description" class="mc-description">{{ activePlaylist.description }}</p><p v-if="view === 'downloads' && coverProgress.running" role="status">正在补全封面 {{ coverProgress.completed }}/{{ coverProgress.total }}</p></div></div><div class="mc-heading-actions"><template v-if="view === 'downloads'"><button class="mc-icon" :disabled="coverProgress.running" title="补全封面" aria-label="补全封面" @click="backfill"><LoaderCircle v-if="coverProgress.running" :size="18" class="spin" /><RefreshCw v-else :size="18" /></button><button class="mc-command" :disabled="importing" @click="importInput?.click()"><LoaderCircle v-if="importing" :size="17" class="spin" /><Upload v-else :size="17" />{{ importing ? '导入中' : '导入音乐' }}</button></template><button v-if="view === 'history' && tracks.length" class="mc-icon" title="清空播放历史" aria-label="清空播放历史" @click="clearHistory"><Trash2 :size="18" /></button><template v-if="view === 'playlist'"><button class="mc-icon" title="歌单外观设置" aria-label="歌单外观设置" @click="openAppearance(null, activePlaylist || null)"><Palette :size="18" /></button><button class="mc-icon" title="编辑歌单" aria-label="编辑歌单" @click="openModal('playlist', activePlaylist || null)"><Pencil :size="18" /></button><button class="mc-icon" title="删除歌单" aria-label="删除歌单" @click="deletePlaylist"><Trash2 :size="18" /></button></template></div></div>
        <template v-if="view === 'home'">
          <section class="mc-themes" aria-label="主题音乐"><button v-for="theme in themes" :key="theme.query" class="mc-theme" :class="theme.className" @click="search(theme.query, 'all')"><img :src="theme.image" :alt="theme.title" /><span class="mc-theme-caption"><span><small>{{ theme.subtitle }}</small><strong>{{ theme.title }}</strong></span><span class="theme-play"><Play :size="18" fill="currentColor" /></span></span></button></section>
          <section class="mc-home-section"><div class="mc-section-heading"><h2>{{ recent.length ? '继续聆听' : '本地音乐' }}</h2><button class="mc-text-button" @click="go(recent.length ? 'history' : 'downloads')">查看全部<ChevronRight :size="16" /></button></div><div v-if="loading" class="mc-loading"><LoaderCircle :size="22" class="spin" /></div><div v-else-if="(recent.length ? recent : local).length" class="mc-home-tracks"><div v-for="track in (recent.length ? recent : local)" :key="trackKey(track)" class="mc-home-track"><button @click="playTrack(track, recent.length ? recent : local)"><TrackCover :src="trackArtwork(track)" :title="track.title" /><span><strong>{{ track.title }}</strong><small>{{ track.artist || '本地音乐' }}</small></span></button><button class="mc-icon" :title="`播放 ${track.title}`" :aria-label="`播放 ${track.title}`" @click="playTrack(track, recent.length ? recent : local)"><Play :size="17" /></button></div></div><div v-else class="mc-empty"><Headphones :size="30" /><p>暂无播放记录</p></div></section>
          <section class="mc-home-section"><div class="mc-section-heading"><h2>我的歌单</h2><button class="mc-text-button" @click="targetTrack = null; openModal('playlist')"><Plus :size="16" />创建歌单</button></div><div v-if="playlists.length" class="mc-playlist-grid"><button v-for="playlist in playlists" :key="playlist.id" class="mc-playlist-item" @click="go('playlist', playlist.id)"><span class="playlist-art"><img v-if="playlist.thumbnail" :src="playlist.thumbnail" :alt="playlist.name" /><ListMusic v-else :size="29" /></span><span><strong>{{ playlist.name }}</strong><small>{{ playlist.count }} 首歌曲</small></span><ChevronRight :size="17" /></button></div><div v-else class="mc-empty compact"><ListMusic :size="29" /><p>暂无歌单</p></div></section>
        </template>
        <MusicImportWorkspace v-show="view === 'bulk'" :active="view === 'bulk'" :download-active="Boolean(searchWorkspace?.activeTask)" @batch-created="searchWorkspace?.showBatch($event)" />
        <ScenePlaylist v-if="view === 'scene'" :active="props.active && view === 'scene'" @open-playlist="go('playlist', $event)" />
        <section v-show="view === 'search' || view === 'bulk'" class="mc-search-workspace"><MusicWorkspace ref="searchWorkspace" :active="view === 'search' || view === 'bulk'" :queue-only="view === 'bulk'" :embedded="true" @track-menu="showMenu" /></section>
        <template v-if="!['home', 'search', 'bulk', 'scene', 'profile', 'appearance', 'resources'].includes(view)"><div class="mc-list-toolbar"><button class="mc-command primary" :disabled="!tracks.length" @click="playTrack(tracks[0]!, tracks)"><Play :size="17" fill="currentColor" />播放全部</button><span>{{ tracks.length }} 首歌曲</span></div><div v-if="loading" class="mc-loading"><LoaderCircle :size="23" class="spin" /></div><MusicTrackList v-else :tracks="tracks" :empty="view === 'favorites' ? '暂无喜欢的音乐' : view === 'history' ? '暂无播放记录' : view === 'downloads' ? '暂无本地音乐' : view === 'trials' ? '暂无试听记录' : '歌单暂无歌曲'" @more="showMenu" /></template>
        <nav v-if="view === 'profile' || view === 'appearance'" class="mc-profile-tabs" aria-label="个人设置"><button :aria-current="view === 'appearance' ? 'page' : undefined" @click="openAppearance()"><Palette :size="17" />外观设置</button><button :aria-current="view === 'profile' ? 'page' : undefined" @click="go('profile')"><ShieldCheck :size="17" />账号安全</button></nav>
        <AccountSettings v-if="view === 'profile'" />
        <WorkHistory v-if="view === 'resources'" />
        <p v-if="view === 'appearance' && themeError" class="mc-error" role="alert">{{ themeError }}</p><AppearanceSettings v-if="view === 'appearance'" :initial-track="appearanceTrack" :initial-playlist="appearancePlaylist" />
      </main>
    </div>
    <input ref="importInput" type="file" multiple accept=".mp3,.m4a,.flac,.wav,.ogg,.aac,.opus" hidden aria-label="导入本地音乐" @change="importFiles" />
    <div v-if="toast" class="mc-toast" role="status">{{ toast }}</div>
  </div>
  <Teleport to="body">
    <div v-if="menuTrack" class="mc-menu-backdrop" @click.self="menuTrack = null" @keydown.esc="menuTrack = null"><div ref="menuElement" class="mc-track-menu" :style="{ top: `${menuPosition.top}px`, left: `${menuPosition.left}px` }"><button @click="queueSong"><ListMusic :size="17" />加入播放队列</button><button @click="selectPlaylist"><Plus :size="17" />加入歌单</button><button @click="openAppearance(menuTrack!)"><Palette :size="17" />外观设置</button><button @click="download(menuTrack!); menuTrack = null"><Download :size="17" />{{ menuTrack.local_url ? '保存音频' : '下载歌曲' }}</button><a v-if="!menuTrack.webpage_url.startsWith('/api/')" :href="menuTrack.webpage_url" target="_blank" rel="noreferrer" @click="menuTrack = null"><ArrowRight :size="17" />原始页面</a><button v-if="view === 'playlist' || view === 'history' || view === 'trials'" class="danger" @click="removeSong"><Trash2 :size="17" />{{ view === 'playlist' ? '移出歌单' : '移除记录' }}</button></div></div>
    <div v-if="modal" class="mc-modal-backdrop" @click.self="closeModal"><section ref="dialogElement" class="mc-modal" role="dialog" aria-modal="true" aria-labelledby="music-modal-title" @keydown="modalKey"><header><h2 id="music-modal-title">{{ modal === 'add' ? '加入歌单' : editingPlaylist ? '编辑歌单' : '创建歌单' }}</h2><button class="mc-icon" :disabled="modalBusy" title="关闭" aria-label="关闭" @click="closeModal"><X :size="21" /></button></header><p v-if="modalError" class="mc-modal-error" role="alert">{{ modalError }}</p><form v-if="modal === 'playlist'" @submit.prevent="savePlaylist"><label>歌单名称<input v-model="modalName" maxlength="80" required /></label><label>简介<textarea v-model="modalDescription" maxlength="300" rows="3" /></label><footer><button type="button" class="mc-command" :disabled="modalBusy" @click="closeModal">取消</button><button class="mc-command primary" :disabled="modalBusy || !modalName.trim()" type="submit"><LoaderCircle v-if="modalBusy" :size="17" class="spin" />保存</button></footer></form><div v-else class="mc-add-playlists"><button v-for="playlist in playlists" :key="playlist.id" :disabled="modalBusy" @click="addToPlaylist(playlist.id)"><ListMusic :size="21" /><span>{{ playlist.name }}<small>{{ playlist.count }} 首歌曲</small></span><Plus :size="17" /></button><button :disabled="modalBusy" @click="openModal('playlist')"><Plus :size="21" />新建歌单</button></div></section></div>
  </Teleport>
</template>
