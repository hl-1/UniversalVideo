<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { Search, RotateCcw, ChevronDown, UserRound, ArrowUpDown, SlidersHorizontal, X } from 'lucide-vue-next'
import { useAndroidBack } from '../../../shared/native/navigation'
import { defaultMusicListFilters, type MusicListFilters } from '../musicList'

const props = withDefaults(defineProps<{
  modelValue: MusicListFilters; artists: string[]; total: number; count: number; hasTime: boolean; timeLabel?: string; label?: string
}>(), { timeLabel: '记录时间', label: '歌曲列表筛选' })
const emit = defineEmits<{ 'update:modelValue': [value: MusicListFilters] }>()
const panelId = useId()
const panel = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const isMobile = ref(false)
const sheetOpen = ref(false)
const advancedCount = computed(() => Number(!!props.modelValue.artist) + Number(props.modelValue.field !== 'all') + Number(props.modelValue.match !== 'fuzzy') + Number(props.modelValue.sort !== 'default'))
let viewport: MediaQueryList | undefined
function updateViewport() {
  isMobile.value = !!viewport?.matches
  if (!isMobile.value) closeFilters()
}
function update<K extends keyof MusicListFilters>(key: K, value: MusicListFilters[K]) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}
function closeFilters() { sheetOpen.value = false }
function onBackdrop(event: MouseEvent) {
  if (event.target !== event.currentTarget || !panel.value) return
  const bounds = panel.value.getBoundingClientRect()
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeFilters()
}
watch(sheetOpen, async open => {
  await nextTick()
  if (sheetOpen.value !== open || !(panel.value instanceof HTMLDialogElement)) return
  if (open) {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
    trigger.value?.focus({ preventScroll: true })
    panel.value.showModal()
  } else if (panel.value.open) panel.value.close()
})
onMounted(() => {
  viewport = window.matchMedia('(max-width: 760px)')
  updateViewport()
  viewport.addEventListener('change', updateViewport)
})
onBeforeUnmount(() => {
  viewport?.removeEventListener('change', updateViewport)
  if (panel.value instanceof HTMLDialogElement && panel.value.open) panel.value.close()
})
useAndroidBack(() => {
  if (!sheetOpen.value) return false
  closeFilters()
  return true
}, 120)
</script>

<template>
  <div class="music-list-controls" role="group" :aria-label="label">
    <label class="ml-search"><Search :size="16" aria-hidden="true" /><input type="search" :value="modelValue.query" placeholder="搜索歌名或歌手" aria-label="搜索当前列表" enterkeyhint="search" @input="update('query', ($event.target as HTMLInputElement).value)" @keydown.enter.prevent="($event.target as HTMLInputElement).blur()" /></label>
    <button v-if="isMobile" ref="trigger" type="button" class="ml-filter-trigger" :class="{ 'is-active': advancedCount }" :aria-label="advancedCount ? `筛选与排序，已启用 ${advancedCount} 项条件` : '筛选与排序'" :aria-expanded="sheetOpen" :aria-controls="panelId" aria-haspopup="dialog" @click="sheetOpen = true"><SlidersHorizontal :size="16" aria-hidden="true" /><span>筛选</span><span v-if="advancedCount" class="ml-filter-badge" aria-hidden="true">{{ advancedCount }}</span></button>
    <Teleport to="body" :disabled="!isMobile">
      <component :is="isMobile ? 'dialog' : 'div'" :id="panelId" ref="panel" class="ml-options" :aria-labelledby="isMobile ? `${panelId}-title` : undefined" @cancel.prevent="closeFilters" @close="closeFilters" @click="onBackdrop">
        <header v-if="isMobile" class="ml-panel-header"><div><h2 :id="`${panelId}-title`">筛选与排序</h2><p>匹配 {{ count }} / 已加载 {{ total }} 首</p></div><button type="button" class="ml-close" aria-label="关闭筛选面板" autofocus @click="closeFilters"><X :size="20" aria-hidden="true" /></button></header>
        <div class="ml-fields">
          <label class="ml-select ml-artist" title="按歌手筛选"><span class="ml-option-name">歌手</span><UserRound class="ml-prefix" :size="14" aria-hidden="true" /><select :value="modelValue.artist" aria-label="按歌手筛选" @change="update('artist', ($event.target as HTMLSelectElement).value)"><option value="">全部歌手</option><option v-if="modelValue.artist && !artists.includes(modelValue.artist)" :value="modelValue.artist">{{ modelValue.artist }}（当前无歌曲）</option><option v-for="name in artists" :key="name" :value="name">{{ name }}</option></select><ChevronDown class="ml-chevron" :size="13" aria-hidden="true" /></label>
          <label class="ml-select" title="搜索范围"><span class="ml-option-name">搜索范围</span><select :value="modelValue.field" aria-label="列表搜索范围" @change="update('field', ($event.target as HTMLSelectElement).value as MusicListFilters['field'])"><option value="all">歌名和歌手</option><option value="title">仅歌名</option><option value="artist">仅歌手</option></select><ChevronDown class="ml-chevron" :size="13" aria-hidden="true" /></label>
          <label class="ml-select" title="匹配方式"><span class="ml-option-name">匹配方式</span><select :value="modelValue.match" aria-label="列表匹配方式" @change="update('match', ($event.target as HTMLSelectElement).value as MusicListFilters['match'])"><option value="fuzzy">模糊匹配</option><option value="exact">精确匹配</option></select><ChevronDown class="ml-chevron" :size="13" aria-hidden="true" /></label>
          <label class="ml-select ml-sort" :title="`按${timeLabel}排序`"><span class="ml-option-name">{{ timeLabel }}</span><ArrowUpDown class="ml-prefix" :size="14" aria-hidden="true" /><select :value="modelValue.sort" aria-label="按时间排序" @change="update('sort', ($event.target as HTMLSelectElement).value as MusicListFilters['sort'])"><option value="default">{{ isMobile ? '默认顺序' : `${timeLabel} · 默认` }}</option><option value="newest" :disabled="!hasTime">{{ isMobile ? '新到旧' : `${timeLabel} · 新到旧` }}</option><option value="oldest" :disabled="!hasTime">{{ isMobile ? '旧到新' : `${timeLabel} · 旧到新` }}</option></select><ChevronDown class="ml-chevron" :size="13" aria-hidden="true" /></label>
          <p v-if="isMobile && total && !hasTime" class="ml-time-hint">暂无时间信息，暂不支持按时间排序</p>
        </div>
        <footer class="ml-panel-footer"><button type="button" class="ml-reset" aria-label="重置列表筛选" @click="emit('update:modelValue', defaultMusicListFilters())"><RotateCcw :size="14" aria-hidden="true" />重置</button><button v-if="isMobile" type="button" class="ml-done" @click="closeFilters">查看 {{ count }} 首歌曲</button></footer>
      </component>
    </Teleport>
    <span class="ml-count" role="status" aria-live="polite">匹配 {{ count }} / 已加载 {{ total }} 首<span v-if="total && !hasTime" class="ml-missing-time"> · 暂无时间信息</span></span>
  </div>
</template>

<style scoped>
.music-list-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; min-width: 0; margin: 12px 0 16px; color: var(--music-muted, #7a827e); font-size: 12px; }
.ml-options, .ml-fields, .ml-panel-footer { display: contents; }
.ml-search, .ml-select { position: relative; display: flex; align-items: center; min-width: 0; border: 1px solid var(--music-line, #cfd9d2); border-radius: 8px; background: color-mix(in srgb, var(--music-surface, #fff) 35%, transparent); transition: border-color .15s, background-color .15s; }
.ml-search:hover, .ml-select:hover { border-color: color-mix(in srgb, var(--music-accent, #109b6d) 45%, var(--music-line, #cfd9d2)); }
.music-list-controls .ml-search { box-sizing: border-box; flex: 0 1 160px; gap: 8px; padding: 0 12px; }
.ml-search input, .ml-select select { min-width: 0; max-width: 100%; height: 36px; padding: 0 10px; border: 0; border-radius: 8px; color: var(--music-text, #252c29); background: transparent; font: inherit; }
.music-list-controls .ml-search input { width: 100%; padding: 0; border: 0; background: transparent; }
.ml-search:focus-within, .ml-select:focus-within { border-color: var(--music-accent, #109b6d); outline: 2px solid color-mix(in srgb, var(--music-accent, #109b6d) 22%, transparent); outline-offset: 1px; }
.ml-search input:focus-visible, .ml-select select:focus-visible { outline: none; box-shadow: none; }
.ml-select select { width: 100%; max-width: 180px; padding-right: 30px; appearance: none; cursor: pointer; text-overflow: ellipsis; }
.ml-artist select, .ml-sort select { padding-left: 31px; }
.ml-prefix, .ml-chevron { position: absolute; pointer-events: none; }
.ml-prefix { left: 10px; }
.ml-chevron { right: 9px; }
.ml-select option { color: var(--music-text, #252c29); background: var(--music-surface, #fff); }
.ml-reset { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 36px; padding: 0 9px; border: 0; border-radius: 8px; color: var(--music-highlight, #109b6d); background: transparent; font: inherit; cursor: pointer; }
.ml-reset:hover { background: var(--music-accent-soft, #edf5ec); }
.ml-count { flex-basis: 100%; overflow-wrap: anywhere; }
.ml-option-name { display: none; }
@media (max-width: 760px) {
  .music-list-controls { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 7px 8px; margin: 12px 0; }
  .music-list-controls .ml-search { grid-column: 1; }
  .music-list-controls input { height: 42px; font-size: 16px; }
  .ml-filter-trigger { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 44px; padding: 0 11px; border: 1px solid var(--music-line, #cfd9d2); border-radius: 8px; color: var(--music-text, #252c29); background: var(--music-surface, #fff); font: inherit; cursor: pointer; }
  .ml-filter-trigger.is-active { color: var(--music-highlight, #109b6d); border-color: var(--music-accent, #109b6d); background: var(--music-accent-soft, #edf5ec); }
  .ml-filter-badge { display: inline-flex; align-items: center; justify-content: center; min-width: 16px; height: 16px; border-radius: 50%; background: var(--music-accent, #109b6d); color: var(--music-accent-text, #fff); font-size: 10px; font-weight: 600; }
  .music-list-controls .ml-count { grid-column: 1 / -1; grid-row: 2; font-size: 11px; }
  .ml-missing-time { display: none; }
  .ml-options { display: none; box-sizing: border-box; width: 100%; max-width: 560px; max-height: calc(100dvh - 20px); margin: auto auto 0; padding: 0; border: 1px solid var(--music-line, #cfd9d2); border-bottom: 0; border-radius: 18px 18px 0 0; color: var(--music-text, #252c29); background: var(--music-surface, #fff); font-family: inherit; font-size: 14px; line-height: 1.5; overflow: hidden; overscroll-behavior: contain; box-shadow: 0 -8px 40px #0002; }
  .ml-options[open] { display: flex; flex-direction: column; animation: ml-sheet-in .16s ease-out; }
  .ml-options::backdrop { background: #11182770; }
  .ml-panel-header { display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; padding: 16px 16px 12px; }
  .ml-panel-header h2 { margin: 0; font-size: 17px; font-weight: 600; }
  .ml-panel-header p { margin: 4px 0 0; color: var(--music-muted, #7a827e); font-size: 12px; }
  .ml-close { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; width: 44px; height: 44px; padding: 0; border: 0; border-radius: 50%; color: var(--music-muted, #7a827e); background: transparent; cursor: pointer; }
  .ml-close:hover { background: var(--music-accent-soft, #edf5ec); }
  .ml-fields { display: flex; flex-direction: column; gap: 10px; min-height: 0; padding: 4px 16px 16px; overflow-y: auto; overscroll-behavior: contain; }
  .ml-select { flex-shrink: 0; padding-left: 12px; }
  .ml-option-name { display: block; flex: 0 0 auto; max-width: 40%; color: var(--music-muted, #7a827e); font-size: 13px; }
  .ml-select select { flex: 1; width: 0; max-width: 100%; height: 46px; padding-left: 8px; padding-right: 28px; font-size: 16px; text-align: right; }
  .ml-prefix { display: none; }
  .ml-time-hint { margin: 0; color: var(--music-muted, #7a827e); font-size: 12px; }
  .ml-panel-footer { display: flex; align-items: center; gap: 12px; flex-shrink: 0; padding: 12px 16px max(16px, env(safe-area-inset-bottom)); border-top: 1px solid var(--music-line, #cfd9d2); }
  .ml-panel-footer .ml-reset { flex: 0 0 72px; min-height: 44px; font-size: 14px; }
  .ml-done { flex: 1; min-width: 0; min-height: 44px; padding: 8px 12px; border: 0; border-radius: 10px; color: var(--music-accent-text, #fff); background: var(--music-accent, #109b6d); font: inherit; font-weight: 500; cursor: pointer; }
  .ml-filter-trigger:focus-visible, .ml-options button:focus-visible { outline: 2px solid var(--music-accent, #109b6d); outline-offset: 2px; }
  :global(body:has(.ml-options[open])) { overflow: hidden; }
}
@keyframes ml-sheet-in { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: translateY(0); } }
@media (prefers-reduced-motion: reduce) { .ml-options[open] { animation: none; } }
</style>
