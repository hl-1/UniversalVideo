import type { MusicTrack } from '../features/music/musicLibrary'
import { ref } from 'vue'
import { saveResponseToFile, waitForFileSave } from '../features/video/fileSave.ts'
import type { FilePickerHandle } from '../features/video/types.ts'
import { readApiError } from './api/client.ts'
import { isAndroidApp } from './native/bridge.ts'
import { saveWithAndroid, saveBlobWithAndroid } from './native/downloads.ts'

export type DownloadProgress = { phase: 'preparing' | 'transferring' | 'closing'; bytes: number; total: number | null; speed: number; eta: number | null }
export type SaveResult = { status: 'saved' | 'handed_off'; bytes: number }
export type SaveOptions = { handle?: FilePickerHandle | null; controller?: AbortController; fallbackUrl?: string; onProgress?: (progress: DownloadProgress) => void; track?: unknown; nativeTarget?: string }
export type MediaRequest = { url: string; title?: string; format_id?: string; parse_record_id?: string; media_type?: 'video' | 'audio'; audio_format?: 'original' | 'm4a' | 'mp3'; destination?: 'device' | 'library'; compatibility?: boolean }
export type DeliveryPlan = { download_id: string; mode: 'file' | 'relay' | 'direct' | 'task'; filename: string; url: string; fallback_url: string; task_id?: string; destination: 'device' | 'library' }
export const downloadNotice = ref('')
export const activeDownloads = ref(new Map<string, { label: string; progress: string; detail?: DownloadProgress; cancel: () => void }>())
let sessionRevision = 0

export function downloadFilename(name: string) {
  return name.replace(/^(?:import-)?[a-f0-9]{12}\.(?=.+\.[^.]+$)/i, '').replace(/[\x00-\x1f\\/:*?"<>|]/g, ' ').trim() || 'download'
}

export function progressText(progress: DownloadProgress) {
  if (progress.phase === 'preparing') return '正在解析与准备…'
  if (progress.phase === 'closing') return '正在完成文件写入…'
  const size = `${(progress.bytes / 1048576).toFixed(1)}${progress.total ? ` / ${(progress.total / 1048576).toFixed(1)}` : ''} MB`
  const percent = progress.total ? `（${Math.min(99, Math.floor(progress.bytes / progress.total * 100))}%）` : ''
  const speed = ` · ${progress.speed >= 1048576 ? `${(progress.speed / 1048576).toFixed(1)} MB/s` : `${Math.round(progress.speed / 1024)} KB/s`}`
  return `正在保存到本机：${size}${percent}${speed}${progress.eta !== null ? ` · 剩余约 ${Math.ceil(progress.eta)} 秒` : ''}`
}

export function chooseFile(name: string): Promise<FilePickerHandle | null> {
  if (isAndroidApp()) return Promise.resolve(null)
  return window.showSaveFilePicker ? window.showSaveFilePicker({ suggestedName: downloadFilename(name), startIn: 'downloads' }) : Promise.resolve(null)
}

export function nativeDownload(url: string, name: string) {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = downloadFilename(name)
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

async function saveRemoteFileImpl(url: string, name: string, options: SaveOptions = {}): Promise<SaveResult> {
  const controller = options.controller || new AbortController()
  if (isAndroidApp()) return saveWithAndroid('download.file', { url: options.fallbackUrl || url, filename: downloadFilename(name), track: options.track, target: options.nativeTarget }, controller.signal, options.onProgress)
  const handle = options.handle === undefined ? await chooseFile(name) : options.handle
  controller.signal.throwIfAborted()
  if (!handle) {
    nativeDownload(options.fallbackUrl || url, name)
    return { status: 'handed_off', bytes: 0 }
  }
  options.onProgress?.({ phase: 'transferring', bytes: 0, total: null, speed: 0, eta: null })
  let response: Response
  try {
    response = await waitForFileSave(() => fetch(url, { signal: controller.signal }), controller)
    if (!response.ok && options.fallbackUrl && options.fallbackUrl !== url) {
      void response.body?.cancel()
      response = await waitForFileSave(() => fetch(options.fallbackUrl!, { signal: controller.signal }), controller)
    }
  } catch (error) {
    if (!options.fallbackUrl || options.fallbackUrl === url || controller.signal.aborted) throw error
    response = await waitForFileSave(() => fetch(options.fallbackUrl!, { signal: controller.signal }), controller)
  }
  if (!response.ok) throw new Error(await readApiError(response))
  let bytes = 0
  const started = performance.now()
  await saveResponseToFile(response, handle, controller, (written, total) => {
    bytes = written
    const speed = written / Math.max(.01, (performance.now() - started) / 1000)
    options.onProgress?.({ phase: 'transferring', bytes, total, speed, eta: total && speed ? (total - bytes) / speed : null })
  }, () => options.onProgress?.({ phase: 'closing', bytes, total: null, speed: 0, eta: null }))
  return { status: 'saved', bytes }
}

export async function saveRemoteFile(url: string, name: string, options: SaveOptions = {}): Promise<SaveResult> {
  const controller = options.controller || new AbortController()
  const id = `save-${Date.now()}-${Math.random()}`
  const revision = sessionRevision
  const uid = options.track ? (await import('../features/accounts/auth')).account.value?.id : undefined
  const handle = isAndroidApp() ? null : options.handle === undefined ? await chooseFile(name) : options.handle
  activeDownloads.value.set(id, { label: downloadFilename(name), progress: '正在选择保存位置…', cancel: () => controller.abort(new DOMException('下载已取消', 'AbortError')) })
  try {
    const result = await saveRemoteFileImpl(url, name, { ...options, handle, controller, onProgress(progress) {
      const active = activeDownloads.value.get(id)
      if (active) { active.progress = progressText(progress); active.detail = progress }
      options.onProgress?.(progress)
    } })
    if (revision === sessionRevision && result.status === 'saved' && options.track) {
      const library = await import('../features/music/deviceLibrary')
      if (handle?.getFile) await library.registerDeviceFile(options.track as MusicTrack, handle as Required<Pick<FilePickerHandle, 'getFile'>>, uid)
      else if (isAndroidApp()) await library.refreshDeviceLibrary()
      if (uid === (await import('../features/accounts/auth')).account.value?.id) void library.cacheDeviceAssets(options.track as MusicTrack).catch(() => {})
    }
    if (revision === sessionRevision) downloadNotice.value = saveMessage(result)
    return result
  } catch (error) {
    if (revision === sessionRevision) downloadNotice.value = error instanceof DOMException && error.name === 'AbortError' ? '下载已取消。' : error instanceof Error ? error.message : '下载失败。'
    throw error
  } finally { activeDownloads.value.delete(id) }
}

if (typeof window !== 'undefined') window.addEventListener?.('videodream-session-cleared', () => {
  sessionRevision++
  for (const item of activeDownloads.value.values()) item.cancel()
  activeDownloads.value.clear()
  downloadNotice.value = ''
})

export async function saveBlob(blob: Blob, name: string, handle?: FilePickerHandle | null) {
  if (isAndroidApp()) return saveBlobWithAndroid(blob, downloadFilename(name))
  const target = handle === undefined ? await chooseFile(name) : handle
  if (target) {
    await saveResponseToFile(new Response(blob), target, new AbortController(), () => {}, () => {})
    return { status: 'saved' as const, bytes: blob.size }
  }
  const url = URL.createObjectURL(blob)
  nativeDownload(url, name)
  window.setTimeout(() => URL.revokeObjectURL(url), 60000)
  return { status: 'handed_off' as const, bytes: 0 }
}

export async function deliveryComplete(id: string, status: 'completed' | 'handed_off' | 'cancelled' | 'failed' | 'library_saved', bytes = 0) {
  const response = await fetch(`/api/downloads/${id}/complete`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status, transferred_bytes: bytes, completion_source: status === 'completed' ? 'filesystem' : status === 'handed_off' ? 'browser' : 'none' }) })
  if (!response.ok) throw new Error(await readApiError(response))
  const actual = await response.json()
  if (actual.status !== status) throw new Error(actual.status === 'failed' || actual.status === 'cancelled'
    ? '服务端已确认本次下载失败或取消，不能记为已保存，请重新下载。' : '本次保存状态尚未确认，请检查下载结果。')
}

export async function prepareMedia(request: MediaRequest, controller: AbortController, onProgress?: SaveOptions['onProgress'], onTask?: (task: any) => void): Promise<DeliveryPlan> {
  onProgress?.({ phase: 'preparing', bytes: 0, total: null, speed: 0, eta: null })
  const response = await fetch('/api/downloads/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request), signal: controller.signal })
  if (!response.ok) throw new Error(await readApiError(response))
  const plan: DeliveryPlan = await response.json()
  if (plan.mode === 'task') {
    try {
      while (true) {
        controller.signal.throwIfAborted()
        const status = await fetch(`/api/tasks/${plan.task_id}`, { signal: controller.signal })
        if (!status.ok) throw new Error(await readApiError(status))
        const task = await status.json()
        onTask?.(task)
        if (task.status === 'finished') { plan.url = task.download_url; plan.fallback_url = task.download_url; plan.filename = downloadFilename(task.filename || plan.filename); break }
        if (['failed', 'cancelled'].includes(task.status)) throw new Error(task.error || task.message || '资源处理失败。')
        await new Promise<void>((resolve, reject) => {
          const aborted = () => { clearTimeout(timer); reject(controller.signal.reason) }
          const timer = setTimeout(() => { controller.signal.removeEventListener('abort', aborted); resolve() }, 1000)
          controller.signal.addEventListener('abort', aborted, { once: true })
        })
      }
    } catch (error) {
      void deliveryComplete(plan.download_id, controller.signal.aborted ? 'cancelled' : 'failed').catch(() => {})
      throw error
    }
  }
  return plan
}

export async function downloadMedia(request: MediaRequest, options: SaveOptions & { onTask?: (task: any) => void; preparedPlan?: DeliveryPlan } = {}) {
  const controller = options.controller || new AbortController()
  const revision = sessionRevision
  const uid = options.track ? (await import('../features/accounts/auth')).account.value?.id : undefined
  const ext = request.media_type === 'audio' ? request.audio_format === 'original' ? 'm4a' : request.audio_format || 'm4a' : 'mp4'
  const id = `media-${Date.now()}-${Math.random()}`
  activeDownloads.value.set(id, { label: downloadFilename(`${request.title || 'download'}.${ext}`), progress: '正在选择保存位置…', cancel: () => controller.abort(new DOMException('下载已取消', 'AbortError')) })
  const onProgress: NonNullable<SaveOptions['onProgress']> = progress => {
    const active = activeDownloads.value.get(id)
    if (active) { active.progress = progressText(progress); active.detail = progress }
    options.onProgress?.(progress)
  }
  const onTask = (task: any) => {
    const active = activeDownloads.value.get(id)
    if (active && task.status !== 'finished') active.progress = `正在准备文件${Number.isFinite(task.progress) ? `：${Math.round(task.progress)}%` : '…'}`
    options.onTask?.(task)
  }
  let plan: DeliveryPlan | undefined
  try {
    if (isAndroidApp() && request.destination !== 'library') {
      const result = await saveWithAndroid<SaveResult & { plan: DeliveryPlan }>('download.media', {
        media: request, filename: downloadFilename(`${request.title || 'download'}.${ext}`), track: options.track, target: options.nativeTarget,
      }, controller.signal, onProgress)
      if (revision === sessionRevision && result.status === 'saved' && options.track) {
        const library = await import('../features/music/deviceLibrary')
        await library.refreshDeviceLibrary()
        void library.cacheDeviceAssets(options.track as MusicTrack).catch(() => {})
      }
      if (revision === sessionRevision) downloadNotice.value = saveMessage(result)
      return result
    }
    // Invoke the picker during the click, before source parsing or any request.
    const handle = request.destination === 'library' ? null : options.handle === undefined ? await chooseFile(`${request.title || 'download'}.${ext}`) : options.handle
    controller.signal.throwIfAborted()
    plan = options.preparedPlan || await prepareMedia(request, controller, onProgress, onTask)
    const active = activeDownloads.value.get(id)
    if (active) active.label = downloadFilename(plan.filename)
    if (request.destination === 'library') {
      await deliveryComplete(plan.download_id, 'library_saved')
      if (revision === sessionRevision) downloadNotice.value = saveMessage({ status: 'library' })
      return { plan, status: 'library' as const, bytes: 0 }
    }
    let result: SaveResult
    try { result = await saveRemoteFileImpl(plan.url, plan.filename, { ...options, controller, handle, onProgress, fallbackUrl: plan.fallback_url }) }
    catch (error) {
      if (controller.signal.aborted || request.compatibility || !handle || request.media_type !== 'audio' || plan.mode === 'task') throw error
      // Restart from byte zero after aborting the old writer; never append data
      // from a different source or a different encoding to a partial file.
      await deliveryComplete(plan.download_id, 'failed').catch(() => {})
      plan = await prepareMedia({ ...request, compatibility: true }, controller, onProgress, onTask)
      result = await saveRemoteFileImpl(plan.url, plan.filename, { ...options, controller, handle, onProgress, fallbackUrl: plan.fallback_url })
    }
    await deliveryComplete(plan.download_id, result.status === 'saved' ? 'completed' : 'handed_off', result.bytes)
    if (revision === sessionRevision && result.status === 'saved' && options.track && handle?.getFile) {
      const library = await import('../features/music/deviceLibrary')
      await library.registerDeviceFile(options.track as MusicTrack, handle as Required<Pick<FilePickerHandle, 'getFile'>>, uid)
      if (revision === sessionRevision) void library.cacheDeviceAssets(options.track as MusicTrack).catch(() => {})
    }
    if (revision === sessionRevision) downloadNotice.value = saveMessage(result)
    return { ...result, plan }
  } catch (error) {
    if (revision === sessionRevision) downloadNotice.value = error instanceof DOMException && error.name === 'AbortError' ? '下载已取消。' : error instanceof Error ? error.message : '保存状态未确认。'
    if (plan) void deliveryComplete(plan.download_id, controller.signal.aborted ? 'cancelled' : 'failed').catch(() => {})
    throw error
  } finally { activeDownloads.value.delete(id) }
}

export const saveMessage = (result: { status: string }) => result.status === 'saved' ? '已保存到你选择的位置。' : result.status === 'library' ? '已保存到云端资料库。' : '已交给浏览器下载，请在浏览器下载列表查看结果。'
