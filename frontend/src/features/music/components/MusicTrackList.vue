<script setup lang="ts">
import { Heart, MoreHorizontal, Pause, Play, LoaderCircle, Music2 } from 'lucide-vue-next'
import TrackCover from './TrackCover.vue'
import { duration, isLiked, providers, toggleFavorite, trackArtwork, trackKey, type MusicTrack } from '../musicLibrary'
import { isPlaybackTrack, playbackState, togglePlayback } from '../player/musicPlayer'
defineProps<{ tracks: MusicTrack[]; empty?: string }>()
defineEmits<{ more: [track: MusicTrack, event: MouseEvent] }>()
</script>

<template>
  <div v-if="!tracks.length" class="mc-empty"><Music2 :size="34" /><p>{{ empty || '暂无歌曲' }}</p></div>
  <div v-else class="mc-track-table" role="table" aria-label="歌曲列表">
    <div class="mc-track-table-head" role="row"><span>#</span><span>歌曲</span><span>来源</span><span>时长</span><span /></div>
    <div v-for="(track, index) in tracks" :key="trackKey(track)" class="mc-track-row" :class="{ playing: isPlaybackTrack(track) }" role="row">
      <button class="mc-icon track-number-play" :title="`播放 ${track.title}`" :aria-label="`播放 ${track.title}`" @click="togglePlayback(track, tracks)"><LoaderCircle v-if="isPlaybackTrack(track) && playbackState === 'loading'" :size="18" class="spin" /><Pause v-else-if="isPlaybackTrack(track) && playbackState === 'playing'" :size="17" /><Play v-else :size="17" /><span>{{ String(index + 1).padStart(2, '0') }}</span></button>
      <button class="mc-track-title" @click="togglePlayback(track, tracks)"><TrackCover :src="trackArtwork(track)" :title="track.title" /><span><strong>{{ track.title }}</strong><small>{{ track.artist || '未知歌手' }}<em v-if="track.preview">试听</em></small><small class="mc-mobile-file-state" v-if="track.save_unverified || track.local_url || track.filename">{{ track.save_unverified ? '旧保存记录待核实' : track.local_url ? '本地可播放' : '文件缺失' }}</small></span></button>
      <span class="mc-track-source">{{ providers[track.provider || 'video'] }}<small v-if="track.save_unverified">旧保存记录待核实</small><small v-else-if="track.local_url">资料库可播放</small><small v-else-if="track.saved_to_device">本机保存记录</small><small v-else-if="track.filename" class="file-missing">文件缺失</small></span>
      <time>{{ duration(track.duration) }}</time>
      <div class="mc-track-actions"><button class="mc-icon" :class="{ liked: isLiked(track) }" :title="isLiked(track) ? '取消喜欢' : '喜欢'" :aria-label="`${isLiked(track) ? '取消喜欢' : '喜欢'} ${track.title}`" @click="toggleFavorite(track)"><Heart :size="17" :fill="isLiked(track) ? 'currentColor' : 'none'" /></button><button class="mc-icon" :title="`更多操作 ${track.title}`" :aria-label="`更多操作 ${track.title}`" @click="$emit('more', track, $event)"><MoreHorizontal :size="19" /></button></div>
    </div>
  </div>
</template>
