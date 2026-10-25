<script setup lang="ts">
import { isMusicDownloaded } from '../musicDownloads'
import { Heart, MoreHorizontal, Pause, Play, LoaderCircle, Music2 } from 'lucide-vue-next'
import TrackCover from './TrackCover.vue'
import MusicListControls from './MusicListControls.vue'
import { useMusicList } from '../useMusicList'
import { formatMusicListTime } from '../musicList'
import { duration, isLiked, providers, toggleFavorite, trackArtwork, trackKey, type MusicTrack } from '../musicLibrary'
import { isPlaybackTrack, playbackState, playTrack, togglePlayback } from '../player/musicPlayer'
const props = withDefaults(defineProps<{ tracks: MusicTrack[]; empty?: string; scope: string; timeLabel?: string; playAll?: boolean }>(), { timeLabel: '记录时间', playAll: false })
const { filters, artists, filtered, hasTime, cycleSort } = useMusicList(() => props.tracks, () => props.scope)
defineEmits<{ more: [track: MusicTrack, event: MouseEvent] }>()
</script>

<template>
  <MusicListControls v-model="filters" :artists="artists" :total="tracks.length" :count="filtered.length" :has-time="hasTime" :time-label="timeLabel" />
  <div v-if="playAll" class="mc-list-toolbar"><button type="button" class="mc-command primary" :disabled="!filtered.length" @click="playTrack(filtered[0]!, filtered)"><Play :size="17" fill="currentColor" />播放全部</button></div>
  <div v-if="!tracks.length" class="mc-empty"><Music2 :size="34" /><p>{{ empty || '暂无歌曲' }}</p></div>
  <div v-else-if="!filtered.length" class="mc-empty" role="status"><Music2 :size="34" /><p>没有匹配的歌曲，请调整筛选条件</p></div>
  <div v-else class="mc-track-table mc-track-table-timed" role="table" aria-label="歌曲列表">
    <div class="mc-track-table-head" role="row"><span>#</span><span>歌曲</span><span role="columnheader" :aria-sort="filters.sort === 'newest' ? 'descending' : filters.sort === 'oldest' ? 'ascending' : 'none'"><button type="button" class="mc-time-sort" :disabled="!hasTime" @click="cycleSort">{{ timeLabel }} {{ filters.sort === 'newest' ? '↓' : filters.sort === 'oldest' ? '↑' : '↕' }}</button></span><span>来源</span><span>时长</span><span /></div>
    <div v-for="(track, index) in filtered" :key="trackKey(track)" class="mc-track-row" :class="{ playing: isPlaybackTrack(track) }" role="row">
      <button class="mc-icon track-number-play" :title="`播放 ${track.title}`" :aria-label="`播放 ${track.title}`" @click="togglePlayback(track, filtered)"><LoaderCircle v-if="isPlaybackTrack(track) && playbackState === 'loading'" :size="18" class="spin" /><Pause v-else-if="isPlaybackTrack(track) && playbackState === 'playing'" :size="17" /><Play v-else :size="17" /><span>{{ String(index + 1).padStart(2, '0') }}</span></button>
      <button class="mc-track-title" @click="togglePlayback(track, filtered)"><TrackCover :src="trackArtwork(track)" :title="track.title" /><span><strong>{{ track.title }}</strong><small>{{ track.artist || '未知歌手' }}<em v-if="track.preview">试听</em></small><small class="mc-mobile-list-time">{{ timeLabel }}：{{ formatMusicListTime(track.list_time) }}</small><small class="mc-mobile-file-state" v-if="track.save_unverified || track.device_file || track.server_url || track.filename">{{ track.save_unverified ? '旧保存记录待核实' : isMusicDownloaded(track) ? '本设备可播放' : track.server_url ? '服务器音源' : '文件需重新定位' }}</small></span></button>
      <time class="mc-list-time" :title="formatMusicListTime(track.list_time)">{{ formatMusicListTime(track.list_time) }}</time>
      <span class="mc-track-source">{{ providers[track.provider || 'video'] }}<small v-if="track.save_unverified">旧保存记录待核实</small><small v-else-if="isMusicDownloaded(track)">本设备可播放</small><small v-else-if="track.saved_to_device">本设备保存记录</small><small v-else-if="track.filename" class="file-missing">文件缺失</small></span>
      <time>{{ duration(track.duration) }}</time>
      <div class="mc-track-actions"><button class="mc-icon" :class="{ liked: isLiked(track) }" :title="isLiked(track) ? '取消喜欢' : '喜欢'" :aria-label="`${isLiked(track) ? '取消喜欢' : '喜欢'} ${track.title}`" @click="toggleFavorite(track)"><Heart :size="17" :fill="isLiked(track) ? 'currentColor' : 'none'" /></button><button class="mc-icon" :title="`更多操作 ${track.title}`" :aria-label="`更多操作 ${track.title}`" @click="$emit('more', track, $event)"><MoreHorizontal :size="19" /></button></div>
    </div>
  </div>
</template>
