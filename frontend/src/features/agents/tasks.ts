import { api, jsonRequest } from '../../shared/api/client.ts'

export interface AgentTask {
  id: string; user_id: string; kind: string; status: string; revision: number
  graph_version: string; skill_version: string; created_at: string; updated_at: string
  message: string; error?: string; node: string; next_event_seq: number
  usage: { actual?: number; reserved?: number; seconds?: number; actions?: number }
  limits: { tokens: number; seconds: number; actions: number }
  result?: { answer?: string; citations?: Citation[]; insufficient?: boolean; playlist_id?: string; track_count?: number; chunk_count?: number }
  observation?: { revision: number; url: string; title: string; text: string }
  actions?: AgentAction[]; children?: Array<{ id: string; kind: string; status: string }>
  model_calls?: Array<{ id: string; phase: string; model: string; state: string; actual_tokens?: number; reserved_tokens: number }>
}
export interface Citation {
  chunk_id: string; document_id: string; kb_id: string; version: number; content: string
  locator: { page?: number; section?: string; start_ms?: number; end_ms?: number; offset_start?: number }
}
export interface AgentAction {
  id: string; kind: string; tool: string; status: string; approval_state: string
  payload_hash: string; approval_expires_at?: string
  arguments: { name?: string; source_url?: string; tracks?: Array<{ title: string; artist: string }> }
}
export const terminal = (status: string) => ['succeeded', 'partial', 'failed', 'cancelled'].includes(status)
export const statusLabels: Record<string, string> = {
  queued: '排队中', running: '执行中', succeeded: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消',
  waiting_input: '等待补充', waiting_auth: '等待登录', waiting_approval: '等待审批', waiting_usage: '用量待核对',
  waiting_children: '等待子任务', interrupted: '已中断',
}
export const kindLabels: Record<string, string> = {
  music_scene: '场景歌单', music_profile: '音乐画像', knowledge_ingest: '文档入库', knowledge_query: '知识问答', browser_playlist: '浏览器歌单导入',
}
export const requestKey = () => crypto.randomUUID()
export function versionRequest(task: AgentTask, action: string) {
  return api(`/api/agent/tasks/${task.id}/${action}`, jsonRequest('POST', { revision: task.revision }))
}
export function parseEvents(buffer: string): { events: Array<{ id: number; type: string; data: unknown }>; rest: string } {
  const normalized = buffer.replace(/\r\n/g, '\n')
  const blocks = normalized.split('\n\n')
  const rest = blocks.pop() || ''
  const events = blocks.flatMap(block => {
    const lines = block.split('\n')
    const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
    if (!data) return []
    const id = Number(lines.find(line => line.startsWith('id:'))?.slice(3).trim())
    if (!Number.isSafeInteger(id) || id < 1) return []
    try { return [{ id, type: lines.find(line => line.startsWith('event:'))?.slice(6).trim() || 'message', data: JSON.parse(data) }] }
    catch { return [] }
  })
  return { events, rest }
}
export async function subscribe(taskId: string, after: number, signal: AbortSignal, receive: (id: number, value: any) => void) {
  const response = await fetch(`/api/agent/tasks/${taskId}/events?after=${after}`, { signal, headers: { Accept: 'text/event-stream' } })
  if (!response.ok || !response.body) throw new Error('任务事件连接暂不可用')
  const reader = response.body.getReader(), decoder = new TextDecoder()
  let buffer = ''
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read()
      if (done) return
      buffer += decoder.decode(value, { stream: true })
      const parsed = parseEvents(buffer)
      buffer = parsed.rest
      for (const event of parsed.events) receive(event.id, event.data)
    }
  } finally { reader.releaseLock() }
}
export async function sourceFile(citation: Citation) {
  const response = await fetch(`/api/knowledge/documents/${citation.document_id}/versions/${citation.version}/source`)
  if (!response.ok) throw new Error('原文不可用，权限或版本可能已变化')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = `source-${citation.document_id}`; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
