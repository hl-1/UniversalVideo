<script setup lang="ts">
import { chooseFile, saveBlob } from '../../../shared/downloads'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MindElixir, { type MindElixirInstance, type Theme } from 'mind-elixir'
import 'mind-elixir/style.css'
import { toMindElixirData, type MindMapDisplayMode, type MindMapNode, type MindMapResult } from '../mindMap'
import { createMindMapImage, mindMapImageFilename, type MindMapImageFormat } from '../mindMapExport'
import type { MapView } from '../videoHistory'
import { chooseMindMapLayout, mindMapFitScale, type MindMapLayout, type ResolvedMindMapLayout } from '../mindMapLayout'
import { Download, Expand, FileImage, FoldVertical, Image, LoaderCircle, Maximize2, Minus, Plus, UnfoldVertical, X } from 'lucide-vue-next'

const props = defineProps<{ result: MindMapResult; seekEnabled?: boolean; savedView?: MapView | null }>()
const emit = defineEmits<{ seek: [timestamp: number]; viewChange: [view: MapView]; rendered: [milliseconds: number] }>()
let renderStarted = performance.now()
let renderReported = false

const host = ref<HTMLDivElement | null>(null)
const shell = ref<HTMLDivElement | null>(null)
const displayMode = ref<MindMapDisplayMode>('overview')
const layoutPreference = ref<MindMapLayout>(props.savedView?.layout || 'auto')
const resolvedLayout = ref<ResolvedMindMapLayout>('balanced')
const layoutNames = { balanced: '居中双向', right: '向右展开', left: '向左展开' }
const layoutHint = computed(() => layoutPreference.value === 'auto' ? `自动布局：${layoutNames[resolvedLayout.value]}` : layoutNames[resolvedLayout.value])
const overviewDepth = ref(2)
const selected = ref<MindMapNode | null>(null)
const exportScope = ref<'overview' | 'detail'>('overview')
const overviewHint = computed(() => overviewDepth.value === 1 ? '已展示全部主主题，点击节点查看说明' : '已展示主主题与关键点，点击节点查看说明')
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
let layoutFrame: number | undefined

function mapPadding() { return host.value && host.value.clientWidth < 640 ? 12 : 24 }

function defaultOverviewDepth() { return host.value && host.value.clientWidth < 640 ? 1 : 2 }

function mapData(layout = resolvedLayout.value, preserveExpansion = false) {
  const collapsed: string[] = []
  if (preserveExpansion && mind) {
    const visit = (node: ReturnType<typeof toMindElixirData>['nodeData']) => {
      if (node.expanded === false) collapsed.push(node.id)
      node.children?.forEach(visit)
    }
    visit(mind.getData().nodeData)
  }
  return toMindElixirData(props.result, { mode: displayMode.value,
    layout, depth: preserveExpansion ? 4 : displayMode.value === 'overview' ? overviewDepth.value : 4,
    collapsed: preserveExpansion ? collapsed : displayMode.value === 'detail' ? props.savedView?.collapsed : [] })
}

function captureView() {
  if (displayMode.value === 'overview' || restoring || !mind || !host.value || captureQueued) return
  captureQueued = true
  requestAnimationFrame(() => {
    captureQueued = false
    if (displayMode.value === 'overview' || restoring || !mind || !host.value || !host.value.clientWidth) return
    updatePanTravel()
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
      y: (rect.top - container.top) / container.height, collapsed,
      layout: layoutPreference.value, resolved_layout: resolvedLayout.value, framing_valid: true })
  })
}

const theme: Theme = {
  name: 'videodream',
  type: 'light',
  palette: ['#2563eb', '#f59e0b', '#22c55e', '#ec4899', '#8b5cf6', '#06b6d4'],
  cssVar: {
    '--node-gap-x': '22px',
    '--node-gap-y': '4px',
    '--main-gap-x': '30px',
    '--main-gap-y': '12px',
    '--main-color': '#172033',
    '--main-bgcolor': 'transparent',
    '--main-bgcolor-transparent': 'transparent',
    '--main-border': '1.5px solid #94a3b8',
    '--color': '#334155',
    '--bgcolor': '#ffffff',
    '--selected': '#dbeafe',
    '--accent-color': '#2563eb',
    '--root-color': '#172033',
    '--root-bgcolor': '#f5f3ff',
    '--root-border-color': '#8b5cf6',
    '--root-radius': '24px',
    '--main-radius': '16px',
    '--topic-padding': '3px 5px',
    '--panel-color': '#334155',
    '--panel-bgcolor': '#ffffff',
    '--panel-border-color': '#d9e2ec',
    '--map-padding': '12px',
  },
}

function fitMap(save = true, restore = false) {
  const savedView = restore && displayMode.value === 'detail' && props.savedView ? { ...props.savedView } : null
  restoring = true
  if (layoutFrame !== undefined) cancelAnimationFrame(layoutFrame)
  // Keep fitting and restoration atomic so resize cannot save a temporary centered view.
  layoutFrame = requestAnimationFrame(() => {
    layoutFrame = undefined
    if (!mind || !host.value || !host.value.clientWidth || !host.value.clientHeight) { restoring = false; return }
    const padding = mapPadding()
    const viewport = { width: host.value.clientWidth, height: host.value.clientHeight }
    const fit = (layout: ResolvedMindMapLayout) => {
      mind!.refresh(mapData(layout, true))
      const root = MindElixir.E('video-mind-map-root', host.value!)
      return mindMapFitScale(layout, mind!.nodes.getBoundingClientRect(), root.getBoundingClientRect(),
        viewport, mind!.scaleVal, padding)
    }
    let targetScale: number
    if (layoutPreference.value === 'auto') {
      const balancedScale = fit('balanced')
      const rightScale = fit('right')
      resolvedLayout.value = chooseMindMapLayout(balancedScale, rightScale)
      targetScale = resolvedLayout.value === 'right' ? rightScale : fit('balanced')
    } else {
      resolvedLayout.value = layoutPreference.value
      targetScale = fit(resolvedLayout.value)
    }
    syncSeekButtons()
    if (displayMode.value === 'overview' && overviewDepth.value > 1
      && (targetScale < 12 / 14 || host.value.querySelectorAll('me-tpc').length > 25)) {
      overviewDepth.value = 1
      mind.refresh(mapData())
      syncSeekButtons()
      nextTick(() => fitMap(save))
      return
    }
    mind.scale(Math.max(mind.scaleMin, Math.min(1, targetScale)))
    mind.toCenter()
    frameMap()
    if (savedView && savedView.framing_valid !== false && (savedView.resolved_layout || 'balanced') === resolvedLayout.value) {
      mind.scale(Math.min(mind.scaleMax, Math.max(mind.scaleMin, savedView.scale)))
      const root = MindElixir.E('video-mind-map-root', host.value)
      const rect = root.getBoundingClientRect()
      const container = host.value.getBoundingClientRect()
      mind.move(savedView.x * container.width - (rect.left - container.left), savedView.y * container.height - (rect.top - container.top))
    }
    updatePanTravel()
    restoring = false
    if (!renderReported) { renderReported = true; emit('rendered', performance.now() - renderStarted) }
    if (save) captureView()
  })
}

function frameMap() {
  if (!mind || !host.value) return
  const root = MindElixir.E('video-mind-map-root', host.value)
  if (!root) return
  const hostRect = host.value.getBoundingClientRect()
  const anchor = resolvedLayout.value === 'balanced' ? root.getBoundingClientRect() : mind.nodes.getBoundingClientRect()
  mind.move(hostRect.left + hostRect.width / 2 - (anchor.left + anchor.width / 2),
    hostRect.top + hostRect.height / 2 - (anchor.top + anchor.height / 2))
}

function updatePanTravel() {
  if (!mind || !host.value) return
  const hostRect = host.value.getBoundingClientRect()
  const nodesRect = mind.nodes.getBoundingClientRect()
  const padding = mapPadding()
  const travel = nodesRect.width - (hostRect.width - padding * 2)
  panTravel.value = travel > 1 ? travel : 0
  panPosition.value = panTravel.value > 0
    ? Math.min(100, Math.max(0, (hostRect.left + padding - nodesRect.left) / panTravel.value * 100))
    : 0
}

function syncSeekButtons() {
  host.value?.querySelectorAll<HTMLButtonElement>('.video-mind-time').forEach((button) => {
    button.disabled = !props.seekEnabled
  })
}

function renderMap(restore = false, save = false, preserveExpansion = false) {
  if (!mind) return
  renderStarted = performance.now(); renderReported = false
  mind.refresh(mapData(resolvedLayout.value, preserveExpansion))
  nextTick(() => {
    syncSeekButtons()
    fitMap(save, restore)
  })
}

function zoom(delta: number) {
  if (!mind) return
  mind.scale(Math.min(mind.scaleMax, Math.max(mind.scaleMin, mind.scaleVal + delta)))
  requestAnimationFrame(updatePanTravel)
}

function handleHorizontalPan(event: Event) {
  if (!mind || !host.value || !(event.target instanceof HTMLInputElement)) return
  updatePanTravel()
  const next = Number(event.target.value)
  const nodesRect = mind.nodes.getBoundingClientRect()
  const hostRect = host.value.getBoundingClientRect()
  mind.move(hostRect.left + mapPadding() - next / 100 * panTravel.value - nodesRect.left, 0)
  updatePanTravel()
}

function setAllExpanded(expanded: boolean) {
  if (!mind || !host.value) return
  displayMode.value = expanded ? 'detail' : 'overview'
  exportScope.value = displayMode.value
  overviewDepth.value = 1
  mind.refresh(toMindElixirData(props.result, { mode: displayMode.value, depth: expanded ? 4 : 1, layout: resolvedLayout.value }))
  syncSeekButtons()
  const root = MindElixir.E('video-mind-map-root', host.value)
  if (root && expanded) mind.expandNodeAll(root, true)
  nextTick(() => fitMap())
}

function setDisplayMode(mode: MindMapDisplayMode) {
  displayMode.value = mode
  exportScope.value = mode
  overviewDepth.value = defaultOverviewDepth()
  selected.value = null
  renderMap(mode === 'detail')
}

function changeLayout() {
  selected.value = null
  if (displayMode.value === 'overview') {
    // Persist only the preference; keep the detailed view's framing and collapsed nodes.
    emit('viewChange', { scale: 1, x: 0.5, y: 0.5, collapsed: [], framing_valid: false, ...props.savedView,
      layout: layoutPreference.value })
  }
  renderMap(false, true, true)
}

async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen()
    else await shell.value?.requestFullscreen()
  } catch { /* The regular canvas remains usable when fullscreen is unavailable. */ }
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
    const exportName = mindMapImageFilename(`${result.title}-${exportScope.value === 'overview' ? '概览' : '完整'}`, format)
    const handle = await chooseFile(exportName)
    const collapsed: string[] = []
    if (mind && displayMode.value === 'overview') {
      const visit = (node: ReturnType<typeof toMindElixirData>['nodeData']) => {
        if (node.expanded === false) collapsed.push(node.id)
        node.children?.forEach(visit)
      }
      visit(mind.getData().nodeData)
    }
    const blob = await createMindMapImage(result, theme, format, exportScope.value === 'detail'
      ? { mode: 'detail', depth: 4, layout: resolvedLayout.value } : { mode: 'overview', layout: resolvedLayout.value,
        depth: displayMode.value === 'overview' ? 4 : defaultOverviewDepth(), collapsed })
    if (disposed) return
    await saveBlob(blob, exportName, handle)
  } catch (failure) {
    if (failure instanceof DOMException && failure.name === 'AbortError') return
    if (!disposed) exportError.value = `${format.toUpperCase()} 导出失败，请重试。`
  } finally {
    exporting.value = false
    if (!disposed) await nextTick(() => exportTrigger.value?.focus())
  }
}

function handleClick(event: MouseEvent) {
  if (!(event.target instanceof Element)) return
  const time = event.target.closest<HTMLButtonElement>('.video-mind-time')
  if (time && host.value?.contains(time) && props.seekEnabled) {
    const timestamp = Number(time.dataset.timestamp)
    if (Number.isFinite(timestamp) && timestamp >= 0) emit('seek', timestamp)
    return
  }
  const label = event.target.closest<HTMLElement>('[data-mind-node-id]')
  if (!label || !host.value?.contains(label)) return
  const find = (nodes: MindMapNode[]): MindMapNode | null => {
    for (const node of nodes) { if (node.id === label.dataset.mindNodeId) return node; const found = find(node.children); if (found) return found }
    return null
  }
  selected.value = find(props.result.nodes)
}

onMounted(() => {
  if (!host.value) return
  mind = new MindElixir({
    el: host.value,
    direction: MindElixir.SIDE,
    editable: false,
    contextMenu: false,
    toolBar: false,
    keypress: false,
    allowUndo: false,
    overflowHidden: false,
    compact: false,
    alignment: 'root',
    scaleMin: 0.2,
    scaleMax: 1.6,
    theme,
  })
  // The library's overflowHidden option also disables pointer controls.
  mind.container.style.overflow = 'hidden'
  overviewDepth.value = defaultOverviewDepth()
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
  let previousWidth = host.value.clientWidth
  let previousHeight = host.value.clientHeight
  resizeObserver = new ResizeObserver(entries => {
    const width = entries[0]?.contentRect.width || 0
    const height = entries[0]?.contentRect.height || 0
    if (width > 0 && height > 0 && (width !== previousWidth || height !== previousHeight)) {
      if (displayMode.value === 'overview') { overviewDepth.value = defaultOverviewDepth(); renderMap() }
      else fitMap(false, true)
    }
    previousWidth = width
    previousHeight = height
  })
  resizeObserver.observe(host.value)
})

watch(() => props.result, () => { overviewDepth.value = defaultOverviewDepth(); selected.value = null; renderMap() }, { deep: true })
watch(() => props.seekEnabled, syncSeekButtons)

onBeforeUnmount(() => {
  disposed = true
  document.removeEventListener('pointerdown', closeExportMenu)
  if (layoutFrame !== undefined) cancelAnimationFrame(layoutFrame)
  resizeObserver?.disconnect()
  host.value?.removeEventListener('click', handleClick)
  mind?.destroy()
  mind = null
})
</script>

<template>
  <div ref="shell" class="video-mind-map-shell" :class="{ 'is-overview': displayMode === 'overview' }">
    <div class="video-mind-map-tools" aria-label="思维导图视图控制">
      <div class="video-mind-map-modes" role="group" aria-label="导图阅读模式"><button type="button" :aria-pressed="displayMode === 'overview'" @click="setDisplayMode('overview')">概览</button><button type="button" :aria-pressed="displayMode === 'detail'" @click="setDisplayMode('detail')">详细</button></div>
      <select v-model="layoutPreference" class="video-mind-map-layout" aria-label="导图布局" title="选择布局" @change="changeLayout"><option value="auto">自动布局</option><option value="balanced">居中双向</option><option value="right">向右展开</option><option value="left">向左展开</option></select>
      <button type="button" aria-label="全部收起" title="全部收起" @click="setAllExpanded(false)"><FoldVertical :size="17" /></button>
      <button type="button" aria-label="全部展开" title="全部展开" @click="setAllExpanded(true)"><UnfoldVertical :size="17" /></button>
      <button type="button" aria-label="缩小" title="缩小" @click="zoom(-0.1)"><Minus :size="17" /></button>
      <button type="button" aria-label="适应画布" title="适应画布" @click="fitMap()"><Maximize2 :size="17" /></button>
      <button type="button" aria-label="放大" title="放大" @click="zoom(0.1)"><Plus :size="17" /></button>
      <button type="button" aria-label="全屏查看思维导图" title="全屏查看" @click="toggleFullscreen"><Expand :size="17" /></button>
      <div ref="exportControl" class="video-mind-map-export-control" @keydown="handleExportKeys" @focusout="handleExportFocus">
        <button ref="exportTrigger" type="button" aria-label="导出思维导图" :title="exporting ? '正在导出' : '导出思维导图'"
          aria-haspopup="menu" :aria-expanded="exportMenuOpen" :aria-busy="exporting" :disabled="exporting"
          @click="toggleExportMenu" @keydown.down.prevent="!exportMenuOpen && toggleExportMenu()">
          <LoaderCircle v-if="exporting" :size="17" class="video-mind-map-export-spinner" />
          <Download v-else :size="17" />
        </button>
        <div v-if="exportMenuOpen" class="video-mind-map-export-menu" role="menu" aria-label="导出格式">
          <div class="video-mind-map-export-scope"><label for="mind-map-export-scope">导出内容</label><select id="mind-map-export-scope" v-model="exportScope"><option value="overview">当前概览图</option><option value="detail">完整详细图</option></select></div>
          <button type="button" role="menuitem" @click="exportImage('svg')"><FileImage :size="16" /><span>导出 SVG</span></button>
          <button type="button" role="menuitem" @click="exportImage('png')"><Image :size="16" /><span>导出 PNG</span></button>
        </div>
      </div>
    </div>
    <p v-if="exportError" class="video-mind-map-export-error" role="alert">{{ exportError }}</p>
    <div ref="host" class="video-mind-map-canvas" aria-label="AI 视频总结思维导图"></div>
    <aside v-if="selected" class="video-mind-map-node-detail" aria-label="节点详情"><div><strong>{{ selected.title }}</strong><button type="button" aria-label="关闭节点详情" @click="selected = null"><X :size="16" /></button></div><p>{{ selected.summary || '暂无补充说明' }}</p><button type="button" class="video-mind-detail-seek" :disabled="!seekEnabled" @click="emit('seek', selected.timestamp)">定位视频 {{ Math.floor(selected.timestamp / 60) }}:{{ String(Math.floor(selected.timestamp % 60)).padStart(2, '0') }}</button><ul v-if="selected.children.length"><li v-for="child in selected.children" :key="child.id"><button type="button" @click="selected = child">{{ child.title }}</button></li></ul></aside>
    <p class="video-mind-map-hint">{{ layoutHint }} · {{ displayMode === 'overview' ? overviewHint : '完整内容可拖拽、缩放，点击时间戳定位视频' }}</p>
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
