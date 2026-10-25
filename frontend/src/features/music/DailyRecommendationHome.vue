<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ChevronRight, Play, Heart } from 'lucide-vue-next'
import { playTrack } from './player/musicPlayer'
import { trackKey, trackArtwork, toggleFavorite, type MusicTrack } from './musicLibrary'
import TrackCover from './components/TrackCover.vue'
import MusicListControls from './components/MusicListControls.vue'
import { useMusicList } from './useMusicList'
import { useDailyFeed, useFeedExposure } from './useDailyFeed'
const emit = defineEmits<{ open: [] }>()
const { feed, busy, feedbackBusy, loading, error, notice, canChange, change, reduceRecommendation } = useDailyFeed()
const section = ref<HTMLElement | null>(null), message = ref('')
const { filters, artists, filtered: tracks, hasTime } = useMusicList(() => feed.value?.tracks || [], () => `daily-home:${feed.value?.feed_id || 'draft'}`)
const cards = computed(() => tracks.value.slice(0, 6))
const pages = computed(() => Array.from({ length: Math.ceil(cards.value.length / 3) }, (_, index) => cards.value.slice(index * 3, index * 3 + 3)))
const carousel = ref<HTMLElement | null>(null), activePage = ref(0)
useFeedExposure(section, () => cards.value, () => feed.value?.feed_id)
function scrollPage(index: number, behavior: ScrollBehavior = 'smooth') {
  const strip = carousel.value, card = strip?.children[index] as HTMLElement | undefined
  if (!strip || !card) return
  activePage.value = index
  strip.scrollTo({ left: card.offsetLeft, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : behavior })
}
function pageScrolled() {
  const strip = carousel.value
  if (!strip || strip.scrollWidth <= strip.clientWidth) return
  const left = strip.getBoundingClientRect().left
  const distances = [...strip.children].map(card => Math.abs(card.getBoundingClientRect().left - left))
  activePage.value = distances.indexOf(Math.min(...distances))
}
watch(() => cards.value.map(trackKey), async (keys, previous) => {
  const sameList = previous?.some(key => keys.includes(key))
  const index = sameList ? Math.min(activePage.value, pages.value.length - 1) : 0
  activePage.value = Math.max(0, index)
  await nextTick()
  scrollPage(activePage.value, 'auto')
})
async function feedback(track: MusicTrack) {
  try { if (await reduceRecommendation(track, 'today')) message.value = '今天减少推荐这首歌。' }
  catch (e) { message.value = String(e) }
}
</script>
<template>
  <section ref="section" class="mc-home-section daily-home" aria-label="今日推荐"><div class="mc-section-heading daily-home-heading"><h2>今日推荐</h2><div class="daily-home-actions"><button class="mc-text-button" :disabled="!tracks.length" @click="playTrack(tracks[0]!, tracks)"><Play :size="15" />播放全部</button><button class="mc-text-button" :disabled="!canChange" @click="change()">{{ busy ? '正在换歌…' : '换一批' }}</button><button class="mc-text-button" @click="emit('open')">查看全部<ChevronRight :size="16" /></button></div></div>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p><p v-if="message" role="status">{{ message }}</p>
    <MusicListControls v-if="feed?.tracks.length" v-model="filters" :artists="artists" :total="feed.tracks.length" :count="tracks.length" :has-time="hasTime" />
    <p v-if="feed?.tracks.length && !tracks.length" role="status">没有匹配的歌曲，请调整筛选条件。</p>
    <div v-if="cards.length" ref="carousel" class="mc-home-tracks daily-carousel" role="group" aria-label="推荐歌曲" @scroll.passive="pageScrolled" @keydown.left.prevent="scrollPage(Math.max(0, activePage - 1))" @keydown.right.prevent="scrollPage(Math.min(pages.length - 1, activePage + 1))">
      <div v-for="(page, pageIndex) in pages" :key="pageIndex" class="daily-carousel-page" role="group" :aria-label="`第 ${pageIndex + 1} 页，共 ${pages.length} 页`">
        <div v-for="(track, trackIndex) in page" :key="trackKey(track)" :data-key="track.key" class="mc-home-track daily-home-track" role="group" :aria-label="`第 ${pageIndex * 3 + trackIndex + 1} 首，共 ${cards.length} 首`">
          <button @click="playTrack(track, tracks)"><TrackCover class="daily-home-art" :src="trackArtwork(track)" :title="track.title" /><span><strong>{{ track.title }}</strong><small>{{ track.artist }}</small></span></button>
          <div class="daily-home-track-actions"><button class="mc-icon" :aria-label="`播放 ${track.title}`" @click="playTrack(track, tracks)"><Play :size="17" /></button><button class="mc-icon" :aria-label="`收藏或取消收藏 ${track.title}`" @click="toggleFavorite(track)"><Heart :size="17" /></button><button class="mc-icon daily-feedback" :disabled="busy || feedbackBusy" :aria-label="`今天不想听 ${track.title}`" @click="feedback(track)">减少推荐</button></div>
        </div>
      </div>
    </div>
    <nav v-if="cards.length" class="daily-carousel-pagination" aria-label="切换推荐分组"><div class="daily-carousel-dots"><button v-for="(_, index) in pages" :key="index" :aria-label="`查看第 ${index + 1} 页推荐`" :aria-current="activePage === index ? 'true' : undefined" @click="scrollPage(index)"><span /></button></div><span class="daily-carousel-count" aria-live="polite">{{ activePage + 1 }} / {{ pages.length }}</span></nav>
    <div v-else class="daily-home-empty" role="status"><span v-if="loading">正在获取推荐…</span><button v-if="feed?.reason === 'cold_start'" class="mc-text-button" @click="emit('open')">设置偏好<ChevronRight :size="16" /></button><button v-else-if="feed?.reason === 'exhausted'" class="mc-text-button" :disabled="!canChange" @click="change({ replay: true })">重听推荐</button></div>
  </section>
</template>
<style scoped>
.daily-home-heading, .daily-home-actions, .daily-home-track-actions { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }.daily-home-actions button:disabled { opacity: .45; cursor: default; }.daily-home-empty { display: flex; align-items: center; min-height: 90px; color: var(--mc-muted); }.daily-home-track { min-width: 0; }.daily-home-track-actions { gap: 2px; flex-wrap: nowrap; flex-shrink: 0; }.daily-feedback { font-size: 11px; width: auto; padding-inline: 6px; }.daily-home :deep(.mc-home-track > button:first-child) { min-width: 0; }
.daily-carousel { position: relative; }.daily-carousel-page { display: contents; }.daily-carousel-pagination { display: none; }
@media(max-width:760px) {
  .daily-home-art { display: none; }
  .daily-home .daily-carousel { display: flex; gap: 12px; overflow-x: auto; overscroll-behavior-x: contain; scroll-snap-type: x mandatory; scrollbar-width: none; border-radius: 14px; }
  .daily-carousel::-webkit-scrollbar { display: none; }
  .daily-carousel-page { display: grid; align-content: start; box-sizing: border-box; flex: 0 0 100%; min-width: 0; border: 1px solid var(--music-line, #e4e9df); border-radius: 14px; background: var(--music-surface, #fff); scroll-snap-align: start; scroll-snap-stop: always; }
  .daily-home .daily-home-track { gap: 8px; min-height: 76px; padding: 8px 12px; box-sizing: border-box; }
  .daily-home-track:last-child { border-bottom: 0; }
  .daily-home .daily-home-track > button:first-child { min-height: 56px; }
  .daily-home .daily-home-track strong { font-size: 14px; line-height: 1.5; }
  .daily-home .daily-home-track small { font-size: 12px; }
  .daily-home-track-actions { justify-content: flex-end; gap: 2px; }
  .daily-home-track-actions button { min-width: 44px; min-height: 44px; }
  .daily-home-track-actions .daily-feedback { font-size: 11px; padding-inline: 6px; }
  .daily-carousel-pagination { display: flex; align-items: center; justify-content: center; gap: 12px; min-height: 44px; margin-top: 4px; }
  .daily-carousel-dots { display: flex; }
  .daily-carousel-dots button { display: grid; place-items: center; width: 44px; min-height: 44px; padding: 0; border: 0; background: transparent; }
  .daily-carousel-dots span { width: 6px; height: 6px; border-radius: 50%; background: var(--music-muted, #7a827e); opacity: .35; transition: width .15s, opacity .15s; }
  .daily-carousel-dots button[aria-current=true] span { width: 18px; border-radius: 6px; background: var(--mc-green); opacity: 1; }
  .daily-carousel-count { color: var(--music-muted, #7a827e); font-size: 12px; font-variant-numeric: tabular-nums; }
  .daily-carousel-dots button:focus-visible { outline: 2px solid var(--mc-green); outline-offset: -4px; border-radius: 8px; }
}
@media(prefers-reduced-motion:reduce) { .daily-carousel-dots span { transition: none; } }
</style>
