import { ref } from 'vue'
import { api, jsonRequest } from '../../shared/api/client'

export type SocialTab = 'messages' | 'friends' | 'listen' | 'karaoke' | 'rules' | 'duets' | 'playlists' | 'settings'
export const socialEvent = ref<Record<string, any> | null>(null)
export const connectionState = ref<'connecting' | 'online' | 'offline'>('offline')
export const unread = ref(0)
export const shareRequest = ref<{ kind: 'track' | 'playlist' | 'rule' | 'performance' | 'room'; key: string; title: string } | null>(null)
export const socialNavigation = ref<{ tab: SocialTab; id?: string; code?: string } | null>(null)
let socket: WebSocket | null = null
let retry: ReturnType<typeof setTimeout> | undefined
let enabled = false
let attempt = 0
let epoch = 0

export function request<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  return api<T>('/api/social' + path, method === 'GET' ? undefined : jsonRequest(method, body))
}
export function msgId() { return crypto.randomUUID().replaceAll('-', '') }
export function failure(error: unknown) { return error instanceof Error ? error.message : '操作失败，请重试。' }
export function assetUrl(id: string) { return `/api/social/assets/${id}/content` }
export function openShare(kind: 'track' | 'playlist' | 'rule' | 'performance' | 'room', key: string, title: string) { shareRequest.value = { kind, key, title } }
export async function upload(file: Blob, kind: string, filename = '录音.webm') {
  const form = new FormData()
  form.append('file', file, file instanceof File ? file.name : filename)
  return api<any>(`/api/social/assets?kind=${kind}`, { method: 'POST', body: form })
}
export async function updateUnread() {
  const data = await request('/conversations')
  unread.value = data.items.filter((x: any) => !x.muted_until).reduce((sum: number, x: any) => sum + x.unread, 0)
}
async function connect(revision: number) {
  if (!enabled || revision !== epoch) return
  connectionState.value = 'connecting'
  try {
    const ticket = await request('/ws-ticket', 'POST', {})
    if (!enabled || revision !== epoch) return
    const url = new URL('/api/social/ws', location.href)
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    url.searchParams.set('ticket', ticket.ticket)
    const ws = new WebSocket(url)
    socket = ws
    ws.onopen = () => { attempt = 0; connectionState.value = 'online'; socialEvent.value = { topic: 'reconnected', event_id: msgId() }; void updateUnread().catch(() => {}) }
    ws.onmessage = event => {
      let data: any
      try { data = JSON.parse(event.data) } catch { return }
      if (data.topic === 'heartbeat') { ws.send('{}'); return }
      socialEvent.value = data
      if (['message', 'read', 'conversation'].includes(data.topic)) void updateUnread().catch(() => {})
    }
    ws.onclose = () => {
      if (socket !== ws) return
      connectionState.value = 'offline'
      if (enabled && revision === epoch) retry = setTimeout(() => void connect(revision), Math.min(30000, 1000 * 2 ** Math.min(attempt++, 5)))
    }
    ws.onerror = () => ws.close()
  } catch {
    connectionState.value = 'offline'
    if (enabled && revision === epoch) retry = setTimeout(() => void connect(revision), 5000)
  }
}
export function startSocial() { if (!enabled) { enabled = true; epoch++; void connect(epoch) } }
export function stopSocial() { enabled = false; epoch++; clearTimeout(retry); socket?.close(); socket = null; unread.value = 0; shareRequest.value = null; socialNavigation.value = null; socialEvent.value = null; connectionState.value = 'offline' }
window.addEventListener('videodream-session-cleared', stopSocial)
