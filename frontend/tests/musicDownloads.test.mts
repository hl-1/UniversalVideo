import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const song = (id: number) => ({ title: `song ${id}`, artist: 'artist', provider: 'qq', identifier: String(id), webpage_url: `https://example.com/song/${id}` })
const receipt = { source: 'filesystem', artifact: 'individual_file', status: 'completed', bytes: 100, saved_at: 123 }

function setup(t: any, stored: any) {
  const previous = globalThis.localStorage
  const data = new Map<string, string>(stored === undefined ? [] : [['videodream.music.saved.1', JSON.stringify(stored)]])
  globalThis.localStorage = { getItem: key => data.get(key) ?? null, setItem: (key, value) => { data.set(key, value) } } as Storage
  const account: any = { value: { id: 1 } }
  let change = (_uid: number | undefined) => {}
  const stubs: Record<string, any> = {
    vue: { ref: (value: any) => ({ value }), watch: (_: any, callback: any) => { change = callback; callback(account.value?.id) } },
    '../accounts/auth': { account },
    '../../shared/api/client': { api: async () => ({ tracks: [] }), jsonRequest: (_: any, data: any) => data },
    './musicLibrary': { trackKey: (track: any) => `${track.provider}:${track.identifier}`, trackPayload: (track: any) => ({ ...track }) },
  }
  const source = fs.readFileSync(new URL('../src/features/music/musicDownloads.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const downloads: any = {}
  new Function('require', 'exports', compiled)((id: string) => { assert.ok(id in stubs, id); return stubs[id] }, downloads)
  t.after(() => { globalThis.localStorage = previous })
  return { downloads, data, switchAccount(id?: number) { account.value = id ? { id } : null; change(id) }, reload() { change(account.value?.id) } }
}

test('legacy keys regain download buttons and are backed up instead of accepted as saved files', t => {
  const { downloads, data, reload } = setup(t, ['qq:1', 'qq:2'])
  assert.equal(downloads.isMusicDownloaded(song(1)), false)
  assert.equal(downloads.musicDownloadLabel(song(1)), '旧保存记录待核实')
  assert.equal(downloads.unverifiedMusicCount.value, 2)
  assert.deepEqual(downloads.savedMusicHistory.value, [])
  assert.equal(data.get('videodream.music.saved.backup.1.v1'), JSON.stringify(['qq:1', 'qq:2']))
  assert.equal(JSON.parse(data.get('videodream.music.saved.1')!).version, 2)
  reload()
  assert.equal(downloads.isMusicDownloaded(song(1)), false)
  assert.equal(downloads.unverifiedMusicCount.value, 2)
  assert.equal(data.get('videodream.music.saved.backup.1.v1'), JSON.stringify(['qq:1', 'qq:2']))
})

test('old ZIP success flags and metadata become explicitly unverified history', t => {
  const { downloads } = setup(t, { version: 1, keys: ['qq:1'], tracks: [{ ...song(1), saved_to_device: true }] })
  assert.equal(downloads.isMusicDownloaded(song(1)), false)
  assert.equal(downloads.savedMusicHistory.value.length, 0)
  assert.equal(downloads.unverifiedMusicHistory.value[0].save_unverified, true)
  assert.equal(downloads.unverifiedMusicHistory.value[0].saved_to_device, false)
})

test('only completed individual files with nonzero bytes remain confirmed after reload', t => {
  const { downloads } = setup(t, { version: 2, tracks: [
    { ...song(1), file_confirmation: receipt },
    { ...song(2), file_confirmation: { ...receipt, artifact: 'zip' } },
    { ...song(3), file_confirmation: { ...receipt, status: 'handed_off' } },
    { ...song(4), file_confirmation: { ...receipt, bytes: 0 } },
    { ...song(5), saved_to_device: true },
  ] })
  assert.equal(downloads.isMusicDownloaded(song(1)), true)
  for (const id of [2, 3, 4, 5]) assert.equal(downloads.isMusicDownloaded(song(id)), false)
  assert.equal(downloads.savedMusicHistory.value.length, 1)
  assert.equal(downloads.unverifiedMusicCount.value, 4)
})

test('a newly confirmed single song replaces its unverified entry without confirming the rest', t => {
  const { downloads, reload, data } = setup(t, { version: 1, keys: ['qq:1', 'qq:2'], tracks: [song(1), song(2)] })
  downloads.rememberMusicDownloads([song(1)], 1, { bytes: 123, download_id: 'single-file' })
  assert.equal(downloads.isMusicDownloaded(song(1)), true)
  assert.equal(downloads.isMusicDownloaded(song(2)), false)
  assert.equal(downloads.unverifiedMusicCount.value, 1)
  reload()
  assert.equal(downloads.isMusicDownloaded(song(1)), true)
  assert.equal(downloads.unverifiedMusicHistory.value[0].identifier, '2')
  const confirmed = JSON.parse(data.get('videodream.music.saved.1')!).tracks[0].file_confirmation
  assert.equal(confirmed.download_id, 'single-file')
  assert.equal(confirmed.artifact, 'individual_file')
})

test('zero bytes and a batch-wide receipt cannot mark songs as downloaded', t => {
  const { downloads } = setup(t, undefined)
  downloads.rememberMusicDownloads([song(1)], 1, { bytes: 0 })
  downloads.rememberMusicDownloads([song(1), song(2)], 1, { bytes: 123 })
  assert.equal(downloads.isMusicDownloaded(song(1)), false)
  assert.equal(downloads.isMusicDownloaded(song(2)), false)
})

test('a local URL without explicit server availability cannot hide a download button', t => {
  const { downloads } = setup(t, undefined)
  assert.equal(downloads.isMusicDownloaded({ ...song(1), local_url: '/api/files/old.mp3' }), false)
  assert.equal(downloads.isMusicDownloaded({ ...song(1), local_url: '/api/files/old.mp3', file_available: false }), false)
  assert.equal(downloads.isMusicDownloaded({ ...song(1), local_url: '/api/files/valid.mp3', file_available: true }), true)
})

test('switching accounts clears confirmed and unverified state and rejects late saves', t => {
  const { downloads, switchAccount, data } = setup(t, ['qq:1'])
  switchAccount(2)
  downloads.rememberMusicDownloads([song(1)], 1, { bytes: 123 })
  assert.equal(downloads.unverifiedMusicCount.value, 0)
  assert.equal(downloads.isMusicDownloaded(song(1)), false)
  assert.equal(data.has('videodream.music.saved.2'), false)
  switchAccount(1)
  assert.equal(downloads.unverifiedMusicCount.value, 1)
  assert.equal(downloads.isMusicDownloaded(song(1)), false)
})
