<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MindElixir, { type MindElixirInstance, type Theme } from 'mind-elixir'
import 'mind-elixir/style.css'
import { mindMapPanDelta, toMindElixirData, type MindMapResult } from './mindMap'

const props = defineProps<{ result: MindMapResult; seekEnabled?: boolean }>()
const emit = defineEmits<{ seek: [timestamp: number] }>()

const host = ref<HTMLDivElement | null>(null)
const panPosition = ref(0)
const panTravel = ref(0)
let mind: MindElixirInstance | null = null

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

function fitMap() {
  requestAnimationFrame(() => {
    if (!mind || !host.value) return
    mind.scaleFit()
    const targetScale = host.value.clientWidth >= 760
      ? mind.scaleVal * 1.18
      : Math.max(0.6, mind.scaleVal * 1.2)
    mind.toCenter()
    mind.scale(Math.min(1.35, targetScale))
    requestAnimationFrame(alignMapLeft)
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
  mind.refresh(toMindElixirData(props.result))
  nextTick(() => {
    syncSeekButtons()
    fitMap()
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
  nextTick(fitMap)
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
  mind.init(toMindElixirData(props.result))
  host.value.addEventListener('click', handleClick)
  nextTick(() => {
    syncSeekButtons()
    fitMap()
  })
})

watch(() => props.result, renderMap, { deep: true })
watch(() => props.seekEnabled, syncSeekButtons)

onBeforeUnmount(() => {
  host.value?.removeEventListener('click', handleClick)
  mind?.destroy()
  mind = null
})
</script>

<template>
  <div class="video-mind-map-shell">
    <div class="video-mind-map-tools" aria-label="思维导图视图控制">
      <button type="button" aria-label="全部收起" title="全部收起" @click="setAllExpanded(false)">⊟</button>
      <button type="button" aria-label="全部展开" title="全部展开" @click="setAllExpanded(true)">⊞</button>
      <button type="button" aria-label="缩小" title="缩小" @click="zoom(-0.1)">−</button>
      <button type="button" aria-label="适应画布" title="适应画布" @click="fitMap">⌾</button>
      <button type="button" aria-label="放大" title="放大" @click="zoom(0.1)">+</button>
    </div>
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
