export type MindMapStatus = 'pending' | 'running' | 'finished' | 'failed'

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
}

const statuses: MindMapStatus[] = ['pending', 'running', 'finished', 'failed']
const branchColors = ['#2563eb', '#f59e0b', '#22c55e', '#ec4899', '#8b5cf6', '#06b6d4']

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

function toMindElixirNode(node: MindMapNode, branchColor: string, depth: number): NodeObj<{ timestamp: number }> {
  const title = escapeHtml(node.title)
  const summary = escapeHtml(node.summary)
  const timestamp = formatMindMapTimestamp(node.timestamp)
  return {
    id: node.id,
    topic: node.title,
    direction: 1,
    branchColor,
    expanded: true,
    metadata: { timestamp: node.timestamp },
    dangerouslySetInnerHTML: [
      '<div class="video-mind-node">',
      `<div class="video-mind-node-heading"><span>${title}</span><button type="button" class="video-mind-time" data-timestamp="${node.timestamp}">${timestamp}</button></div>`,
      summary ? `<p>${summary}</p>` : '',
      '</div>',
    ].join(''),
    children: node.children.map((child) => toMindElixirNode(child, branchColor, depth + 1)),
  }
}

export function toMindElixirData(result: MindMapResult): MindElixirData {
  return {
    direction: 1,
    nodeData: {
      id: 'video-mind-map-root',
      topic: result.title,
      expanded: true,
      children: result.nodes.map((node, index) => toMindElixirNode(node, branchColors[index % branchColors.length], 1)),
    },
  }
}
import type { MindElixirData, NodeObj } from 'mind-elixir'
