import { filterTiming, type ExecutionTiming, type ModelInfo } from './executionTiming.ts'
export type MindMapStatus = 'pending' | 'running' | 'finished' | 'failed' | 'cancelled'

export type MindMapNode = {
  id: string
  title: string
  summary: string
  timestamp: number
  segment_ids: number[]
  children: MindMapNode[]
}

export type MindMapResult = {
  title: string
  nodes: MindMapNode[]
  generated_at: string
}

export type MindMapTask = {
  task_id: string
  summary_task_id: string
  status: MindMapStatus
  progress: number
  message: string
  error?: string | null
  result?: MindMapResult | null
  version?: number
  timing?: ExecutionTiming | null
  model_info?: ModelInfo | null
}

const statuses: MindMapStatus[] = ['pending', 'running', 'finished', 'failed', 'cancelled']
const branchColors = ['#2563eb', '#f59e0b', '#22c55e', '#ec4899', '#8b5cf6', '#06b6d4']
export type MindMapDisplayMode = 'overview' | 'detail'
export type MindMapDisplayOptions = { mode?: MindMapDisplayMode; depth?: number; collapsed?: string[] }

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function filterNode(value: unknown, depth: number): MindMapNode | null {
  if (!isRecord(value) || depth > 4) return null
  const { id, title, summary, timestamp, segment_ids: segmentIds, children } = value
  if (
    typeof id !== 'string' ||
    !id.trim() ||
    typeof title !== 'string' ||
    !title.trim() ||
    typeof timestamp !== 'number' ||
    !Number.isFinite(timestamp) ||
    timestamp < 0 ||
    !Array.isArray(segmentIds) ||
    !segmentIds.every((item) => Number.isInteger(item) && item >= 0) ||
    !Array.isArray(children)
  ) {
    return null
  }
  return {
    id: id.trim(),
    title: title.trim().slice(0, 80),
    summary: typeof summary === 'string' ? summary.trim().slice(0, 300) : '',
    timestamp,
    segment_ids: segmentIds,
    children: depth === 4 ? [] : filterNodes(children, depth + 1),
  }
}

function filterNodes(values: unknown[], depth: number): MindMapNode[] {
  const nodes: MindMapNode[] = []
  for (const value of values) {
    const node = filterNode(value, depth)
    if (node) nodes.push(node)
    if (nodes.length === 10) break
  }
  return nodes
}

function filterResult(value: unknown): MindMapResult | null {
  if (!isRecord(value) || typeof value.title !== 'string' || !value.title.trim() || !Array.isArray(value.nodes)) return null
  const nodes = filterNodes(value.nodes, 1)
  if (!nodes.length) return null
  return {
    title: value.title.trim().slice(0, 80),
    nodes,
    generated_at: typeof value.generated_at === 'string' ? value.generated_at : '',
  }
}

export function filterMindMapTask(value: unknown): MindMapTask | null {
  if (!isRecord(value) || typeof value.task_id !== 'string' || !statuses.includes(value.status as MindMapStatus)) return null
  const progress = typeof value.progress === 'number' && Number.isFinite(value.progress) ? Math.min(100, Math.max(0, value.progress)) : 0
  const result = value.result == null ? null : filterResult(value.result)
  if (value.status === 'finished' && !result) return null
  return {
    task_id: value.task_id,
    summary_task_id: typeof value.summary_task_id === 'string' ? value.summary_task_id : '',
    status: value.status as MindMapStatus,
    progress,
    message: typeof value.message === 'string' ? value.message : '',
    error: typeof value.error === 'string' ? value.error : null,
    result,
    version: typeof value.version === 'number' && Number.isInteger(value.version) ? value.version : 1,
    timing: filterTiming(value.timing),
    model_info: isRecord(value.model_info) && typeof value.model_info.model === 'string'
      ? { model: value.model_info.model, service: typeof value.model_info.service === 'string' ? value.model_info.service : null } : null,
  }
}

export function clampSeekTime(timestamp: number, duration: number): number {
  if (!Number.isFinite(timestamp)) return 0
  const safeTimestamp = Math.max(0, timestamp)
  return Number.isFinite(duration) && duration > 0 ? Math.min(safeTimestamp, duration) : safeTimestamp
}

export function canSeekMedia(media: { readyState: number; duration: number }): boolean {
  return media.readyState >= 1 && Number.isFinite(media.duration) && media.duration > 0
}

export function mindMapPanDelta(previous: number, next: number, travel: number): number {
  if (!Number.isFinite(travel) || travel <= 0) return 0
  return -((next - previous) / 100) * travel
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function formatMindMapTimestamp(timestamp: number): string {
  const totalSeconds = Math.max(0, Math.floor(timestamp))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    : `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function branchWeight(node: MindMapNode, depth: number, limit: number): number {
  const own = Math.max(1, Math.ceil(node.title.length / 12))
  return depth >= limit || !node.children.length ? own
    : Math.max(own, node.children.reduce((sum, child) => sum + branchWeight(child, depth + 1, limit), 0))
}

export function mindMapDirections(nodes: MindMapNode[], depth = 2): (0 | 1)[] {
  const heights = [0, 0]
  const directions: (0 | 1)[] = nodes.map(() => 1)
  // Place larger branches first, keeping the original order within each side.
  nodes.map((node, index) => ({ index, weight: branchWeight(node, 1, depth) }))
    .sort((a, b) => b.weight - a.weight || a.index - b.index).forEach(({ index, weight }) => {
      const side = heights[1]! <= heights[0]! ? 1 : 0
      directions[index] = side
      heights[side] = heights[side]! + weight + 1
    })
  return directions
}

function compactLabel(title: string, maximum: number) {
  const characters = Array.from(title)
  return characters.length > maximum ? characters.slice(0, maximum - 1).join('') + '…' : title
}

function toMindElixirNode(node: MindMapNode, branchColor: string, depth: number, direction: 0 | 1,
  options: Required<MindMapDisplayOptions>): NodeObj<{ timestamp: number }> {
  const title = escapeHtml(node.title)
  const label = escapeHtml(options.mode === 'overview' ? compactLabel(node.title, 18) : node.title)
  const summary = escapeHtml(node.summary)
  const timestamp = formatMindMapTimestamp(node.timestamp)
  return {
    id: node.id,
    topic: node.title,
    direction,
    branchColor,
    style: depth === 1 ? { border: `1.5px solid ${branchColor}`, background: '#ffffff' } : undefined,
    expanded: depth < options.depth && !options.collapsed.includes(node.id),
    metadata: { timestamp: node.timestamp },
    dangerouslySetInnerHTML: [
      '<div class="video-mind-node">',
      `<div class="video-mind-node-heading"><button type="button" class="video-mind-label" data-mind-node-id="${escapeHtml(node.id)}" title="${title}">${label}</button>`,
      options.mode === 'detail' ? `<button type="button" class="video-mind-time" data-timestamp="${node.timestamp}">${timestamp}</button>` : '',
      '</div>',
      summary && options.mode === 'detail' ? `<p>${summary}</p>` : '',
      '</div>',
    ].join(''),
    children: node.children.map((child) => toMindElixirNode(child, branchColor, depth + 1, direction, options)),
  }
}

export function toMindElixirData(result: MindMapResult, display: MindMapDisplayOptions = {}): MindElixirData {
  const mode = display.mode || 'overview'
  const options = { mode, depth: display.depth ?? (mode === 'overview' ? 2 : 4), collapsed: display.collapsed || [] }
  const directions = mindMapDirections(result.nodes, options.depth)
  return {
    direction: 2,
    nodeData: {
      id: 'video-mind-map-root',
      topic: result.title,
      dangerouslySetInnerHTML: `<div class="video-mind-root" title="${escapeHtml(result.title)}">${escapeHtml(mode === 'overview' ? compactLabel(result.title, 22) : result.title)}</div>`,
      expanded: true,
      children: result.nodes.map((node, index) => toMindElixirNode(node, branchColors[index % branchColors.length]!, 1, directions[index]!, options)),
    },
  }
}
import type { MindElixirData, NodeObj } from 'mind-elixir'
