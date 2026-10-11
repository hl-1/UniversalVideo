<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MindElixir, { type MindElixirInstance, type Theme } from 'mind-elixir'
import 'mind-elixir/style.css'
import { mindMapPanDelta, toMindElixirData, type MindMapResult } from './mindMap'
import { createMindMapImage, mindMapImageFilename, type MindMapImageFormat } from './mindMapExport'
import type { MapView } from './videoHistory'
import { Download, FileImage, FoldVertical, Image, LoaderCircle, Maximize2, Minus, Plus, UnfoldVertical } from 'lucide-vue-next'

const props = defineProps<{ result: MindMapResult; seekEnabled?: boolean; savedView?: MapView | null }>()
const emit = defineEmits<{ seek: [timestamp: number]; viewChange: [view: MapView] }>()

const host = ref<HTMLDivElement | null>(null)
const panPosition = ref(0)
const panTravel = ref(0)
const exportControl = ref<HTMLDivElement | null>(null)
const exportTrigger = ref<HTMLButtonElement | null>(null)
const exportMenuOpen = ref(false)
const exporting = ref(false)
const exportError = ref('')
let mind: MindElixirInstance | null = null
let disposed = false
let restoring = true
let captureQueued = false
let resizeObserver: ResizeObserver | null = null

function mapData() {
  const data = toMindElixirData(props.result)
  const collapsed = new Set(props.savedView?.collapsed || [])
  const visit = (node: typeof data.nodeData) => { node.expanded = !collapsed.has(node.id); node.children?.forEach(visit) }
  visit(data.nodeData)
  return data
}

function captureView() {
  if (restoring || !mind || !host.value || captureQueued) return
  captureQueued = true
  requestAnimationFrame(() => {
    captureQueued = false
    if (restoring || !mind || !host.value || !host.value.clientWidth) return
    const root = MindElixir.E('video-mind-map-root', host.value)
    if (!root) return
    const collapsed: string[] = []
    const data = mind.getData()
    const visit = (node: typeof data.nodeData) => { if (node.expanded === false) collapsed.push(node.id); node.children?.forEach(visit) }
    visit(data.nodeData)
    const rect = root.getBoundingClientRect()
    const container = host.value.getBoundingClientRect()
    // Relative root coordinates preserve framing across different screen sizes.
    emit('viewChange', { scale: mind.scaleVal, x: (rect.left - container.left) / container.width,
      y: (rect.top - container.top) / container.height, collapsed })
  })
}

const theme: Theme = {
  name: 'videodream',
  type: 'light',
  palette: ['#2563eb', '#f59e0b', '#22c55e', '#ec4899', '#8b5cf6', '#06b6d4'],
  cssVar: {
    '--node-gap-x': '54px',
    '--node-gap-y': '10px',
    '--main-gap-x': '86px',
    '--main-gap-y': '18px',
    '--main-color': '#172033',
    '--main-bgcolor': 'transparent',
    '--main-bgcolor-transparent': 'transparent',
    '--main-border': 'none',
    '--color': '#334155',
    '--bgcolor': '#ffffff',
    '--selected': '#dbeafe',
    '--accent-color': '#2563eb',
    '--root-color': '#172033',
    '--root-bgcolor': '#ffffff',
    '--root-border-color': '#d9e2ec',
    '--root-radius': '6px',
    '--main-radius': '0',
    '--topic-padding': '3px 5px',
    '--panel-color': '#334155',
    '--panel-bgcolor': '#ffffff',
    '--panel-border-color': '#d9e2ec',
    '--map-padding': '44px',
  },
}

function fitMap(save = true, restore = false) {
  restoring = true
  requestAnimationFrame(() => {
    if (!mind || !host.value || !host.value.clientWidth) { restoring = false; return }
    mind.scaleFit()
    const targetScale = host.value.clientWidth >= 760
      ? mind.scaleVal * 1.18
      : Math.max(0.6, mind.scaleVal * 1.2)
    mind.toCenter()
    mind.scale(Math.min(1.35, targetScale))
    requestAnimationFrame(() => {
      alignMapLeft()
      if (restore && props.savedView && mind && host.value) {
        mind.scale(props.savedView.scale)
        const root = MindElixir.E('video-mind-map-root', host.value)
        if (root) {
          const rect = root.getBoundingClientRect()
          const container = host.value.getBoundingClientRect()
          mind.move(props.savedView.x * container.width - (rect.left - container.left), props.savedView.y * container.height - (rect.top - container.top))
        }
      }
      restoring = false
      if (save) captureView()
    })
  })
}

function alignMapLeft() {
  if (!mind || !host.value) return
  const root = MindElixir.E('video-mind-map-root', host.value)
  if (!root) return
  const hostRect = host.value.getBoundingClientRect()
  const rootRect = root.getBoundingClientRect()
  mind.move(hostRect.left + 36 - rootRect.left, 0)
  panPosition.value = 0
  requestAnimationFrame(updatePanTravel)
}

function updatePanTravel() {
  if (!mind || !host.value) return
  const hostRect = host.value.getBoundingClientRect()
  const nodesRect = mind.nodes.getBoundingClientRect()
  panTravel.value = Math.max(0, nodesRect.right - hostRect.right + 36)
}

function syncSeekButtons() {
  host.value?.querySelectorAll<HTMLButtonElement>('.video-mind-time').forEach((button) => {
    button.disabled = !props.seekEnabled
  })
}

function renderMap() {
  if (!mind) return
  mind.refresh(mapData())
  nextTick(() => {
    syncSeekButtons()
    fitMap(false, true)
  })
}

function zoom(delta: number) {
  if (!mind) return
  mind.scale(Math.min(1.6, Math.max(0.5, mind.scaleVal + delta)))
  requestAnimationFrame(updatePanTravel)
}

function handleHorizontalPan(event: Event) {
  if (!mind || !(event.target instanceof HTMLInputElement)) return
  const next = Number(event.target.value)
  mind.move(mindMapPanDelta(panPosition.value, next, panTravel.value), 0)
  panPosition.value = next
}

function setAllExpanded(expanded: boolean) {
  if (!mind || !host.value) return
  const root = MindElixir.E('video-mind-map-root', host.value)
  if (root) mind.expandNodeAll(root, expanded)
  nextTick(() => fitMap())
}

async function toggleExportMenu() {
  exportMenuOpen.value = !exportMenuOpen.value
  if (exportMenuOpen.value) {
    await nextTick()
    exportControl.value?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus()
  }
}

function closeExportMenu(event: PointerEvent) {
  if (event.target instanceof Node && !exportControl.value?.contains(event.target)) exportMenuOpen.value = false
}

function handleExportKeys(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    exportMenuOpen.value = false
    exportTrigger.value?.focus()
    return
  }
  const buttons = Array.from(exportControl.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') || [])
  if (!buttons.length || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (current + (event.key === 'ArrowUp' ? -1 : 1) + buttons.length) % buttons.length
  buttons[index]?.focus()
}

function handleExportFocus(event: FocusEvent) {
  if (event.relatedTarget instanceof Node && !exportControl.value?.contains(event.relatedTarget)) exportMenuOpen.value = false
}

async function exportImage(format: MindMapImageFormat) {
  if (exporting.value) return
  exportMenuOpen.value = false
  exporting.value = true
  exportError.value = ''
  const result = props.result
  try {
    const blob = await createMindMapImage(result, theme, format)
    if (disposed) return
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = mindMapImageFilename(result.title, format)
    document.body.appendChild(anchor)
    try { anchor.click() } finally {
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    }
  } catch {
    if (!disposed) exportError.value = `${format.toUpperCase()} 导出失败，请重试。`
  } finally {
    exporting.value = false
    if (!disposed) await nextTick(() => exportTrigger.value?.focus())
  }
}

function handleClick(event: MouseEvent) {
  const target = event.target instanceof Element ? event.target.closest<HTMLButtonElement>('.video-mind-time') : null
  if (!target || !host.value?.contains(target) || !props.seekEnabled) return
  const timestamp = Number(target.dataset.timestamp)
  if (Number.isFinite(timestamp) && timestamp >= 0) emit('seek', timestamp)
}

onMounted(() => {
  if (!host.value) return
  mind = new MindElixir({
    el: host.value,
    direction: MindElixir.RIGHT,
    editable: false,
    contextMenu: false,
    toolBar: false,
    keypress: false,
    allowUndo: false,
    overflowHidden: true,
    compact: false,
    alignment: 'root',
    scaleMin: 0.5,
    scaleMax: 1.6,
    theme,
  })
  mind.init(mapData())
  mind.bus.addListener('scale', captureView)
  mind.bus.addListener('move', captureView)
  mind.bus.addListener('expandNode', captureView)
  host.value.addEventListener('click', handleClick)
  document.addEventListener('pointerdown', closeExportMenu)
  nextTick(() => {
    syncSeekButtons()
    fitMap(false, true)
  })
  let previousWidth = 0
  resizeObserver = new ResizeObserver(entries => {
    const width = entries[0]?.contentRect.width || 0
    if (width > 0 && width !== previousWidth) fitMap(false, true)
    previousWidth = width
  })
  resizeObserver.observe(host.value)
})

watch(() => props.result, renderMap, { deep: true })
watch(() => props.seekEnabled, syncSeekButtons)

onBeforeUnmount(() => {
  disposed = true
  document.removeEventListener('pointerdown', closeExportMenu)
  resizeObserver?.disconnect()
  host.value?.removeEventListener('click', handleClick)
  mind?.destroy()
  mind = null
})
</script>

<template>
  <div class="video-mind-map-shell">
    <div class="video-mind-map-tools" aria-label="思维导图视图控制">
      <button type="button" aria-label="全部收起" title="全部收起" @click="setAllExpanded(false)"><FoldVertical :size="17" /></button>
      <button type="button" aria-label="全部展开" title="全部展开" @click="setAllExpanded(true)"><UnfoldVertical :size="17" /></button>
      <button type="button" aria-label="缩小" title="缩小" @click="zoom(-0.1)"><Minus :size="17" /></button>
      <button type="button" aria-label="适应画布" title="适应画布" @click="fitMap()"><Maximize2 :size="17" /></button>
      <button type="button" aria-label="放大" title="放大" @click="zoom(0.1)"><Plus :size="17" /></button>
      <div ref="exportControl" class="video-mind-map-export-control" @keydown="handleExportKeys" @focusout="handleExportFocus">
        <button ref="exportTrigger" type="button" aria-label="导出思维导图" :title="exporting ? '正在导出' : '导出思维导图'"
          aria-haspopup="menu" :aria-expanded="exportMenuOpen" :aria-busy="exporting" :disabled="exporting"
          @click="toggleExportMenu" @keydown.down.prevent="!exportMenuOpen && toggleExportMenu()">
          <LoaderCircle v-if="exporting" :size="17" class="video-mind-map-export-spinner" />
          <Download v-else :size="17" />
        </button>
        <div v-if="exportMenuOpen" class="video-mind-map-export-menu" role="menu" aria-label="导出格式">
          <button type="button" role="menuitem" @click="exportImage('svg')"><FileImage :size="16" /><span>导出 SVG</span></button>
          <button type="button" role="menuitem" @click="exportImage('png')"><Image :size="16" /><span>导出 PNG</span></button>
        </div>
      </div>
    </div>
    <p v-if="exportError" class="video-mind-map-export-error" role="alert">{{ exportError }}</p>
    <div ref="host" class="video-mind-map-canvas" aria-label="AI 视频总结思维导图"></div>
    <label class="video-mind-map-pan">
      <input
        type="range"
        min="0"
        max="100"
        step="1"
        :value="panPosition"
        :disabled="panTravel <= 0"
        aria-label="水平浏览思维导图"
        @input="handleHorizontalPan"
      />
    </label>
  </div>
</template>
