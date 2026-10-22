import { ref, watch } from 'vue'
import { account } from '../accounts/auth'
import { currentDeviceId, deviceContext } from '../../shared/device'
import { trackKey, trackPayload, type MusicTrack } from './musicLibrary'
import { inspectDeviceAudio } from './deviceAudio'

type FileHandle = { getFile(): Promise<File>; isSameEntry?(other: FileHandle): Promise<boolean>; queryPermission?(options: { mode: 'read' }): Promise<PermissionState>; requestPermission?(options: { mode: 'read' }): Promise<PermissionState> }
type DeviceEntry = { key: string; uid: number; device_id: string; track: MusicTrack; handle?: FileHandle; bytes: number; modified?: number; added_at: number }
export const deviceTracks = ref<MusicTrack[]>([])
export const deviceLibraryRevision = ref(0)
export const deviceLibraryError = ref('')
const entries = new Map<string, DeviceEntry>()
const files = new Map<string, File>()
const objectUrls = new Map<string, string>()
let generation = 0
let database: Promise<IDBDatabase> | undefined
const audioExtension = /\.(mp3|m4a|flac|wav|ogg|aac|opus)$/i
const entryKey = (uid: number, device: string, track: MusicTrack) => `${uid}:${device}:${trackKey(track)}`

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
    .map(entry => ({ ...entry.track, device_id: device, device_file: true, saved_to_device: true }))
  deviceLibraryRevision.value++
}
function reset() {
  generation++
  releaseDevicePlaybackUrls()
  entries.clear(); files.clear(); deviceTracks.value = []; deviceLibraryError.value = ''
  deviceLibraryRevision.value++
}
export function releaseDevicePlaybackUrls() {
  objectUrls.forEach(url => URL.revokeObjectURL(url))
  objectUrls.clear()
}
async function forgetEntry(key: string) {
  const url = objectUrls.get(key)
  if (url) URL.revokeObjectURL(url)
  entries.delete(key); files.delete(key); objectUrls.delete(key)
  await store('readwrite', storage => storage.delete(key)).catch(() => {})
}
async function readable(entry: DeviceEntry, ask = false) {
  if (entry.uid !== account.value?.id || entry.device_id !== currentDeviceId()) return null
  if (files.has(entry.key)) return files.get(entry.key)!
  if (!entry.handle) return null
  const permission = await entry.handle.queryPermission?.({ mode: 'read' })
  if (permission && permission !== 'granted') {
    if (!ask || await entry.handle.requestPermission?.({ mode: 'read' }) !== 'granted') return null
  }
  const file = await entry.handle.getFile()
  return file.size > 0 && file.size === entry.bytes && (!entry.modified || file.lastModified === entry.modified) ? file : null
}
export async function refreshDeviceLibrary() {
  const revision = generation, uid = account.value?.id
  if (!uid) return
  try {
    const context = await deviceContext()
    if (uid !== account.value?.id || revision !== generation) return
    const saved: DeviceEntry[] = await store('readonly', storage => storage.getAll()).catch(() => [])
    if (uid !== account.value?.id || revision !== generation) return
    for (const entry of new Map([...entries.values(), ...saved].map(entry => [entry.key, entry])).values()) {
      if (entry.uid !== uid || entry.device_id !== context.device_id) continue
      try { const file = await readable(entry); if (revision !== generation || uid !== account.value?.id) return; if (file) entries.set(entry.key, entry); else entries.delete(entry.key) }
      catch { if (revision !== generation || uid !== account.value?.id) return; entries.delete(entry.key) }
    }
    if (uid !== account.value?.id || revision !== generation) return
    publish(); deviceLibraryError.value = ''
  } catch (error) { if (revision === generation) deviceLibraryError.value = error instanceof Error ? error.message : '设备曲库读取失败。' }
}
export function hasDeviceFile(track: MusicTrack) {
  void deviceLibraryRevision.value // Vue consumers update when file availability changes.
  const uid = account.value?.id, device = currentDeviceId()
  return Boolean(uid && device && entries.has(entryKey(uid, device, track)))
}
export async function registerDeviceFile(track: MusicTrack, source: File | FileHandle, expectedUid = account.value?.id) {
  const revision = generation, context = await deviceContext()
  if (!expectedUid || expectedUid !== account.value?.id || revision !== generation) return
  const handle = 'getFile' in source ? source : undefined
  const file = handle ? await handle.getFile() : source as File
  if (!file.size || !audioExtension.test(file.name)) return
  if (expectedUid !== account.value?.id || revision !== generation) return
  const key = entryKey(expectedUid, context.device_id, track)
  const entry: DeviceEntry = { key, uid: expectedUid, device_id: context.device_id, track: { ...trackPayload(track), filename: file.name, device_id: context.device_id, device_file: true }, handle, bytes: file.size, modified: file.lastModified, added_at: Date.now() }
  entries.set(key, entry)
  if (!handle) files.set(key, file)
  if (handle) await store('readwrite', storage => storage.put(entry)).catch(() => { deviceLibraryError.value = '文件可在本页播放；浏览器未能记住文件授权。' })
  if (expectedUid === account.value?.id && revision === generation) publish()
}
export async function chooseDeviceFiles(fallback: () => void) {
  const picker = (window as any).showOpenFilePicker
  if (!picker) { fallback(); return }
  const uid = account.value?.id
  const handles: FileHandle[] = await picker({ multiple: true, types: [{ description: '音频文件', accept: { 'audio/*': ['.mp3', '.m4a', '.flac', '.wav', '.ogg', '.aac', '.opus'] } }] })
  await importSelection(handles, uid)
}
async function importOne(file: File, source: File | FileHandle, uid = account.value?.id) {
  if (!audioExtension.test(file.name) || !file.size) return
  const revision = generation, context = await deviceContext()
  if (!uid || uid !== account.value?.id || revision !== generation) return
  const saved: DeviceEntry[] = await store('readonly', storage => storage.getAll()).catch(() => [])
  const candidates = new Map([...saved, ...entries.values()].map(entry => [entry.key, entry]))
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
  await registerDeviceFile({ ...track, duration }, 'getFile' in source ? source : existing?.handle || source, uid)
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
  } catch (error) { entries.delete(entry.key); publish(); throw error }
}
export async function removeDeviceTrack(track: MusicTrack) {
  const uid = account.value?.id
  if (!uid) return
  await forgetEntry(entryKey(uid, currentDeviceId(), track))
  await refreshDeviceLibrary(); publish()
}
watch(() => account.value?.id, () => { reset(); void refreshDeviceLibrary() }, { immediate: true })
if (typeof window !== 'undefined') {
  window.addEventListener('focus', () => { void refreshDeviceLibrary() })
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') void refreshDeviceLibrary() })
}
