<script setup lang="ts">
import { activeDownloads, downloadNotice } from './downloads'
import { onBeforeUnmount, watch } from 'vue'
let noticeTimer: ReturnType<typeof setTimeout> | undefined
watch(downloadNotice, value => {
  clearTimeout(noticeTimer)
  if (value) noticeTimer = setTimeout(() => { downloadNotice.value = '' }, 6000)
})
onBeforeUnmount(() => clearTimeout(noticeTimer))
</script>

<template>
  <aside v-if="activeDownloads.size || downloadNotice" class="download-tray" aria-label="本机下载" aria-live="polite">
    <div v-for="[id, download] in activeDownloads" :key="id"><strong>{{ download.label }}</strong><p>{{ download.progress }}</p><progress v-if="download.detail" :value="download.detail.phase === 'transferring' && download.detail.total ? Math.min(99, download.detail.bytes / download.detail.total * 100) : undefined" max="100" :aria-label="`${download.label} 保存进度`" /><button type="button" @click="download.cancel">取消下载</button></div>
    <div v-if="downloadNotice"><span>{{ downloadNotice }}</span><button type="button" aria-label="关闭下载提示" @click="downloadNotice = ''">关闭</button></div>
  </aside>
</template>

<style scoped>
.download-tray { position: fixed; right: 16px; bottom: 100px; z-index: 1100; box-sizing: border-box; width: min(400px, calc(100vw - 32px)); max-height: 30vh; overflow: auto; padding: 14px; color: var(--music-text, #203044); background: var(--music-surface, #fff); border: 1px solid var(--music-line, #ccd6df); border-radius: 8px; box-shadow: 0 4px 20px #0002; font-size: 13px; }
.download-tray strong { overflow-wrap: anywhere; }
.download-tray p { margin: 6px 0; overflow-wrap: anywhere; }
.download-tray > div + div { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--music-line, #ccd6df); }
.download-tray progress { display: block; width: 100%; height: 6px; margin: 10px 0 6px; accent-color: var(--music-accent, #228c74); }
.download-tray button { margin: 6px 0 0 8px; padding: 4px 8px; cursor: pointer; color: inherit; background: transparent; border: 1px solid #abb7c5; border-radius: 4px; }
@media (max-width: 760px) { .download-tray { bottom: calc(90px + env(safe-area-inset-bottom)); } :global(body:has(.has-music-player)) .download-tray { bottom: calc(170px + env(safe-area-inset-bottom)); } }
</style>
