<script setup lang="ts">
import DownloadTray from '../shared/DownloadTray.vue'
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import MusicCenter from '../features/music/MusicCenter.vue'
import MusicPlayer from '../features/music/player/MusicPlayer.vue'
import { currentTrack, pausePlayback } from '../features/music/player/musicPlayer'
import type { MusicTrack } from '../features/music/musicLibrary'
import VideoWorkspace from '../features/video/VideoWorkspace.vue'
import AdminWorkspace from '../features/admin/AdminWorkspace.vue'

const appPage = ref<'music' | 'tools' | 'admin'>('music')
const musicCenter = ref<InstanceType<typeof MusicCenter> | null>(null)
const videoWorkspace = ref<InstanceType<typeof VideoWorkspace> | null>(null)
const keyboardOpen = ref(false)
let viewportHeight = window.visualViewport?.height || window.innerHeight
function updateKeyboard() {
  const viewport = window.visualViewport
  const mobile = window.matchMedia('(max-width: 760px)').matches
  const editable = document.activeElement instanceof HTMLElement && document.activeElement.matches('input:not([type=range]):not([type=checkbox]):not([type=file]), textarea, [contenteditable=true]')
  const height = viewport?.height || window.innerHeight
  if (!editable) viewportHeight = Math.max(height, window.innerHeight)
  keyboardOpen.value = mobile && Boolean(editable) && viewportHeight - height > 120
}
function onOrientationChange() { viewportHeight = window.innerHeight; updateKeyboard() }
onMounted(() => {
  window.visualViewport?.addEventListener('resize', updateKeyboard)
  window.addEventListener('resize', updateKeyboard)
  window.addEventListener('orientationchange', onOrientationChange)
  document.addEventListener('focusin', updateKeyboard)
  document.addEventListener('focusout', updateKeyboard)
})
onBeforeUnmount(() => {
  window.visualViewport?.removeEventListener('resize', updateKeyboard)
  window.removeEventListener('resize', updateKeyboard)
  window.removeEventListener('orientationchange', onOrientationChange)
  document.removeEventListener('focusin', updateKeyboard)
  document.removeEventListener('focusout', updateKeyboard)
})

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
  <DownloadTray />
  <div class="page-shell" :class="{ 'music-page': appPage === 'music', 'has-music-player': Boolean(currentTrack), 'mobile-keyboard-open': keyboardOpen }">
    <div v-show="appPage === 'music'"><MusicCenter ref="musicCenter" :active="appPage === 'music'" @open-tools="openMusicTools" @open-admin="pausePlayback(); appPage = 'admin'" /></div>
    <AdminWorkspace v-if="appPage === 'admin'" @back="appPage = 'music'" />
    <div v-show="appPage === 'tools'" @play.capture="onToolsPlay"><VideoWorkspace ref="videoWorkspace" @activate="appPage = 'tools'" @back="returnMusicCenter" /></div>
    <div v-show="appPage !== 'admin'"><MusicPlayer @download="downloadFromPlayer" /></div>
  </div>
</template>
