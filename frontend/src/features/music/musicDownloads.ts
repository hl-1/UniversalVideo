import { ref, watch } from 'vue'
import { account } from '../accounts/auth'
import { api, jsonRequest } from '../../shared/api/client'
import { trackKey, trackPayload, type MusicTrack } from './musicLibrary'
import { currentDeviceId } from '../../shared/device'
import { hasDeviceFile, refreshDeviceLibrary } from './deviceLibrary'

const saved = ref(new Set<string>())
const unverified = ref(new Set<string>())
export const savedMusicHistory = ref<MusicTrack[]>([])
export const unverifiedMusicHistory = ref<MusicTrack[]>([])
export const unverifiedMusicCount = ref(0)
type FileConfirmation = { source: 'filesystem'; artifact: 'individual_file'; status: 'completed'; bytes: number; saved_at: number; download_id?: string }
type SavedTrack = MusicTrack & { file_confirmation: FileConfirmation }
let confirmations: SavedTrack[] = []
const storageKey = (uid: string) => `videodream.music.saved.${uid}.${currentDeviceId()}`
function validTrack(track: any): track is MusicTrack { return track && typeof track.title === 'string' && typeof track.webpage_url === 'string' }
function validConfirmation(track: any): track is SavedTrack {
  const receipt = track?.file_confirmation
  return validTrack(track) && receipt?.source === 'filesystem' && receipt.artifact === 'individual_file' && receipt.status === 'completed'
    && Number.isSafeInteger(receipt.bytes) && receipt.bytes > 0 && Number.isFinite(receipt.saved_at) && receipt.saved_at > 0
}
function persist(uid: string) {
  try { localStorage.setItem(storageKey(uid), JSON.stringify({ version: 2, tracks: confirmations,
    unverified_keys: [...unverified.value], unverified_tracks: unverifiedMusicHistory.value })) } catch { /* A full browser store cannot change save confirmation. */ }
}
watch(() => account.value?.id, uid => {
  confirmations = []; saved.value = new Set(); savedMusicHistory.value = []
  unverified.value = new Set(); unverifiedMusicHistory.value = []; unverifiedMusicCount.value = 0
  if (!uid) return
  try {
    let raw = localStorage.getItem(storageKey(uid))
    const legacyId = account.value?.legacy_id
    if (!raw && legacyId) {
      raw = localStorage.getItem(storageKey(legacyId))
      if (raw) localStorage.setItem(storageKey(uid), raw)
    }
    if (!raw) {
      const legacy = localStorage.getItem(`videodream.music.saved.${legacyId || uid}`)
      if (legacy) {
        const old = JSON.parse(legacy)
        // Unknown device provenance cannot become a current-device receipt.
        raw = JSON.stringify({ version: 1, keys: Array.isArray(old) ? old : old.keys || [], tracks: old.tracks || [], unverified_keys: old.unverified_keys || [], unverified_tracks: old.unverified_tracks || [] })
      }
    }
    if (!raw) return
    const stored = JSON.parse(raw)
    const tracks: MusicTrack[] = Array.isArray(stored?.tracks) ? stored.tracks.filter(validTrack) : []
    confirmations = stored?.version === 2 ? tracks.filter(validConfirmation) : []
    savedMusicHistory.value = confirmations.map(track => ({ ...track, list_time: track.file_confirmation.saved_at / 1000, saved_to_device: true, save_unverified: false }))
    saved.value = new Set(confirmations.map(trackKey))
    const legacyKeys = Array.isArray(stored) ? stored : [...(Array.isArray(stored?.keys) ? stored.keys : []), ...(Array.isArray(stored?.unverified_keys) ? stored.unverified_keys : [])]
    const legacyTracks = [...tracks.filter(track => !saved.value.has(trackKey(track))), ...(Array.isArray(stored?.unverified_tracks) ? stored.unverified_tracks.filter(validTrack) : [])]
    unverified.value = new Set([...legacyKeys.filter((key: unknown) => typeof key === 'string'), ...legacyTracks.map(trackKey)].filter(key => !saved.value.has(key)))
    unverifiedMusicHistory.value = [...new Map(legacyTracks.filter(track => unverified.value.has(trackKey(track)))
      .map(track => [trackKey(track), { ...trackPayload(track), saved_to_device: false, save_unverified: true }])).values()]
    unverifiedMusicCount.value = unverified.value.size
    if (stored?.version !== 2) {
      // Retain the exact old record before changing its interpretation. A ZIP
      // or browser handoff in v1 never proved that each song reached the disk.
      try {
        const backup = `videodream.music.saved.backup.${uid}.v1`
        if (localStorage.getItem(backup) === null) localStorage.setItem(backup, raw)
        persist(uid)
      } catch { /* Keep the in-memory correction even when storage is full. */ }
    }
  } catch { /* Invalid legacy data provides no evidence of a saved file. */ }
}, { immediate: true })

export function hasLibraryFile(track: MusicTrack) { return Boolean(track.local_url && track.file_available === true) }
export function isMusicDownloaded(track: MusicTrack) { return hasDeviceFile(track) }
export function musicDownloadLabel(track: MusicTrack) { return hasDeviceFile(track) ? '本设备可播放' : saved.value.has(trackKey(track)) ? '本设备保存记录' : unverified.value.has(trackKey(track)) ? '旧保存记录待核实' : '' }
export function rememberMusicDownloads(tracks: MusicTrack[], uid: string | undefined, receipt: { bytes: number; download_id?: string }) {
  if (!uid || uid !== account.value?.id || tracks.length !== 1 || !Number.isSafeInteger(receipt.bytes) || receipt.bytes <= 0) return
  const track = tracks[0]!, key = trackKey(track)
  const entry: SavedTrack = { ...trackPayload(track), saved_to_device: true, file_confirmation: {
    source: 'filesystem', artifact: 'individual_file', status: 'completed', bytes: receipt.bytes, saved_at: Date.now(), download_id: receipt.download_id } }
  confirmations = [...new Map([...confirmations, entry].map(track => [trackKey(track), track])).values()]
  saved.value = new Set(confirmations.map(trackKey))
  savedMusicHistory.value = confirmations.map(track => ({ ...track, list_time: track.file_confirmation.saved_at / 1000 }))
  unverified.value.delete(key)
  unverifiedMusicHistory.value = unverifiedMusicHistory.value.filter(track => trackKey(track) !== key)
  unverifiedMusicCount.value = unverified.value.size
  persist(uid)
  void refreshDeviceLibrary()
}
export async function refreshMusicDownloadStatus(tracks: MusicTrack[], signal?: AbortSignal) {
  const unique = [...new Map(tracks.map(track => [trackKey(track), track])).values()]
  const statuses = new Map<string, Partial<MusicTrack>>()
  for (let offset = 0; offset < unique.length; offset += 200) {
    const data = await api<{ tracks: MusicTrack[] }>('/api/library/download-status', { ...jsonRequest('POST', { tracks: unique.slice(offset, offset + 200).map(trackPayload) }), signal })
    for (const track of data.tracks) statuses.set(trackKey(track), track)
  }
  return statuses
}
