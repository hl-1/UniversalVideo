<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ChevronDown, FileMusic, LoaderCircle, Music2, RefreshCw } from 'lucide-vue-next'
import { activeLyricIndex, audioFilename, type Lyrics } from './lyrics'

const props = defineProps<{
  track: { title: string; artist?: string | null; album?: string; thumbnail?: string | null; filename?: string | null; download_url?: string | null; webpage_url?: string; token?: string }
  currentTime: number
  lyricsEndpoint?: string
  resultVersion?: number
}>()
const emit = defineEmits<{ close: []; seek: [seconds: number] }>()
const lyrics = ref<Lyrics>({ lines: [], plain: '', source: '' })
const loading = ref(false)
const importing = ref(false)
const error = ref('')
const coverFailed = ref(false)
const lyricViewport = ref<HTMLElement | null>(null)
const dialog = ref<HTMLElement | null>(null)
const importInput = ref<HTMLInputElement | null>(null)
const filename = computed(() => props.track.filename || audioFilename(props.track.download_url))
const activeIndex = computed(() => activeLyricIndex(lyrics.value.lines, props.currentTime))
let request: AbortController | undefined
let generation = 0
let followAfter = 0
let previousFocus: HTMLElement | null = null
let previousOverflow = ''

async function apiError(response: Response) {
  try { return (await response.json()).detail || '歌词加载失败。' }
  catch { return `歌词加载失败（${response.status}）。` }
}
async function loadLyrics(fetchOnline = true) {
  request?.abort()
  const current = ++generation
  const name = filename.value
  error.value = ''
  if (!name && !props.lyricsEndpoint && !props.track.webpage_url) { loading.value = false; return }
  const track = { ...props.track }
  request = new AbortController()
  const signal = request.signal
  loading.value = true
  try {
    if (props.lyricsEndpoint || !name) {
      let endpoint = props.lyricsEndpoint || '/api/music/lyrics/online'
      let body: Record<string, unknown> = { result_version: props.resultVersion }
      if (!props.lyricsEndpoint) {
        const parsed = await fetch('/api/music/parse', { method: 'POST', signal,
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: track.webpage_url }) })
        if (!parsed.ok) throw new Error(await apiError(parsed))
        const data = await parsed.json()
        body = { token: data.token, webpage_url: data.webpage_url }
      }
      const response = await fetch(endpoint, { method: 'POST', signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      if (!response.ok) throw new Error(await apiError(response))
      const data = await response.json()
      if (current === generation) { lyrics.value = data; await followCurrentLine(true) }
      return
    }
    const response = await fetch(`/api/music/lyrics/${encodeURIComponent(name)}`, { signal })
    if (!response.ok) throw new Error(await apiError(response))
    const local: Lyrics = await response.json()
    if (current !== generation) return
    lyrics.value = local
    if (fetchOnline && !local.lines.length && !local.plain && track.artist) {
      const online = await fetch('/api/music/lyrics/fetch', { method: 'POST', signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: name, title: track.title, artist: track.artist, album: track.album || '' }) })
      if (!online.ok) throw new Error(await apiError(online))
      const data: Lyrics = await online.json()
      if (current === generation) lyrics.value = data
    }
    await followCurrentLine(true)
  } catch (failure) {
    if (current === generation && !(failure instanceof DOMException && failure.name === 'AbortError')) {
      error.value = failure instanceof Error ? failure.message : '歌词加载失败。'
    }
  } finally { if (current === generation) loading.value = false }
}
async function importLyrics(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || !filename.value) return
  if (file.size > 512000) { error.value = '歌词文件最大支持 500 KB。'; return }
  request?.abort()
  const current = ++generation
  loading.value = false
  importing.value = true
  error.value = ''
  request = new AbortController()
  try {
    const response = await fetch(`/api/music/lyrics/${encodeURIComponent(filename.value)}`, {
      method: 'PUT', body: file, signal: request.signal, headers: { 'Content-Type': 'text/plain' },
    })
    if (!response.ok) throw new Error(await apiError(response))
    const data: Lyrics = await response.json()
    if (current === generation) { lyrics.value = data; await followCurrentLine(true) }
  } catch (failure) {
    if (current === generation && !(failure instanceof DOMException && failure.name === 'AbortError')) {
      error.value = failure instanceof Error ? failure.message : '歌词导入失败。'
    }
  } finally { if (current === generation) importing.value = false }
}
async function followCurrentLine(force = false) {
  if (!force && Date.now() < followAfter) return
  await nextTick()
  const viewport = lyricViewport.value
  const line = viewport?.querySelector<HTMLElement>('[aria-current="true"]')
  if (viewport && line) viewport.scrollTo({ top: line.offsetTop - viewport.clientHeight / 2 + line.clientHeight / 2, behavior: force ? 'instant' : 'smooth' })
}
function seekLine(seconds: number) { followAfter = 0; emit('seek', seconds); void followCurrentLine(true) }
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); emit('close'); return }
  if (event.key !== 'Tab') return
  const buttons = Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not([type="file"]), [tabindex="0"]') || [])
  const first = buttons[0]
  const last = buttons[buttons.length - 1]
  if (!dialog.value?.contains(document.activeElement)) { event.preventDefault(); (event.shiftKey ? last : first)?.focus() }
  else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
watch(() => [filename.value, props.track.title, props.track.artist, props.track.webpage_url, props.lyricsEndpoint, props.resultVersion], () => {
  lyrics.value = { lines: [], plain: '', source: '' }
  coverFailed.value = false
  importing.value = false
  followAfter = 0
  void loadLyrics()
}, { immediate: true })
watch(activeIndex, () => { void followCurrentLine() })
onMounted(() => {
  previousFocus = document.activeElement as HTMLElement | null
  previousOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  document.addEventListener('keydown', onKeydown, true)
  dialog.value?.querySelector<HTMLButtonElement>('button')?.focus()
})
onBeforeUnmount(() => {
  generation++
  request?.abort()
  document.removeEventListener('keydown', onKeydown, true)
  document.body.style.overflow = previousOverflow
  previousFocus?.focus()
})
</script>

<template>
  <Teleport to="body">
    <section ref="dialog" class="lyrics-screen" role="dialog" aria-modal="true" aria-labelledby="lyrics-title">
      <header class="lyrics-topbar">
        <button class="lyrics-icon" type="button" title="收起播放页" aria-label="收起播放页" @click="emit('close')"><ChevronDown :size="25" /></button>
        <span>正在播放</span>
        <div class="lyrics-tools">
          <span v-if="lyrics.source" class="lyrics-source">{{ lyrics.source }}</span>
          <button class="lyrics-icon" type="button" :disabled="(!filename && !lyricsEndpoint && !track.webpage_url) || loading || importing" title="匹配歌词" aria-label="匹配歌词" @click="loadLyrics()"><LoaderCircle v-if="loading" :size="19" class="spin" /><RefreshCw v-else :size="19" /></button>
          <button class="lyrics-icon" type="button" :disabled="!filename || importing" title="导入 LRC 歌词" aria-label="导入 LRC 歌词" @click="importInput?.click()"><LoaderCircle v-if="importing" :size="20" class="spin" /><FileMusic v-else :size="20" /></button>
          <input ref="importInput" type="file" accept=".lrc" hidden @change="importLyrics" />
        </div>
      </header>
      <div class="lyrics-stage">
        <div class="lyrics-artwork">
          <img v-if="track.thumbnail && !coverFailed" :src="track.thumbnail" :alt="`${track.title} 封面`" @error="coverFailed = true" />
          <div v-else class="lyrics-artwork-empty"><Music2 :size="74" :stroke-width="1" /><span>{{ track.title }}</span></div>
        </div>
        <div class="lyrics-song">
          <div class="lyrics-song-heading"><h1 id="lyrics-title">{{ track.title }}</h1><p>{{ track.artist || '未知歌手' }}<span v-if="track.album"> · {{ track.album }}</span></p></div>
          <div ref="lyricViewport" class="lyrics-scroll" tabindex="0" aria-label="歌词" @wheel.passive="followAfter = Date.now() + 4000" @touchmove.passive="followAfter = Date.now() + 4000">
            <div v-if="lyrics.lines.length" class="lyrics-lines">
              <button v-for="(line, index) in lyrics.lines" :key="`${line.time}-${index}`" class="lyrics-line" :class="{ current: activeIndex === index, past: index < activeIndex }" :aria-current="activeIndex === index ? 'true' : undefined" type="button" @click="seekLine(line.time)">{{ line.text || '…' }}</button>
            </div>
            <div v-else-if="lyrics.plain" class="lyrics-plain">{{ lyrics.plain }}</div>
            <div v-else class="lyrics-empty"><LoaderCircle v-if="loading" :size="23" class="spin" /><Music2 v-else :size="27" :stroke-width="1.5" /><p>{{ loading ? '正在匹配歌词' : lyrics.message || '暂无歌词' }}</p></div>
          </div>
          <p v-if="lyrics.message && (lyrics.lines.length || lyrics.plain)" class="lyrics-error">{{ lyrics.message }}</p>
          <p v-if="error" class="lyrics-error" role="alert">{{ error }}</p>
        </div>
      </div>
      <footer class="lyrics-controls"><slot name="controls" /></footer>
    </section>
  </Teleport>
</template>

<style scoped>
.lyrics-screen { position: fixed; inset: 0; z-index: 100; display: grid; grid-template-rows: 68px minmax(0, 1fr) auto; color: #f3f3f1; background: #1d2020; letter-spacing: 0; }
.lyrics-topbar { display: flex; align-items: center; gap: 18px; padding: 0 30px; color: #a9adad; font-size: 13px; }
.lyrics-tools { display: flex; align-items: center; gap: 12px; margin-left: auto; }
.lyrics-source { font-size: 12px; }
.lyrics-icon { width: 40px; height: 40px; display: inline-flex; align-items: center; justify-content: center; padding: 0; border: 0; border-radius: 6px; background: transparent; color: #bcc0c0; cursor: pointer; }
.lyrics-icon:hover:not(:disabled) { color: #fff; background: #ffffff12; }
.lyrics-icon:disabled { opacity: .35; cursor: default; }
.lyrics-icon:focus-visible, .lyrics-line:focus-visible, .lyrics-scroll:focus-visible { outline: 2px solid #f5a35a; outline-offset: 3px; }
.lyrics-stage { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); align-items: center; gap: 8%; width: min(1000px, calc(100% - 100px)); margin: 0 auto; min-height: 0; padding: 35px 0 55px; }
.lyrics-artwork { width: min(100%, 380px); aspect-ratio: 1; justify-self: center; overflow: hidden; border-radius: 6px; box-shadow: 0 16px 60px #0005; }
.lyrics-artwork img { display: block; width: 100%; height: 100%; object-fit: cover; }
.lyrics-artwork-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 30px; width: 100%; height: 100%; padding: 30px; color: #b3bebe; background: #303939; }
.lyrics-artwork-empty span { max-width: 100%; text-align: center; font-size: 18px; overflow-wrap: anywhere; }
.lyrics-song { display: flex; flex-direction: column; min-width: 0; height: min(100%, 510px); min-height: 0; }
.lyrics-song-heading { padding: 0 0 24px; }
.lyrics-song-heading h1 { font-size: 25px; font-weight: 600; line-height: 1.4; overflow-wrap: anywhere; }
.lyrics-song-heading p { margin-top: 8px; color: #929b9b; font-size: 14px; overflow-wrap: anywhere; }
.lyrics-scroll { position: relative; flex: 1; min-height: 0; overflow-y: auto; scrollbar-width: thin; scrollbar-color: #ffffff18 transparent; mask-image: linear-gradient(transparent, #000 8%, #000 85%, transparent); }
.lyrics-lines { padding: 90px 0 160px; }
.lyrics-line { display: block; width: 100%; min-height: 55px; margin: 0 0 12px; padding: 8px 0; border: 0; text-align: left; font-size: 23px; font-weight: 500; line-height: 1.65; white-space: pre-line; overflow-wrap: anywhere; color: #9ca4a4; background: transparent; cursor: pointer; transition: color .2s; }
.lyrics-line.past { color: #737f7f; }
.lyrics-line.current { color: #f5a35a; }
.lyrics-line:hover { color: #fff; }
.lyrics-plain { padding: 24px 0 60px; font-size: 20px; line-height: 2.2; white-space: pre-line; overflow-wrap: anywhere; color: #aeb6b6; }
.lyrics-empty { display: flex; flex-direction: column; align-items: flex-start; justify-content: center; gap: 16px; min-height: 200px; color: #869292; font-size: 15px; }
.lyrics-error { margin: 10px 0 0; font-size: 12px; line-height: 1.6; color: #f8b18f; }
.lyrics-controls { min-height: 102px; padding: 10px 32px 20px; border-top: 1px solid #ffffff0d; }
@media (max-width: 700px) {
  .lyrics-screen { grid-template-rows: 56px minmax(0, 1fr) auto; }
  .lyrics-topbar { padding: 0 12px; gap: 8px; }
  .lyrics-tools { gap: 4px; }
  .lyrics-source { display: none; }
  .lyrics-stage { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); gap: 20px; width: calc(100% - 40px); padding: 12px 0 14px; }
  .lyrics-artwork { width: min(210px, 30vh); }
  .lyrics-artwork-empty { padding: 16px; gap: 16px; }
  .lyrics-artwork-empty span { font-size: 14px; }
  .lyrics-song { height: 100%; text-align: center; }
  .lyrics-song-heading { padding-bottom: 12px; }
  .lyrics-song-heading h1 { font-size: 20px; }
  .lyrics-song-heading p { margin-top: 4px; font-size: 12px; }
  .lyrics-line { text-align: center; font-size: 18px; min-height: 40px; }
  .lyrics-lines { padding-top: 55px; padding-bottom: 100px; }
  .lyrics-empty { align-items: center; min-height: 100px; }
  .lyrics-controls { padding: 8px 14px 18px; }
}
@media (min-width: 701px) and (max-height: 600px) {
  .lyrics-stage { width: calc(100% - 70px); padding: 16px 0; }
  .lyrics-artwork { width: min(100%, 260px, calc(100dvh - 225px)); }
  .lyrics-song-heading { padding-bottom: 12px; }
  .lyrics-song-heading h1 { font-size: 21px; }
  .lyrics-line { font-size: 19px; min-height: 43px; }
  .lyrics-lines { padding-top: 45px; padding-bottom: 100px; }
}
@media (prefers-reduced-motion: reduce) { .lyrics-line { transition: none; } }
</style>
