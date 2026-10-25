import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
const song = { title: '歌曲', provider: 'qq', identifier: 'one', webpage_url: 'https://y.qq.com/song/one' }
function setup(t: any) {
  const previous = globalThis.localStorage
  const data = new Map<string, string>()
  globalThis.localStorage = { getItem: (key: string) => data.get(key) ?? null, setItem: (key: string, value: string) => data.set(key, value) } as any
  const account: any = { value: { id: 1 } }
  let device = 'device-A', change = (_?: any) => {}
  const readable = new Set<string>()
  const key = (track: any) => `${track.provider}:${track.identifier}`
  const stubs: Record<string, any> = {
    vue: { ref: (value: any) => ({ value }), watch: (_: any, callback: any) => { change = callback; callback(1) } },
    '../accounts/auth': { account },
    '../../shared/device': { currentDeviceId: () => device },
    './deviceLibrary': { hasDeviceFile: (track: any) => readable.has(`${account.value?.id}:${device}:${key(track)}`), refreshDeviceLibrary: async () => {} },
    '../../shared/api/client': { api: async () => ({ tracks: [] }), jsonRequest: (_: any, data: any) => data },
    './musicLibrary': { trackKey: key, trackPayload: (track: any) => ({ ...track }) },
  }
  const source = fs.readFileSync(new URL('../src/features/music/musicDownloads.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const downloads: any = {}
  new Function('require', 'exports', compiled)((id: string) => { assert.ok(id in stubs, id); return stubs[id] }, downloads)
  t.after(() => { globalThis.localStorage = previous })
  return { downloads, data, readable, reload: () => change(account.value?.id), switchDevice(id: string) { device = id; change(account.value?.id) }, switchAccount(id: number | string, legacy_id?: string) { account.value = { id, legacy_id }; change(id) } }
}

test('snowflake migration keeps prior download receipts without rounding the new ID', t => {
  const { downloads, data, switchAccount } = setup(t)
  downloads.rememberMusicDownloads([song], 1, { bytes: 100 })
  const uid = '903456789012345679'
  switchAccount(uid, '1')
  assert.equal(downloads.savedMusicHistory.value.length, 1)
  assert.equal(data.get(`videodream.music.saved.${uid}.device-A`), data.get('videodream.music.saved.1.device-A'))
  assert.equal(downloads.isMusicDownloaded(song), false)
})
test('server files and filesystem receipts never replace actual device readability', t => {
  const { downloads, readable, reload } = setup(t)
  downloads.rememberMusicDownloads([song], 1, { bytes: 100, download_id: 'saved' })
  assert.equal(downloads.isMusicDownloaded(song), false)
  assert.equal(downloads.musicDownloadLabel(song), '本设备保存记录')
  reload()
  assert.equal(downloads.savedMusicHistory.value.length, 1)
  assert.equal(downloads.isMusicDownloaded({ ...song, local_url: '/api/files/one.mp3', file_available: true }), false)
  readable.add('1:device-A:qq:one')
  assert.equal(downloads.isMusicDownloaded(song), true)
  readable.clear()
  assert.equal(downloads.isMusicDownloaded(song), false)
})
test('same account has separate save history and file states on two devices', t => {
  const { downloads, readable, switchDevice, data } = setup(t)
  downloads.rememberMusicDownloads([song], 1, { bytes: 100 })
  readable.add('1:device-A:qq:one')
  switchDevice('device-B')
  assert.equal(downloads.savedMusicHistory.value.length, 0)
  assert.equal(downloads.isMusicDownloaded(song), false)
  downloads.rememberMusicDownloads([song], 1, { bytes: 200 })
  assert.equal(JSON.parse(data.get('videodream.music.saved.1.device-A')!).tracks[0].file_confirmation.bytes, 100)
  switchDevice('device-A')
  assert.equal(downloads.isMusicDownloaded(song), true)
})
test('unknown-device history is retained as unverified and never confirmed on migration', t => {
  const { downloads, data, reload } = setup(t)
  const old = JSON.stringify({ version: 2, tracks: [{ ...song, file_confirmation: { source: 'filesystem', artifact: 'individual_file', status: 'completed', bytes: 100, saved_at: 1 } }] })
  data.set('videodream.music.saved.1', old)
  reload()
  assert.equal(downloads.savedMusicHistory.value.length, 0)
  assert.equal(downloads.unverifiedMusicCount.value, 1)
  assert.equal(downloads.isMusicDownloaded(song), false)
  assert.equal(data.get('videodream.music.saved.1'), old)
})
test('late account callbacks and zero byte saves cannot create device records', t => {
  const { downloads, switchAccount, data } = setup(t)
  switchAccount(2)
  downloads.rememberMusicDownloads([song], 1, { bytes: 123 })
  downloads.rememberMusicDownloads([song], 2, { bytes: 0 })
  assert.equal(data.size, 0)
  assert.equal(downloads.savedMusicHistory.value.length, 0)
})
