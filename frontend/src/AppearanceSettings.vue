<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Disc3, ImagePlus, ListMusic, LoaderCircle, Palette, RefreshCw, Save, Search, UserRound } from 'lucide-vue-next'
import ThemeSettings from './ThemeSettings.vue'
import ImageSettings from './ImageSettings.vue'
import TrackCover from './TrackCover.vue'
import { api, counts, getTracks, jsonRequest, libraryError, playlists, profile, refreshLibrary,
  trackArtwork, trackArtworkMap, trackKey, trackPayload, type MusicTrack, type Playlist } from './musicLibrary'
import { currentTrack } from './musicPlayer'

const props = defineProps<{ initialTrack?: MusicTrack | null; initialPlaylist?: Playlist | null }>()
type View = 'theme' | 'profile' | 'tracks' | 'playlists'
const tabs = [{ id: 'theme' as const, label: '主题', icon: Palette }, { id: 'profile' as const, label: '个人资料', icon: UserRound },
  { id: 'tracks' as const, label: '歌曲封面', icon: Disc3 }, { id: 'playlists' as const, label: '歌单封面', icon: ListMusic }]
const view = ref<View>(props.initialTrack ? 'tracks' : props.initialPlaylist ? 'playlists' : 'theme')
const draft = ref({ nickname: profile.value.nickname, bio: profile.value.bio })
const busy = ref(false)
const loading = ref(false)
const loaded = ref(false)
const error = ref('')
const status = ref('')
const tracks = ref<MusicTrack[]>([])
const query = ref('')
const selectedKey = ref(props.initialTrack ? trackKey(props.initialTrack) : currentTrack.value ? trackKey(currentTrack.value) : '')
const playlistId = ref(props.initialPlaylist?.id || playlists.value[0]?.id || '')
const editor = ref<{ title: string; endpoint: string; image?: string | null; track?: MusicTrack; playlist?: Playlist } | null>(null)
const profileDirty = computed(() => draft.value.nickname !== profile.value.nickname || draft.value.bio !== profile.value.bio)
const availableTracks = computed(() => {
  const choices = new Map<string, MusicTrack>()
  for (const track of [props.initialTrack, currentTrack.value, ...tracks.value]) {
    if (track) choices.set(trackKey(track), track)
  }
  return [...choices.values()]
})
const filteredTracks = computed(() => {
  const term = query.value.trim().toLocaleLowerCase()
  return availableTracks.value.filter(track => `${track.title} ${track.artist || track.uploader || ''}`.toLocaleLowerCase().includes(term))
})
const selectedTrack = computed(() => availableTracks.value.find(track => trackKey(track) === selectedKey.value))
const selectedPlaylist = computed(() => playlists.value.find(playlist => playlist.id === playlistId.value))

async function loadTracks() {
  loading.value = true
  error.value = ''
  try {
    tracks.value = await getTracks('all')
    loaded.value = true
    if (!selectedKey.value && availableTracks.value[0]) selectedKey.value = trackKey(availableTracks.value[0])
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '歌曲加载失败。' }
  finally { loading.value = false }
}
function tabKey(event: KeyboardEvent) {
  const index = tabs.findIndex(tab => tab.id === view.value)
  const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length
    : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null
  if (next === null) return
  event.preventDefault()
  view.value = tabs[next]!.id
  document.getElementById(`appearance-tab-${view.value}`)?.focus()
}
async function saveProfile() {
  if (busy.value || !draft.value.nickname.trim()) return
  busy.value = true
  error.value = ''
  status.value = ''
  try {
    await api('/api/library/profile', jsonRequest('PATCH', { ...draft.value, nickname: draft.value.nickname.trim(), avatar: profile.value.avatar }))
    await refreshLibrary()
    if (libraryError.value) throw new Error(libraryError.value)
    draft.value = { nickname: profile.value.nickname, bio: profile.value.bio }
    status.value = '个人资料已保存'
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '资料保存失败。' }
  finally { busy.value = false }
}
function editAvatar() {
  error.value = ''; status.value = ''
  editor.value = { title: '个人头像', endpoint: '/api/library/profile/avatar', image: profile.value.avatar }
}
async function editTrackCover() {
  const track = selectedTrack.value
  if (!track || busy.value) return
  busy.value = true
  error.value = ''; status.value = ''
  try {
    const id = track.id || (await api<{ id: string }>('/api/library/tracks', jsonRequest('POST', trackPayload(track)))).id
    editor.value = { title: `${track.title} · 封面`, endpoint: `/api/library/tracks/${id}/cover`, image: trackArtwork(track), track }
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '封面设置加载失败。' }
  finally { busy.value = false }
}
function editPlaylistCover() {
  const playlist = selectedPlaylist.value
  if (!playlist) return
  error.value = ''; status.value = ''
  editor.value = { title: `${playlist.name} · 封面`, endpoint: `/api/library/playlists/${playlist.id}/cover`, image: playlist.thumbnail, playlist }
}
async function imageSaved(result: { thumbnail?: string | null; avatar?: string | null }) {
  const target = editor.value
  if (target?.track) trackArtworkMap.value.set(trackKey(target.track), result.thumbnail || null)
  if (target?.playlist) target.playlist.thumbnail = result.thumbnail || null
  if (result.avatar !== undefined) profile.value = { ...profile.value, avatar: result.avatar }
  await refreshLibrary()
  if (target?.track && loaded.value) await loadTracks()
  if (libraryError.value) error.value = libraryError.value
  else if (!error.value) status.value = '图片已保存'
}
watch(view, next => {
  error.value = ''; status.value = ''
  if (next === 'tracks' && !loaded.value && !loading.value) void loadTracks()
})
watch(query, () => { selectedKey.value = '' }, { flush: 'sync' })
watch(profile, (value, previous) => {
  if (draft.value.nickname === previous.nickname && draft.value.bio === previous.bio) {
    draft.value = { nickname: value.nickname, bio: value.bio }
  }
})
watch(playlists, value => {
  if (!value.some(playlist => playlist.id === playlistId.value)) playlistId.value = value[0]?.id || ''
})
watch(() => props.initialTrack, track => {
  if (track) { query.value = ''; selectedKey.value = trackKey(track); view.value = 'tracks' }
})
watch(() => props.initialPlaylist, playlist => {
  if (playlist) { playlistId.value = playlist.id; view.value = 'playlists' }
})
onMounted(() => { if (view.value === 'tracks') void loadTracks() })
</script>

<template>
  <div class="appearance-workspace">
    <nav class="appearance-tabs" role="tablist" aria-label="个性化设置">
      <button v-for="tab in tabs" :id="`appearance-tab-${tab.id}`" :key="tab.id" type="button" role="tab" :tabindex="view === tab.id ? 0 : -1" :aria-selected="view === tab.id" :aria-controls="`appearance-panel-${tab.id}`" @click="view = tab.id" @keydown="tabKey"><component :is="tab.icon" :size="17" /><span>{{ tab.label }}</span></button>
    </nav>
    <div id="appearance-panel-theme" v-show="view === 'theme'" role="tabpanel" aria-labelledby="appearance-tab-theme"><ThemeSettings /></div>
    <section id="appearance-panel-profile" v-if="view === 'profile'" role="tabpanel" aria-labelledby="appearance-tab-profile">
      <form class="mc-profile-form" @submit.prevent="saveProfile">
        <div class="profile-avatar-row"><span class="mc-avatar large"><img v-if="profile.avatar" :src="profile.avatar" alt="个人头像" /><UserRound v-else :size="36" /></span><button type="button" class="mc-icon" :disabled="busy" title="更换头像" aria-label="更换头像" @click="editAvatar"><ImagePlus :size="21" /></button></div>
        <label>昵称<input v-model="draft.nickname" maxlength="40" :disabled="busy" required /></label><label>个人简介<textarea v-model="draft.bio" maxlength="200" rows="3" :disabled="busy" /></label>
        <div class="mc-profile-stats"><span><strong>{{ counts.favorites }}</strong>喜欢</span><span><strong>{{ counts.history }}</strong>已听歌曲</span><span><strong>{{ playlists.length }}</strong>歌单</span></div>
        <button class="mc-command primary" :disabled="busy || !draft.nickname.trim() || !profileDirty" type="submit"><LoaderCircle v-if="busy" :size="17" class="spin" /><Save v-else :size="17" />保存资料</button>
      </form>
    </section>
    <section id="appearance-panel-tracks" v-if="view === 'tracks'" class="appearance-cover-settings" role="tabpanel" aria-labelledby="appearance-tab-tracks">
      <div class="appearance-section-title"><h2>歌曲封面</h2><button class="mc-icon" type="button" :disabled="loading" title="刷新歌曲" aria-label="刷新歌曲" @click="loadTracks"><LoaderCircle v-if="loading" :size="18" class="spin" /><RefreshCw v-else :size="18" /></button></div>
      <label class="appearance-selector"><span>搜索歌曲</span><div class="appearance-search"><Search :size="17" /><input v-model="query" type="search" aria-label="搜索封面歌曲" /></div></label>
      <label class="appearance-selector"><span>歌曲</span><select v-model="selectedKey" aria-label="选择封面歌曲" :disabled="busy"><option value="">{{ loading ? '正在加载歌曲' : '选择歌曲' }}</option><option v-for="track in filteredTracks" :key="trackKey(track)" :value="trackKey(track)">{{ track.title }}{{ track.artist || track.uploader ? ` - ${track.artist || track.uploader}` : '' }}</option></select></label>
      <div v-if="selectedTrack" class="appearance-cover-row"><TrackCover :src="trackArtwork(selectedTrack)" :title="selectedTrack.title" /><div><strong>{{ selectedTrack.title }}</strong><span>{{ selectedTrack.artist || selectedTrack.uploader || '未知歌手' }}</span></div><button class="mc-icon" type="button" :disabled="busy" title="更换歌曲封面" aria-label="更换歌曲封面" @click="editTrackCover"><LoaderCircle v-if="busy" :size="21" class="spin" /><ImagePlus v-else :size="21" /></button></div>
      <p v-else-if="!loading && !filteredTracks.length" class="appearance-empty">{{ query ? '没有匹配的歌曲' : '暂无歌曲' }}</p>
    </section>
    <section id="appearance-panel-playlists" v-if="view === 'playlists'" class="appearance-cover-settings" role="tabpanel" aria-labelledby="appearance-tab-playlists">
      <div class="appearance-section-title"><h2>歌单封面</h2></div>
      <label class="appearance-selector"><span>歌单</span><select v-model="playlistId" aria-label="选择封面歌单" :disabled="!playlists.length"><option value="">选择歌单</option><option v-for="playlist in playlists" :key="playlist.id" :value="playlist.id">{{ playlist.name }}</option></select></label>
      <div v-if="selectedPlaylist" class="appearance-cover-row"><TrackCover :src="selectedPlaylist.thumbnail" :title="selectedPlaylist.name" /><div><strong>{{ selectedPlaylist.name }}</strong><span>{{ selectedPlaylist.count }} 首歌曲</span></div><button class="mc-icon" type="button" title="更换歌单封面" aria-label="更换歌单封面" @click="editPlaylistCover"><ImagePlus :size="21" /></button></div>
      <p v-if="!playlists.length" class="appearance-empty">暂无歌单</p>
    </section>
    <p v-if="error" class="appearance-error" role="alert">{{ error }}</p><p v-if="status" class="appearance-status" role="status">{{ status }}</p>
    <ImageSettings v-if="editor" :title="editor.title" :endpoint="editor.endpoint" :image="editor.image" @saved="imageSaved" @close="editor = null" />
  </div>
</template>
