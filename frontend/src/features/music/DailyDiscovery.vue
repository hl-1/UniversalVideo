<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api, jsonRequest } from '../../shared/api/client'
import { type MusicTrack, toggleFavorite, trackPayload } from './musicLibrary'
import { playTrack } from './player/musicPlayer'
import TrackCover from './components/TrackCover.vue'
import MusicListControls from './components/MusicListControls.vue'
import MyMusicProfile from './MyMusicProfile.vue'
import { useMusicList } from './useMusicList'
import { useDailyFeed, useFeedExposure } from './useDailyFeed'
import { selectListeningContext, type ListeningContext } from './discoveryEvents'
type Preferences = { languages: string[]; excluded_artists: string[]; seed_track_keys: string[]; long_term_need: string; exploration: string }
const { feed, busy, feedbackBusy, loading, error, notice, canChange, load, change, reduceRecommendation } = useDailyFeed()
const section = ref<HTMLElement | null>(null)
const { filters, artists, filtered: tracks, hasTime } = useMusicList(() => feed.value?.tracks || [], () => `daily:${feed.value?.feed_id || 'draft'}`)
useFeedExposure(section, () => tracks.value, () => feed.value?.feed_id)
const preferences = ref<Preferences>({ languages: [], excluded_artists: [], seed_track_keys: [], long_term_need: '', exploration: 'balanced' })
const need = ref(''), language = ref(''), remember = ref(false), rememberSimilar = ref(false), message = ref(''), context = ref<ListeningContext>('general')
const showAdjust = ref(false), showPreferences = ref(false), showBlocked = ref(false), seeds = ref<MusicTrack[]>([]), currentSeeds = ref<string[]>([])
const blocks = ref<Array<{ track_key: string; scope: string; reason: string; canonical: { title: string } }>>([])
function play(track: MusicTrack) { selectListeningContext(context.value); void playTrack(track, tracks.value) }
async function savePreferences() {
  const value = preferences.value
  await api('/api/music/discovery/profile', jsonRequest('PATCH', { languages: language.value.split(/[、,，]/).map(s => s.trim()).filter(Boolean),
    excluded_artists: value.excluded_artists, seed_track_keys: value.seed_track_keys,
    long_term_need: remember.value ? need.value : value.long_term_need, exploration: value.exploration }))
  message.value = '偏好已更新，下批生效。'; await load()
}
async function generate(replay = false) {
  try {
    if (remember.value) await savePreferences()
    await change({ need: need.value, seed_track_keys: [...new Set([...preferences.value.seed_track_keys, ...currentSeeds.value])].slice(0, 10), exploration: preferences.value.exploration, context: context.value, replay })
  } catch (e) { message.value = String(e) }
}
async function feedback(track: MusicTrack, reason: string, scope = 'work') {
  try {
    const result = await reduceRecommendation(track, reason, scope)
    if (result) message.value = result.notice || '反馈已生效。'
  } catch (e) { message.value = String(e) }
}
async function similar(track: MusicTrack) {
  try {
    await api('/api/music/discovery/events', jsonRequest('POST', { events: [{ event_id: crypto.randomUUID(), occurred_at: Date.now() / 1000,
      event_type: 'exposure', origin: 'web', track: trackPayload(track), track_key: track.key, feed_id: track.discovery!.feed_id, position: track.discovery!.position }] }))
    currentSeeds.value = [track.key!]
    if (rememberSimilar.value) { preferences.value.seed_track_keys = [...new Set([...preferences.value.seed_track_keys, track.key!])].slice(-10); await savePreferences() }
    message.value = '已选择为下一批种子，点击换一批。'
  } catch (e) { message.value = String(e) }
}
async function save() {
  if (!feed.value?.feed_id) return
  try { await api(`/api/music/discovery/feeds/${feed.value.feed_id}/save`, jsonRequest('POST', {})); message.value = '已保存到个人歌单。' }
  catch (e) { message.value = String(e) }
}
async function openPreferences() {
  showPreferences.value = !showPreferences.value
  if (showPreferences.value) {
    try { seeds.value = (await api<{ tracks: MusicTrack[] }>('/api/library/tracks?view=favorites')).tracks }
    catch (e) { message.value = String(e) }
  }
}
async function blocked() {
  showBlocked.value = !showBlocked.value
  if (showBlocked.value) blocks.value = (await api<{ items: typeof blocks.value }>('/api/music/discovery/feedback')).items
}
async function undo(item: typeof blocks.value[number]) {
  try { await api('/api/music/discovery/feedback', jsonRequest('DELETE', { track_key: item.track_key, scope: item.scope, reason: item.reason })); blocks.value = blocks.value.filter(x => x !== item); await load() }
  catch (e) { message.value = String(e) }
}
onMounted(async () => {
  try { const result = await api<{ settings: Preferences }>('/api/music/discovery/profile'); preferences.value = result.settings; language.value = result.settings.languages.join('、') }
  catch (e) { message.value = String(e) }
})
</script>
<template>
  <section ref="section" class="daily-discovery">
    <header><span class="daily-eyebrow">为你精选</span><h2>今天，听点喜欢的新声音</h2><p>根据你的收藏和收听偏好，遇见下一首喜欢的歌。</p></header>
    <div class="daily-actions daily-toolbar"><button class="daily-primary" :disabled="!tracks.length" @click="play(tracks[0]!)">▶ 播放全部</button><button :disabled="!canChange" @click="generate()">{{ busy ? '正在换歌…' : '换一批' }}</button><button @click="showAdjust = !showAdjust">调整推荐</button><button v-if="feed?.tracks.length" @click="save">保存歌单</button><button :disabled="!canChange" @click="generate(true)">重听推荐</button></div>
    <p v-if="loading" role="status">正在获取推荐…</p><p v-if="error" role="alert">{{ error }}<button @click="load()">重试</button></p><p v-if="notice" role="status">{{ notice }}</p><p v-if="message" role="status">{{ message }}</p>
    <p v-if="feed && feed.profile_revision !== feed.latest_profile_revision && !notice.includes('偏好已更新')" role="status">偏好已更新，下批生效。</p>
    <div v-if="showAdjust || feed?.reason === 'cold_start'" class="daily-panel"><label>今天想听什么？<input v-model="need" maxlength="1000" placeholder="例如：轻快一点的中文歌（选填）" /></label><label><input v-model="remember" type="checkbox" />长期记住这次需求</label><label>听歌场景<select v-model="context" @change="selectListeningContext(context)"><option value="general">通用</option><option value="focus">专注</option><option value="commute">通勤</option><option value="exercise">运动</option><option value="sleep">睡前</option></select></label><button :disabled="!canChange" @click="generate()">应用并推荐</button><small>偏好将在下批生效，当前播放可以继续。</small></div>
    <div v-if="showPreferences" class="daily-panel"><label>语言（用顿号分隔）<input v-model="language" /></label><label>探索程度<select v-model="preferences.exploration"><option value="low">稳妥</option><option value="balanced">均衡</option><option value="high">更多探索</option></select></label><p>选择最多 10 首收藏作为种子。</p><label v-for="track in seeds" :key="track.id"><input v-model="preferences.seed_track_keys" type="checkbox" :value="track.id" :disabled="preferences.seed_track_keys.length >= 10 && !preferences.seed_track_keys.includes(track.id!)" />{{ track.title }} · {{ track.artist }}</label><button @click="savePreferences().catch(e => message = String(e))">保存偏好</button></div>
    <div v-if="showBlocked" class="daily-panel"><p v-if="!blocks.length">暂无屏蔽记录</p><div v-for="item in blocks" :key="item.track_key + item.reason">{{ item.canonical.title }} · {{ item.reason }} · {{ item.scope === 'work' ? '作品' : '录音版本' }}<button @click="undo(item)">撤销</button></div></div>
    <MusicListControls v-if="feed?.tracks.length" v-model="filters" :artists="artists" :total="feed.tracks.length" :count="tracks.length" :has-time="hasTime" />
    <p v-if="feed?.tracks.length && !tracks.length" role="status">没有匹配的歌曲，请调整筛选条件。</p>
    <ol class="daily-tracks"><li v-for="track in tracks" :key="track.key" class="daily-track" :data-key="track.key"><div class="daily-song"><TrackCover class="daily-art" :src="track.thumbnail" :title="track.title" /><div><strong>{{ track.title }}</strong><span>{{ track.artist }}</span><p>{{ track.discovery?.reason }}</p></div></div><div class="daily-actions"><button @click="play(track)">播放</button><button @click="toggleFavorite(track)">收藏 / 取消</button><details><summary>反馈</summary><label><input v-model="rememberSimilar" type="checkbox" />长期记住相似偏好</label><button @click="similar(track)">多推荐类似的</button><button :disabled="busy || feedbackBusy" @click="feedback(track, 'dislike')">不喜欢这首歌</button><button :disabled="busy || feedbackBusy" @click="feedback(track, 'dislike', 'recording')">不喜欢这个版本</button><button :disabled="busy || feedbackBusy" @click="feedback(track, 'fatigue')">听腻了（90 天）</button><button :disabled="busy || feedbackBusy" @click="feedback(track, 'today')">今天不想听</button></details></div></li></ol>
    <footer class="daily-actions"><button @click="openPreferences">推荐偏好</button><button @click="blocked().catch(e => message = String(e))">已屏蔽歌曲</button></footer>
    <MyMusicProfile @updated="load()" />
  </section>
</template>
<style scoped>
.daily-discovery { color: var(--mc-ink); min-width: 0; overflow-wrap: anywhere; } h2 { margin: 8px 0; font-size: clamp(22px,3vw,30px); } header p, small { color: var(--mc-muted); line-height: 1.7; }.daily-eyebrow { color: var(--mc-green); font-size: 12px; }
.daily-actions { display: flex; flex-wrap: wrap; gap: 8px; }.daily-toolbar { margin-block: 24px; }.daily-primary { background: var(--mc-green); color: var(--music-accent-text,white); }
button, select, input:not([type=checkbox]), summary { min-height: 44px; max-width: 100%; padding: 8px 12px; box-sizing: border-box; border: 1px solid var(--music-line); border-radius: 8px; background: var(--music-surface); color: inherit; } button, summary { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }
.daily-panel { border: 1px solid var(--music-line); border-radius: 12px; padding: 16px; margin-block: 16px; } label { display: block; margin-block: 12px; } label input:not([type=checkbox]) { display: block; width: 100%; margin-top: 8px; } small { display: block; margin-top: 10px; }
.daily-tracks { list-style: none; padding: 0; }.daily-track { padding-block: 16px; border-bottom: 1px solid var(--music-line); display: flex; justify-content: space-between; gap: 16px; }.daily-song { display: flex; align-items: center; gap: 14px; min-width: 0; }.daily-art { width: 56px; height: 56px; flex-shrink: 0; border-radius: 10px; }.daily-song span { display: block; margin-top: 5px; font-size: 13px; }.daily-song p { font-size: 13px; color: var(--mc-muted); }.daily-track details button { display: block; width: 100%; }.daily-discovery footer { padding-block: 20px; }
@media(max-width:760px) { .daily-song .daily-art, .daily-song p { display: none; }.daily-track { flex-direction: column; }.daily-actions { gap: 6px; } }
</style>
