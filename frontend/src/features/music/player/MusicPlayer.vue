<script setup lang="ts">
import { computed, ref } from 'vue'
import { Heart, ListMusic, LoaderCircle, Music2, Pause, Play, Repeat, Repeat1, Shuffle, SkipBack, SkipForward, Volume2, VolumeX, X, Download, AlignLeft, ChevronUp, Trash2 } from 'lucide-vue-next'
import TrackCover from '../components/TrackCover.vue'
import LyricsView from '../components/LyricsView.vue'
import { duration, isLiked, providers, toggleFavorite, trackArtwork, trackKey, type MusicTrack } from '../musicLibrary'
import { audio, currentTrack, elapsed, total, muted, volume, playMode, playbackError, playbackWarning, playbackState, playbackUrl, queue,
  togglePlayback, playTrack, nextTrack, seek, setVolume, toggleMute, stopPlayback, isPlaybackTrack,
  onMetadata, onTime, onPlaying, onPause, onWaiting, onError, onEnded } from './musicPlayer'

defineEmits<{ download: [track: MusicTrack] }>()
const showQueue = ref(false)
const showLyrics = ref(false)
const showVolume = ref(false)
const modes = ['list', 'loop', 'one', 'shuffle'] as const
const modeNames = { list: '顺序播放', loop: '列表循环', one: '单曲循环', shuffle: '随机播放' }
const modeIcon = computed(() => playMode.value === 'shuffle' ? Shuffle : playMode.value === 'one' ? Repeat1 : Repeat)
const playLabel = computed(() => playbackState.value === 'loading' ? '取消加载' : playbackState.value === 'playing' ? '暂停' : playbackState.value === 'error' ? '重试播放' : '播放')
const lyricTrack = computed(() => currentTrack.value ? { ...currentTrack.value, artist: currentTrack.value.artist || '',
  thumbnail: trackArtwork(currentTrack.value), filename: currentTrack.value.filename, download_url: currentTrack.value.local_url } : null)
function cycleMode() { playMode.value = modes[(modes.indexOf(playMode.value) + 1) % modes.length]! }
function removeQueueTrack(track: MusicTrack) {
  queue.value = queue.value.filter(item => trackKey(item) !== trackKey(track))
}
function closePlayer() { showQueue.value = false; showLyrics.value = false; showVolume.value = false; stopPlayback() }
function clearQueue() { closePlayer(); queue.value = [] }
function volumeClick() {
  if (window.matchMedia('(max-width: 760px)').matches) showVolume.value = !showVolume.value
  else toggleMute()
}
</script>

<template>
  <audio ref="audio" :src="playbackUrl || undefined" preload="metadata" @loadedmetadata="onMetadata" @durationchange="onMetadata"
    @timeupdate="onTime" @playing="onPlaying" @pause="onPause" @waiting="onWaiting" @error="onError" @ended="onEnded" />
  <footer v-if="currentTrack" class="global-player" :style="{ '--played': `${total ? elapsed / total * 100 : 0}%` }" aria-label="音乐播放器">
    <button class="player-art-button" :disabled="!currentTrack" aria-label="展开歌曲" title="展开歌曲" @click="showLyrics = true">
      <TrackCover :src="trackArtwork(currentTrack)" :title="currentTrack?.title" /><ChevronUp v-if="currentTrack" :size="18" />
    </button>
    <div class="player-track-info">
      <button class="player-track-details" aria-label="展开完整播放页" @click="showLyrics = true"><strong>{{ currentTrack.title }}</strong><span v-if="!playbackError && !playbackWarning">{{ currentTrack.artist || providers[currentTrack.provider || 'video'] }}</span></button>
      <small v-if="playbackError" class="player-error" role="alert">{{ playbackError }}</small>
      <small v-else-if="playbackWarning" class="player-warning">{{ playbackWarning }}</small>
    </div>
    <button class="mc-icon player-favorite" :class="{ liked: currentTrack && isLiked(currentTrack) }" :disabled="!currentTrack" :title="currentTrack && isLiked(currentTrack) ? '取消喜欢' : '喜欢'" :aria-label="currentTrack && isLiked(currentTrack) ? '取消喜欢' : '喜欢'" @click="currentTrack && toggleFavorite(currentTrack)"><Heart :size="20" :fill="currentTrack && isLiked(currentTrack) ? 'currentColor' : 'none'" /></button>
    <div class="player-center">
      <div class="player-transport">
        <button class="mc-icon" :title="modeNames[playMode]" :aria-label="modeNames[playMode]" @click="cycleMode"><component :is="modeIcon" :size="18" :class="{ 'mode-active': playMode !== 'list' }" /></button>
        <button class="mc-icon" :disabled="!queue.length" title="上一首" aria-label="上一首" @click="nextTrack(-1)"><SkipBack :size="21" fill="currentColor" /></button>
        <button class="player-main-button" :disabled="!currentTrack" :title="playbackState === 'playing' ? '暂停' : playbackState === 'error' ? '重试播放' : '播放'" :aria-label="playbackState === 'playing' ? '暂停' : playbackState === 'error' ? '重试播放' : '播放'" @click="togglePlayback()"><LoaderCircle v-if="playbackState === 'loading'" :size="23" class="spin" /><Pause v-else-if="playbackState === 'playing'" :size="23" fill="currentColor" /><Play v-else :size="23" fill="currentColor" /></button>
        <button class="mc-icon" :disabled="!queue.length" title="下一首" aria-label="下一首" @click="nextTrack()"><SkipForward :size="21" fill="currentColor" /></button>
        <button class="mc-icon" :disabled="!currentTrack" title="下载歌曲" aria-label="下载歌曲" @click="currentTrack && $emit('download', currentTrack)"><Download :size="18" /></button>
      </div>
      <div class="player-progress"><time>{{ duration(elapsed) }}</time><input type="range" min="0" :max="total || 1" step="0.1" :value="elapsed" :disabled="!playbackUrl || !total" aria-label="播放进度" :style="{ '--progress': `${total ? elapsed / total * 100 : 0}%` }" @input="seek(Number(($event.target as HTMLInputElement).value))" /><time>{{ duration(total) }}</time></div>
    </div>
    <div class="player-extras">
      <button class="mc-icon" title="音量" aria-label="音量设置" :aria-expanded="showVolume" @click="volumeClick"><VolumeX v-if="muted || volume === 0" :size="20" /><Volume2 v-else :size="20" /></button>
      <input type="range" class="player-volume" min="0" max="1" step="0.01" :value="muted ? 0 : volume" aria-label="音量" @input="setVolume(Number(($event.target as HTMLInputElement).value))" />
      <button class="mc-icon" :disabled="!currentTrack" title="歌词" aria-label="歌词" @click="showLyrics = true"><AlignLeft :size="20" /></button>
      <button class="mc-icon" :class="{ active: showQueue }" :aria-expanded="showQueue" title="播放队列" aria-label="播放队列" @click="showQueue = !showQueue"><ListMusic :size="22" /></button>
    </div>
    <div class="player-mobile-controls"><button class="player-mobile-toggle" :aria-label="playLabel" :aria-busy="playbackState === 'loading'" @click="togglePlayback()"><span class="player-main-button"><LoaderCircle v-if="playbackState === 'loading'" :size="18" class="spin" /><Pause v-else-if="playbackState === 'playing'" :size="18" fill="currentColor" /><Play v-else :size="18" fill="currentColor" /></span></button><button class="mc-icon" :aria-expanded="showQueue" aria-label="播放队列" @click="showQueue = !showQueue"><ListMusic :size="21" /></button><button class="mc-icon" aria-label="停止播放并关闭" @click="closePlayer"><X :size="19" /></button></div>
  </footer>
  <div v-if="showVolume" class="player-volume-popup" aria-label="调节音量"><button class="mc-icon" :title="muted ? '取消静音' : '静音'" :aria-label="muted ? '取消静音' : '静音'" @click="toggleMute"><VolumeX v-if="muted || volume === 0" :size="18" /><Volume2 v-else :size="18" /></button><input type="range" min="0" max="1" step="0.01" :value="muted ? 0 : volume" aria-label="调节音量" @input="setVolume(Number(($event.target as HTMLInputElement).value))" /><span>{{ Math.round((muted ? 0 : volume) * 100) }}%</span><button class="mc-icon" title="关闭音量设置" aria-label="关闭音量设置" @click="showVolume = false"><X :size="16" /></button></div>
  <aside v-if="showQueue" class="player-queue" aria-label="播放队列">
    <div class="queue-heading"><h2>播放队列 <span>{{ queue.length }}</span></h2><button class="mc-icon" :disabled="!queue.length" title="清空播放队列" aria-label="清空播放队列" @click="clearQueue"><Trash2 :size="17" /></button><button class="mc-icon" title="关闭播放队列" aria-label="关闭播放队列" @click="showQueue = false"><X :size="19" /></button></div>
    <div v-if="!queue.length" class="mc-empty"><Music2 :size="30" /><p>播放队列为空</p></div>
    <ol v-else><li v-for="track in queue" :key="trackKey(track)" :class="{ active: isPlaybackTrack(track) }"><button class="queue-song" @click="playTrack(track)"><TrackCover :src="trackArtwork(track)" :title="track.title" /><span><strong>{{ track.title }}</strong><small>{{ track.artist || providers[track.provider || 'video'] }}</small></span><Play :size="15" /></button><button class="mc-icon" :title="`移出队列 ${track.title}`" :aria-label="`移出队列 ${track.title}`" @click="removeQueueTrack(track)"><X :size="15" /></button></li></ol>
  </aside>
  <LyricsView v-if="showLyrics && lyricTrack" :track="lyricTrack" :current-time="elapsed" @close="showLyrics = false" @seek="seek">
    <template #controls>
      <p v-if="playbackError || playbackWarning" class="player-full-feedback" :role="playbackError ? 'alert' : 'status'">{{ playbackError || playbackWarning }}</p>
      <div class="lyrics-player-controls"><button class="mc-icon" :title="modeNames[playMode]" :aria-label="modeNames[playMode]" @click="cycleMode"><component :is="modeIcon" :size="20" /></button><button class="mc-icon" title="上一首" aria-label="上一首" @click="nextTrack(-1)"><SkipBack :size="23" /></button><button class="player-main-button" :aria-label="playLabel" @click="togglePlayback()"><Pause v-if="playbackState === 'playing'" :size="23" fill="currentColor" /><LoaderCircle v-else-if="playbackState === 'loading'" :size="23" class="spin" /><Play v-else :size="23" fill="currentColor" /></button><button class="mc-icon" title="下一首" aria-label="下一首" @click="nextTrack()"><SkipForward :size="23" /></button><button class="mc-icon" :aria-label="currentTrack && isLiked(currentTrack) ? '取消喜欢' : '喜欢'" :class="{ liked: currentTrack && isLiked(currentTrack) }" @click="currentTrack && toggleFavorite(currentTrack)"><Heart :size="20" :fill="currentTrack && isLiked(currentTrack) ? 'currentColor' : 'none'" /></button></div>
      <div class="player-progress"><time>{{ duration(elapsed) }}</time><input type="range" min="0" :max="total || 1" step="0.1" :value="elapsed" :disabled="!playbackUrl || !total" aria-label="歌词页播放进度" @input="seek(Number(($event.target as HTMLInputElement).value))" /><time>{{ duration(total) }}</time></div>
      <div class="player-full-actions"><label><Volume2 :size="18" /><input type="range" min="0" max="1" step="0.01" :value="muted ? 0 : volume" aria-label="完整播放页音量" @input="setVolume(Number(($event.target as HTMLInputElement).value))" /></label><button class="mc-icon" aria-label="下载当前歌曲" @click="currentTrack && $emit('download', currentTrack); showLyrics = false"><Download :size="19" /></button></div>
    </template>
  </LyricsView>
</template>
