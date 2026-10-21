import { trackKey, trackPayload, type MusicTrack } from './musicLibrary'
import { api, jsonRequest } from '../../shared/api/client'
import { ref, watch } from 'vue'
import { account } from '../accounts/auth'
import { deliveryComplete, downloadMedia, prepareMedia, progressText, type DeliveryPlan, type DownloadProgress, type MediaRequest } from '../../shared/downloads'
import { isMusicDownloaded, refreshMusicDownloadStatus, rememberMusicDownloads } from './musicDownloads'

type AudioFormat = 'original' | 'mp3' | 'm4a'
type Destination = { playlist_id?: string; playlist_name?: string; cloud?: boolean; local_mode?: 'files' | 'directory' }
type ItemStatus = 'pending' | 'running' | 'finished' | 'handed_off' | 'failed' | 'cancelled'
export type DeviceBatchItem = MusicTrack & { artist: string; provider: string; task_id: string; status: ItemStatus; progress: number; transfer?: DownloadProgress; error?: string; download_id?: string; download_url?: string; message: string }
export type DeviceBatch = {
  batch_id: string; device: true; status: 'running' | 'finished' | 'failed' | 'cancelled'; audio_format: AudioFormat;
  total: number; finished: number; handed_off: number; failed: number; cancelled: number; progress: number;
  items: DeviceBatchItem[]; delivery_status?: string; message?: string
}
export const deviceBatch = ref<DeviceBatch | null>(null)
let batchController: AbortController | undefined
let retryTracks: MusicTrack[] = []
let retryFormat: AudioFormat = 'mp3'
let retryDestination: Destination = {}
export function cancelDeviceBatch() { batchController?.abort(new DOMException('下载已取消', 'AbortError')) }
export function retryDeviceBatch() { return createMusicBatch(retryTracks, retryFormat, undefined, retryDestination) }
watch(() => account.value?.id, () => { cancelDeviceBatch(); deviceBatch.value = null; retryTracks = [] }, { flush: 'sync' })

// Choose one local folder before resolving songs. Native downloads remain an
// explicit option and the fallback for browsers without directory access.
export async function createMusicBatch(tracks: MusicTrack[], audioFormat: AudioFormat, signal?: AbortSignal, destination: Destination = {}) {
  const unique = [...new Map(tracks.map(track => [trackKey(track), track])).values()].filter(track => !isMusicDownloaded(track))
  if (!unique.length) throw new Error('所选歌曲已下载，无需重复下载。')
  if (!destination.cloud) return createDeviceBatch(unique, audioFormat, destination, signal)
  const requests: Array<{ token: string; webpage_url: string }> = []
  for (const track of unique) {
    if (track.provider === 'local' || track.local_url || track.filename) throw new Error('本地歌曲已有文件，请使用歌曲旁的文件下载入口。')
    const resolved = await api<MusicTrack>('/api/music/parse', { ...jsonRequest('POST', { url: track.webpage_url }), signal })
    if (!resolved.token) throw new Error(`无法取得“${track.title}”的下载凭据。`)
    requests.push({ token: resolved.token, webpage_url: resolved.webpage_url })
  }
  return api<{ batch_id: string }>('/api/music/batches', { ...jsonRequest('POST', { tracks: requests, audio_format: audioFormat, ...destination }), signal })
}

declare global { interface Window { showDirectoryPicker?: (options?: { mode: 'readwrite'; startIn: 'downloads' }) => Promise<FileSystemDirectoryHandle> } }
async function createDeviceBatch(tracks: MusicTrack[], audioFormat: AudioFormat, destination: Destination, signal?: AbortSignal) {
  if (batchController) throw new Error('已有批量下载正在进行。')
  const controller = new AbortController()
  batchController = controller
  const uid = account.value?.id
  const isCurrent = () => batchController === controller && account.value?.id === uid
  const ensureCurrent = () => { controller.signal.throwIfAborted(); if (!isCurrent()) throw new DOMException('账号已切换', 'AbortError') }
  const abort = () => controller.abort(signal?.reason)
  signal?.addEventListener('abort', abort, { once: true })
  if (signal?.aborted) abort()
  let batch: DeviceBatch | undefined
  const setBatch = (changes: Partial<DeviceBatch> = {}) => { if (batch) { Object.assign(batch, changes); if (isCurrent()) deviceBatch.value = { ...batch } } }
  try {
    ensureCurrent()
    const directory = destination.local_mode !== 'files' && window.showDirectoryPicker ? await window.showDirectoryPicker({ mode: 'readwrite', startIn: 'downloads' }) : null
    ensureCurrent()
    const statuses = await refreshMusicDownloadStatus(tracks, controller.signal)
    ensureCurrent()
    const pending = tracks.filter(track => !isMusicDownloaded({ ...track, ...statuses.get(trackKey(track)) }))
    if (!pending.length) throw new Error('所选歌曲已下载，无需重复下载。')
    retryFormat = audioFormat; retryDestination = destination; retryTracks = pending
    batch = { batch_id: `device-${Date.now()}`, device: true, status: 'running', audio_format: audioFormat,
      total: pending.length, finished: 0, handed_off: 0, failed: 0, cancelled: 0, progress: 0,
      message: directory ? `正在逐首保存到所选文件夹${directory.name ? `“${directory.name}”` : ''}…` : `${destination.local_mode !== 'files' ? '此浏览器不支持选择文件夹，改用浏览器下载。' : ''}正在逐首准备文件…`,
      items: pending.map(track => ({ ...track, artist: track.artist || '', provider: track.provider || 'video', task_id: trackKey(track), status: 'pending', progress: 0, message: '' })) }
    setBatch()
    if (destination.playlist_name || destination.playlist_id) {
      const playlist = destination.playlist_id || (await api<{ id: string }>('/api/library/playlists', { ...jsonRequest('POST', { name: destination.playlist_name }), signal: controller.signal })).id
      for (const track of pending) { ensureCurrent(); await api(`/api/library/playlists/${playlist}/tracks`, { ...jsonRequest('POST', trackPayload(track)), signal: controller.signal }) }
    }
    let next = 0
    const reservedNames = new Set<string>()
    const worker = async () => {
      while (next < pending.length && !controller.signal.aborted) {
        const index = next++, track = pending[index]!, item = batch!.items[index]!
        const itemController = new AbortController()
        const abortItem = () => itemController.abort(controller.signal.reason)
        controller.signal.addEventListener('abort', abortItem, { once: true })
        let plan: DeliveryPlan | undefined, targetName: string | undefined, createdTarget = false
        item.status = 'running'; setBatch()
        try {
          const resolved = await api<MusicTrack>('/api/music/parse', { ...jsonRequest('POST', { url: track.webpage_url }), signal: itemController.signal })
          const request: MediaRequest = { url: resolved.webpage_url, format_id: resolved.token ? `music:${resolved.token}` : resolved.formats?.[0]?.format_id,
            media_type: 'audio', audio_format: audioFormat, title: track.title,
            // A native browser download cannot abort a partial writer when the
            // upstream fails. Validate this one song first, then serve its file.
            compatibility: !directory }
          plan = await prepareMedia(request, itemController, progress => { item.message = progressText(progress); setBatch() }, task => { item.progress = task.progress || 0; setBatch() })
          ensureCurrent()
          item.filename = plan.filename
          let target = null
          if (directory) {
            const base = plan.filename
            targetName = base
            let suffix = 1
            while (true) {
              if (!reservedNames.has(targetName)) {
                reservedNames.add(targetName)
                try { await directory.getFileHandle(targetName) }
                catch (error) { if (error instanceof DOMException && error.name === 'NotFoundError') break; throw error }
              }
              targetName = base.replace(/(\.[^.]+)$/, ` (${++suffix})$1`)
            }
            target = await directory.getFileHandle(targetName, { create: true })
            createdTarget = true
          }
          const result = await downloadMedia(request, { handle: target, controller: itemController, preparedPlan: plan,
            onProgress(progress) { item.transfer = progress; item.message = progressText(progress); setBatch({ message: `${track.title} · ${item.message}` }) } })
          item.download_id = result.plan.download_id
          item.progress = 100
          if (result.status === 'saved') {
            rememberMusicDownloads([track], uid, { bytes: result.bytes, download_id: result.plan.download_id }); item.status = 'finished'; batch!.finished++
          } else {
            item.status = 'handed_off'; item.download_url = result.plan.url; batch!.handed_off++
          }
        } catch (error) {
          if (plan) void deliveryComplete(plan.download_id, controller.signal.aborted ? 'cancelled' : 'failed').catch(() => {})
          if (directory && createdTarget && targetName) await directory.removeEntry(targetName).catch(() => {})
          item.status = controller.signal.aborted ? 'cancelled' : 'failed'
          item.error = error instanceof Error ? error.message : '下载失败'
          if (controller.signal.aborted) batch!.cancelled++; else batch!.failed++
        } finally { controller.signal.removeEventListener('abort', abortItem) }
        setBatch({ progress: (batch!.finished + batch!.handed_off + batch!.failed + batch!.cancelled) / batch!.total * 100 })
      }
    }
    await Promise.all(Array.from({ length: directory ? 2 : 1 }, worker))
    for (const item of batch.items) if (item.status === 'pending') { item.status = 'cancelled'; batch.cancelled++ }
    if (isCurrent()) retryTracks = pending.filter((_, index) => ['failed', 'cancelled'].includes(batch!.items[index]!.status))
    const message = [batch.finished ? `已保存 ${batch.finished} 首` : '', batch.handed_off ? `已将 ${batch.handed_off} 首分别交给浏览器下载，请在下载列表确认；如被拦截，可点击各首的“保存此歌曲”` : '', batch.failed ? `失败 ${batch.failed} 首，可单独重试` : '', batch.cancelled ? `已取消 ${batch.cancelled} 首` : ''].filter(Boolean).join('，')
    setBatch({ status: controller.signal.aborted ? 'cancelled' : batch.failed ? 'failed' : 'finished',
      delivery_status: controller.signal.aborted ? 'cancelled' : batch.failed ? 'failed' : batch.handed_off ? 'handed_off' : 'saved', message,
      progress: (batch.finished + batch.handed_off + batch.failed + batch.cancelled) / batch.total * 100 })
    return { batch_id: batch.batch_id }
  } catch (error) {
    if (batch) setBatch({ status: controller.signal.aborted ? 'cancelled' : 'failed', message: error instanceof Error ? error.message : '批量下载失败' })
    throw error
  } finally { signal?.removeEventListener('abort', abort); if (batchController === controller) batchController = undefined }
}
