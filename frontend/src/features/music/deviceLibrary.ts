import { ref, watch } from 'vue'
import { account } from '../accounts/auth'
import { currentDeviceId, deviceContext } from '../../shared/device'
import { trackKey, trackPayload, type MusicTrack } from './musicLibrary'
import { inspectDeviceAudio } from './deviceAudio'
import type { Lyrics } from './lyrics'

type FileHandle = { getFile(): Promise<File>; isSameEntry?(other: FileHandle): Promise<boolean>; queryPermission?(options: { mode: 'read' }): Promise<PermissionState>; requestPermission?(options: { mode: 'read' }): Promise<PermissionState> }
type DeviceEntry = { key: string; uid: number; device_id: string; track: MusicTrack; handle?: FileHandle; file?: File; cover?: Blob; lyrics?: Lyrics; manual_lyrics?: boolean; asset_notice?: string; bytes: number; modified?: number; added_at: number }
type UnavailableEntry = { entry: DeviceEntry; reason: string; canAuthorize: boolean }
export const deviceTracks = ref<MusicTrack[]>([])
export const unavailableDeviceTracks = ref<Array<{ track: MusicTrack; reason: string; canAuthorize: boolean }>>([])
export const deviceLibraryRevision = ref(0)
export const deviceLibraryError = ref('')
const entries = new Map<string, DeviceEntry>()
const unavailable = new Map<string, UnavailableEntry>()
const persistenceErrors = new Map<string, string>()
const files = new Map<string, File>()
const objectUrls = new Map<string, string>()
const coverUrls = new Map<string, { blob: Blob; url: string }>()
const assetRequests = new Map<string, AbortController>()
let generation = 0
let refreshSequence = 0
let database: Promise<IDBDatabase> | undefined
const audioExtension = /\.(mp3|m4a|flac|wav|ogg|aac|opus)$/i
const entryKey = (uid: number, device: string, track: MusicTrack) => `${uid}:${device}:${trackKey(track)}`
async function importedCopy(file: File) {
  return new File([await new Response(file.stream()).blob()], file.name, { type: file.type, lastModified: file.lastModified })
}

function openDatabase() {
  return database ||= new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('videodream-device-library', 1)
    request.onupgradeneeded = () => request.result.createObjectStore('tracks', { keyPath: 'key' })
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  }).catch(error => { database = undefined; throw error })
}
async function store(mode: IDBTransactionMode, operation: (store: IDBObjectStore) => IDBRequest): Promise<any> {
  const db = await openDatabase()
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('tracks', mode)
    const request = operation(transaction.objectStore('tracks'))
    transaction.oncomplete = () => resolve(request.result)
    transaction.onerror = () => reject(transaction.error)
    transaction.onabort = () => reject(transaction.error || new Error('设备曲库写入中断。'))
  })
}
function publish() {
  const uid = account.value?.id, device = currentDeviceId()
  deviceTracks.value = [...entries.values()].filter(entry => entry.uid === uid && entry.device_id === device)
    .map(entry => {
      if (entry.cover && coverUrls.get(entry.key)?.blob !== entry.cover) {
        const previous = coverUrls.get(entry.key)
        if (previous) URL.revokeObjectURL(previous.url)
        coverUrls.set(entry.key, { blob: entry.cover, url: URL.createObjectURL(entry.cover) })
      }
      return { ...entry.track, device_id: device, device_file: true, saved_to_device: true,
        device_thumbnail: coverUrls.get(entry.key)?.url, device_assets_notice: entry.asset_notice }
    })
  unavailableDeviceTracks.value = [...unavailable.values()].filter(({ entry }) => entry.uid === uid && entry.device_id === device)
    .map(({ entry, reason, canAuthorize }) => ({ track: entry.track, reason, canAuthorize }))
  deviceLibraryRevision.value++
}
function reset() {
  generation++
  refreshSequence++
  releaseDevicePlaybackUrls()
  coverUrls.forEach(item => URL.revokeObjectURL(item.url)); coverUrls.clear()
  assetRequests.forEach(request => request.abort()); assetRequests.clear()
  entries.clear(); unavailable.clear(); persistenceErrors.clear(); files.clear(); deviceTracks.value = []; unavailableDeviceTracks.value = []; deviceLibraryError.value = ''
  deviceLibraryRevision.value++
}
export function releaseDevicePlaybackUrls() {
  objectUrls.forEach(url => URL.revokeObjectURL(url))
  objectUrls.clear()
}
async function forgetEntry(key: string) {
  const url = objectUrls.get(key)
  if (url) URL.revokeObjectURL(url)
  const cover = coverUrls.get(key)
  if (cover) URL.revokeObjectURL(cover.url)
  coverUrls.delete(key); assetRequests.get(key)?.abort(); assetRequests.delete(key)
  entries.delete(key); unavailable.delete(key); persistenceErrors.delete(key); files.delete(key); objectUrls.delete(key)
  await store('readwrite', storage => storage.delete(key)).catch(() => {})
}
async function readable(entry: DeviceEntry, ask = false) {
  if (entry.uid !== account.value?.id || entry.device_id !== currentDeviceId()) return null
  if (!entry.handle) return entry.file || files.get(entry.key) || null
  const permission = await entry.handle.queryPermission?.({ mode: 'read' })
  if (permission && permission !== 'granted') {
    if (!ask || await entry.handle.requestPermission?.({ mode: 'read' }) !== 'granted') return null
  }
  const file = await entry.handle.getFile()
  return file.size > 0 && file.size === entry.bytes && (!entry.modified || file.lastModified === entry.modified) ? file : null
}
function retainUnavailable(entry: DeviceEntry, failure?: unknown) {
  entries.delete(entry.key)
  const permission = failure instanceof DOMException && (failure.name === 'NotAllowedError' || failure.name === 'SecurityError')
  unavailable.set(entry.key, { entry, canAuthorize: Boolean(entry.handle?.requestPermission), reason:
    permission ? '文件需要重新授权。' : '文件暂不可读取，可能需要授权、重新定位或重新导入。' })
}
export async function refreshDeviceLibrary() {
  const revision = generation, sequence = ++refreshSequence, uid = account.value?.id
  const current = () => uid === account.value?.id && revision === generation && sequence === refreshSequence
  if (!uid) return
  try {
    const context = await deviceContext()
    if (!current()) return
    let readError = ''
    const saved: DeviceEntry[] = await store('readonly', storage => storage.getAll()).catch(() => { readError = '浏览器曲库读取失败，请检查站点存储权限后重试。'; return [] })
    if (!current()) return
    for (const entry of new Map([...saved, ...[...unavailable.values()].map(item => item.entry), ...entries.values()].map(entry => [entry.key, entry])).values()) {
      if (entry.uid !== uid || entry.device_id !== context.device_id) continue
      try {
        const file = await readable(entry)
        if (!current()) return
        if (file) { entries.set(entry.key, entry); unavailable.delete(entry.key) }
        else retainUnavailable(entry)
      } catch (failure) { if (!current()) return; retainUnavailable(entry, failure) }
    }
    deviceLibraryError.value = readError || [...persistenceErrors.values()][0] || ''
    if (!current()) return
    publish()
  } catch (error) { if (current()) deviceLibraryError.value = error instanceof Error ? error.message : '设备曲库读取失败。' }
}
export async function authorizeDeviceTrack(track: MusicTrack) {
  const uid = account.value?.id, revision = generation
  const entry = uid ? unavailable.get(entryKey(uid, currentDeviceId(), track))?.entry : undefined
  if (!entry?.handle?.requestPermission) throw new Error('请重新选择原音频文件。')
  // Request directly in the click handler, before any asynchronous storage read.
  const permission = await entry.handle.requestPermission({ mode: 'read' })
  if (uid !== account.value?.id || revision !== generation) return
  if (permission !== 'granted') throw new Error('未获得文件读取权限，请重新授权或选择原文件。')
  if (entry.track.provider === 'device') {
    const file = await readable(entry)
    if (!file) throw new Error('原文件已更改或无法读取，请重新选择文件。')
    const copy = await importedCopy(file)
    if (uid !== account.value?.id || revision !== generation) return
    await registerDeviceFile(entry.track, copy, uid)
  }
  await refreshDeviceLibrary()
}
export function hasDeviceFile(track: MusicTrack) {
  void deviceLibraryRevision.value // Vue consumers update when file availability changes.
  const uid = account.value?.id, device = currentDeviceId()
  return Boolean(uid && device && entries.has(entryKey(uid, device, track)))
}
export async function registerDeviceFile(track: MusicTrack, source: File | FileHandle, expectedUid = account.value?.id) {
  refreshSequence++
  const revision = generation, context = await deviceContext()
  if (!expectedUid || expectedUid !== account.value?.id || revision !== generation) return
  const handle = 'getFile' in source ? source : undefined
  const file = handle ? await handle.getFile() : source as File
  if (!file.size || !audioExtension.test(file.name)) return
  if (expectedUid !== account.value?.id || revision !== generation) return
  const key = entryKey(expectedUid, context.device_id, track)
  const previous = entries.get(key) || unavailable.get(key)?.entry
  const entry: DeviceEntry = { key, uid: expectedUid, device_id: context.device_id, track: { ...trackPayload(track), filename: file.name, device_id: context.device_id, device_file: true }, handle, ...(!handle ? { file } : {}), cover: previous?.cover, lyrics: previous?.lyrics, manual_lyrics: previous?.manual_lyrics, asset_notice: previous?.asset_notice, bytes: file.size, modified: file.lastModified, added_at: previous?.added_at || Date.now() }
  entries.set(key, entry)
  unavailable.delete(key)
  if (!handle) files.set(key, file)
  try { await store('readwrite', storage => storage.put(entry)); if (revision === generation) persistenceErrors.delete(key) }
  catch { if (revision === generation) persistenceErrors.set(key, '本次文件仅可在本页播放：浏览器存储不足或未允许保存，请释放空间后重新导入。') }
  if (expectedUid === account.value?.id && revision === generation) { refreshSequence++; deviceLibraryError.value = [...persistenceErrors.values()][0] || ''; publish() }
}

async function assetEntry(track: MusicTrack): Promise<DeviceEntry | undefined> {
  const uid = account.value?.id, device = currentDeviceId(), revision = generation
  if (!uid) return
  const key = entryKey(uid, device, track)
  const memory = entries.get(key) || unavailable.get(key)?.entry
  if (memory) return memory
  const saved: DeviceEntry | undefined = await store('readonly', storage => storage.get(key)).catch(() => undefined)
  if (uid === account.value?.id && device === currentDeviceId() && revision === generation) return saved
}
export async function readDeviceLyrics(track: MusicTrack) {
  const entry = await assetEntry(track)
  return entry?.lyrics && { ...entry.lyrics, manual: entry.manual_lyrics }
}
export async function saveDeviceLyrics(track: MusicTrack, lyrics: Lyrics, manual = false) {
  const revision = generation, entry = await assetEntry(track)
  if (!entry || revision !== generation || entry.uid !== account.value?.id || entry.device_id !== currentDeviceId()) return false
  const updated = entries.get(entry.key) || unavailable.get(entry.key)?.entry
  if (!updated || !lyrics.lines.length && !lyrics.plain) return false
  if (updated.manual_lyrics && !manual) return true
  updated.lyrics = lyrics; updated.manual_lyrics = manual || updated.manual_lyrics
  await store('readwrite', storage => storage.put(updated))
  return revision === generation && entry.uid === account.value?.id
}
export async function requestTrackLyrics(track: MusicTrack, signal?: AbortSignal): Promise<Lyrics> {
  const response = await fetch('/api/music/lyrics/tracks/lookup', { method: 'POST', signal,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ track: trackPayload(track) }) })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(typeof detail.detail === 'string' ? detail.detail : `歌词加载失败（${response.status}）。`)
  }
  return response.json()
}
export async function cacheDeviceAssets(track: MusicTrack) {
  const revision = generation, uid = account.value?.id, device = currentDeviceId()
  if (!uid) return
  const key = entryKey(uid, device, track)
  if (assetRequests.has(key)) return
  const entry = await assetEntry(track)
  if (!entry || !entries.has(key) || revision !== generation || uid !== account.value?.id || assetRequests.has(key)) return
  const controller = new AbortController()
  assetRequests.set(key, controller)
  const timer = setTimeout(() => controller.abort(), 20000)
  const current = () => revision === generation && uid === account.value?.id && device === currentDeviceId() && assetRequests.get(key) === controller
  entry.asset_notice = '正在保存封面和歌词…'; publish()
  const results = await Promise.allSettled([
    (async () => {
      if (entry.cover) return
      const thumbnail = track.custom_thumbnail || track.thumbnail || entry.track.thumbnail
      if (!thumbnail) throw new Error('暂无可用封面')
      const url = thumbnail.startsWith('https://') || thumbnail.startsWith('http://') ? `/api/image-proxy?url=${encodeURIComponent(thumbnail)}` : thumbnail
      const response = await fetch(url, { signal: controller.signal })
      if (!response.ok || !/^image\/(jpeg|png|webp|gif)(?:;|$)/i.test(response.headers.get('Content-Type') || '')) throw new Error('封面保存失败')
      const reader = response.body?.getReader(), chunks: Uint8Array<ArrayBuffer>[] = []
      let bytes = 0
      if (!reader) throw new Error('封面内容为空')
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          bytes += value.byteLength
          if (bytes > 10 * 1024 * 1024) throw new Error('封面超过 10 MB')
          chunks.push(new Uint8Array(value))
        }
      } finally { void reader.cancel().catch(() => {}); reader.releaseLock() }
      if (!bytes) throw new Error('封面内容为空')
      return new Blob(chunks, { type: response.headers.get('Content-Type')! })
    })(),
    (async () => {
      if (entry.lyrics?.lines.length || entry.lyrics?.plain) return
      const lyrics = await requestTrackLyrics(entry.track, controller.signal)
      if (!lyrics.lines.length && !lyrics.plain) throw new Error(lyrics.message || '未找到匹配歌词')
      return lyrics
    })(),
  ])
  clearTimeout(timer)
  try {
    const updated = entries.get(key) || unavailable.get(key)?.entry
    if (!current() || !updated) return
    const [cover, lyrics] = results
    if (cover.status === 'fulfilled' && cover.value && !updated.cover) updated.cover = cover.value
    if (lyrics.status === 'fulfilled' && lyrics.value && !updated.manual_lyrics) updated.lyrics = lyrics.value
    updated.asset_notice = results.flatMap(result => result.status === 'rejected' ? [result.reason instanceof Error && result.reason.name !== 'AbortError' ? result.reason.message : '配套资源保存超时'] : []).join('；')
    await store('readwrite', storage => storage.put(updated))
  } catch {
    const updated = entries.get(key) || unavailable.get(key)?.entry
    if (current() && updated) updated.asset_notice = '封面或歌词未能持久保存，请释放浏览器空间后重试。'
  }
  finally {
    if (assetRequests.get(key) === controller) assetRequests.delete(key)
    if (uid === account.value?.id && revision === generation) publish()
  }
}
export async function chooseDeviceFiles(fallback: () => void) {
  const picker = (window as any).showOpenFilePicker
  if (!picker) { fallback(); return false }
  const uid = account.value?.id
  const handles: FileHandle[] = await picker({ multiple: true, types: [{ description: '音频文件', accept: { 'audio/*': ['.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac', '.opus'] } }] })
  await importSelection(handles, uid)
  return true
}
async function importOne(file: File, source: File | FileHandle, uid = account.value?.id) {
  if (!audioExtension.test(file.name) || !file.size) return
  const revision = generation, context = await deviceContext()
  if (!uid || uid !== account.value?.id || revision !== generation) return
  const saved: DeviceEntry[] = await store('readonly', storage => storage.getAll()).catch(() => [])
  const candidates = new Map([...saved, ...[...unavailable.values()].map(item => item.entry), ...entries.values()].map(entry => [entry.key, entry]))
  const matches: DeviceEntry[] = []
  for (const entry of candidates.values()) {
    if (entry.uid !== uid || entry.device_id !== context.device_id || entry.track.filename !== file.name || entry.bytes !== file.size) continue
    try {
      if ('getFile' in source && source.isSameEntry && entry.handle) {
        if (await source.isSameEntry(entry.handle)) matches.push(entry)
      } else {
        const previous = await readable(entry)
        if (previous && await sameFileBytes(previous, file)) matches.push(entry)
      }
    } catch { /* An unavailable old file cannot prove a duplicate. */ }
  }
  if (uid !== account.value?.id || revision !== generation) return
  let duration: number
  try { duration = await inspectDeviceAudio(file) }
  catch (error) {
    // Remove only proven references to the rejected file; its bytes stay intact.
    if (uid === account.value?.id && revision === generation) {
      for (const entry of matches) await forgetEntry(entry.key)
      publish()
    }
    throw error
  }
  if (uid !== account.value?.id || revision !== generation) return
  const existing = matches.find(entry => objectUrls.has(entry.key)) || matches[0]
  const identifier = crypto.randomUUID()
  const name = file.name.replace(audioExtension, '')
  const split = name.indexOf(' - ')
  const track: MusicTrack = existing?.track || { title: split > 0 ? name.slice(split + 3) : name, artist: split > 0 ? name.slice(0, split) : '', provider: 'device', identifier, webpage_url: `device:${identifier}` }
  // A picker handle can lose permission on reload. Import an independent local
  // copy while access is granted, so playback does not depend on that permission.
  const copy = await importedCopy(file)
  if (uid !== account.value?.id || revision !== generation) return
  await registerDeviceFile({ ...track, duration }, copy, uid)
  if (uid !== account.value?.id || revision !== generation) return
  for (const entry of matches) if (entry.key !== entryKey(uid, context.device_id, track)) await forgetEntry(entry.key)
  publish()
}
async function sameFileBytes(a: File, b: File) {
  if (a.size !== b.size) return false
  // Compare only duplicate candidates, in bounded chunks, without hashing a
  // whole music library or buffering a large file in memory.
  for (let offset = 0; offset < a.size; offset += 256 * 1024) {
    const [left, right] = await Promise.all([a.slice(offset, offset + 256 * 1024).arrayBuffer(), b.slice(offset, offset + 256 * 1024).arrayBuffer()])
    const x = new Uint8Array(left), y = new Uint8Array(right)
    if (x.some((value, index) => value !== y[index])) return false
  }
  return true
}
export async function importDeviceFiles(selected: FileList | File[]) {
  const uid = account.value?.id
  await importSelection(Array.from(selected), uid)
}
async function importSelection(selected: Array<File | FileHandle>, uid: number | undefined) {
  const failures: string[] = []
  for (const source of selected) {
    try { await importOne('getFile' in source ? await source.getFile() : source, source, uid) }
    catch (error) { failures.push(error instanceof Error ? error.message : '音频导入失败。') }
  }
  if (failures.length) throw new Error(failures.slice(0, 3).join('\n') + (failures.length > 3 ? `\n另有 ${failures.length - 3} 个文件未导入。` : ''))
}
export async function devicePlaybackUrl(track: MusicTrack) {
  const uid = account.value?.id
  if (!uid) return ''
  const entry = entries.get(entryKey(uid, currentDeviceId(), track))
  if (!entry) {
    if (track.device_file || track.provider === 'device') throw new Error('本设备没有可读取的文件，请重新定位或导入。')
    return ''
  }
  try {
    const file = await readable(entry, true)
    if (!file) throw new Error('文件不存在或需要重新授权，请重新导入。')
    if (entry.uid !== account.value?.id || entry.device_id !== currentDeviceId()) throw new Error('账号或设备已变化。')
    const previous = objectUrls.get(entry.key)
    if (previous) URL.revokeObjectURL(previous)
    const url = URL.createObjectURL(file); objectUrls.set(entry.key, url)
    return url
  } catch (error) { if (uid === account.value?.id) { retainUnavailable(entry, error); publish() }; throw error }
}
export async function removeDeviceTrack(track: MusicTrack) {
  const uid = account.value?.id
  if (!uid) return
  const key = entryKey(uid, currentDeviceId(), track)
  refreshSequence++
  await forgetEntry(key)
  await refreshDeviceLibrary(); publish()
}
watch(() => account.value?.id, () => { reset(); void refreshDeviceLibrary() }, { immediate: true })
if (typeof window !== 'undefined') {
  window.addEventListener('focus', () => { void refreshDeviceLibrary() })
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') void refreshDeviceLibrary() })
}
