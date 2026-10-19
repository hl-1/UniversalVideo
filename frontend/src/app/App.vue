<script setup lang="ts">
import { nextTick, ref } from 'vue'
import MusicCenter from '../features/music/MusicCenter.vue'
import MusicPlayer from '../features/music/player/MusicPlayer.vue'
import { pausePlayback } from '../features/music/player/musicPlayer'
import type { MusicTrack } from '../features/music/musicLibrary'
import VideoWorkspace from '../features/video/VideoWorkspace.vue'
import AdminWorkspace from '../features/admin/AdminWorkspace.vue'

const appPage = ref<'music' | 'tools' | 'admin'>('music')
const musicCenter = ref<InstanceType<typeof MusicCenter> | null>(null)
const videoWorkspace = ref<InstanceType<typeof VideoWorkspace> | null>(null)

async function openMusicTools(view: 'video' | 'watermark') {
  appPage.value = 'tools'
  await nextTick()
  await videoWorkspace.value?.open(view)
}
function returnMusicCenter() {
  appPage.value = 'music'
  void nextTick(() => window.scrollTo({ top: 0 }))
}
function downloadFromPlayer(track: MusicTrack) {
  returnMusicCenter()
  void musicCenter.value?.download(track)
}
function onToolsPlay(event: Event) {
  if (event.target instanceof HTMLVideoElement) pausePlayback()
}
</script>

<template>
  <div class="page-shell" :class="{ 'music-page': appPage === 'music' }">
    <div v-show="appPage === 'music'"><MusicCenter ref="musicCenter" :active="appPage === 'music'" @open-tools="openMusicTools" @open-admin="pausePlayback(); appPage = 'admin'" /></div>
    <AdminWorkspace v-if="appPage === 'admin'" @back="appPage = 'music'" />
    <VideoWorkspace v-show="appPage === 'tools'" ref="videoWorkspace" @activate="appPage = 'tools'" @back="returnMusicCenter" @play.capture="onToolsPlay" />
    <div v-show="appPage !== 'admin'"><MusicPlayer @download="downloadFromPlayer" /></div>
  </div>
</template>
