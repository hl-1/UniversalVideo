import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
function setup(t: any) {
  const account: any = { value: { id: 1 } }
  const inspection = { run: async (_file: File) => 7 }
  let changed = () => {}, device = 'a'
  const stubs: Record<string, any> = {
    vue: { ref: (value: any) => ({ value }), watch: (_: any, callback: any) => { changed = callback } },
    '../accounts/auth': { account },
    '../../shared/device': { currentDeviceId: () => device, deviceContext: async () => ({ device_id: device, client_kind: 'web' }) },
    './musicLibrary': { trackKey: (track: any) => `${track.provider}:${track.identifier}`, trackPayload: (track: any) => ({ ...track }) },
    './deviceAudio': { inspectDeviceAudio: (file: File) => inspection.run(file) },
  }
  const source = fs.readFileSync(new URL('../src/features/music/deviceLibrary.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const library: any = {}
  new Function('require', 'exports', compiled)((id: string) => { assert.ok(id in stubs, id); return stubs[id] }, library)
  return { library, account, inspection, changeAccount(id: number) { account.value = { id }; changed() }, changeDevice() { device = 'b'; changed() } }
}
const track = { title: 'local song', provider: 'qq', identifier: 'one', webpage_url: 'https://y.qq.com/one' }
test('actual imported file plays as a blob and survives refresh without any server request', async t => {
  const { library } = setup(t)
  await library.registerDeviceFile(track, new File(['audio'], 'song.mp3'))
  await library.refreshDeviceLibrary()
  assert.equal(library.deviceTracks.value.length, 1)
  assert.equal(library.hasDeviceFile(track), true)
  assert.match(await library.devicePlaybackUrl(track), /^blob:/)
  await library.removeDeviceTrack(track)
  assert.equal(library.hasDeviceFile(track), false)
})
test('deleted or modified handle is removed and local playback refuses network fallback', async t => {
  const { library } = setup(t)
  const original = new File(['audio'], 'song.mp3', { lastModified: 1 })
  let file = original
  const handle = { getFile: async () => file }
  await library.registerDeviceFile(track, handle)
  file = new File(['different'], 'song.mp3', { lastModified: 2 })
  await assert.rejects(library.devicePlaybackUrl({ ...track, device_file: true }), /重新授权|重新导入/)
  assert.equal(library.hasDeviceFile(track), false)
  assert.equal(library.deviceTracks.value.length, 0)
})
test('account changes while file retrieval is pending discard late registration', async t => {
  const { library, changeAccount } = setup(t)
  let release: any
  const promise = library.registerDeviceFile(track, { getFile: () => new Promise(resolve => { release = resolve }) }, 1)
  await new Promise(resolve => setImmediate(resolve))
  changeAccount(2)
  release(new File(['audio'], 'song.mp3'))
  await promise
  assert.equal(library.deviceTracks.value.length, 0)
  assert.equal(library.hasDeviceFile(track), false)
})
test('switching device instances clears the account local library', async t => {
  const { library, changeDevice } = setup(t)
  await library.registerDeviceFile(track, new File(['audio'], 'song.mp3'))
  changeDevice()
  assert.equal(library.hasDeviceFile(track), false)
  assert.equal(library.deviceTracks.value.length, 0)
})
test('reimporting the same file keeps its identity and decoded duration', async t => {
  const { library } = setup(t)
  const file = new File(['valid-audio'], 'song.m4a', { lastModified: 1 })
  await library.importDeviceFiles([file])
  const identity = library.deviceTracks.value[0].identifier
  await library.importDeviceFiles([new File(['valid-audio'], 'song.m4a', { lastModified: 1 })])
  assert.equal(library.deviceTracks.value.length, 1)
  assert.equal(library.deviceTracks.value[0].identifier, identity)
  assert.equal(library.deviceTracks.value[0].duration, 7)
})
test('same name, size and timestamp cannot merge different audio bytes', async t => {
  const { library } = setup(t)
  await library.importDeviceFiles([new File(['first'], 'song.m4a', { lastModified: 1 })])
  await library.importDeviceFiles([new File(['other'], 'song.m4a', { lastModified: 1 })])
  assert.equal(library.deviceTracks.value.length, 2)
})
test('reimport consolidates proven duplicate legacy handles without changing the file', async t => {
  const { library } = setup(t)
  const file = new File(['audio'], 'song.m4a')
  const handle: any = { getFile: async () => file, isSameEntry: async (other: any) => other === handle }
  for (const identifier of ['old-one', 'old-two']) await library.registerDeviceFile({ ...track, provider: 'device', identifier }, handle)
  // The File input path also proves exact byte equality with both stored handles.
  await library.importDeviceFiles([file])
  assert.equal(library.deviceTracks.value.length, 1)
  assert.equal(library.deviceTracks.value[0].identifier, 'old-one')
  assert.equal(await file.text(), 'audio')
})
test('decode rejection cannot create a playable entry or keep old references to those bytes', async t => {
  const { library, inspection } = setup(t)
  const file = new File(['broken'], 'song.m4a')
  await library.registerDeviceFile({ ...track, provider: 'device' }, file)
  inspection.run = async () => { throw new Error('本地音频解码失败') }
  await assert.rejects(library.importDeviceFiles([file]), /解码失败/)
  assert.equal(library.deviceTracks.value.length, 0)
  assert.equal(await file.text(), 'broken')
})
test('account change during media inspection discards late import', async t => {
  const { library, inspection, changeAccount } = setup(t)
  let release!: (value: number) => void
  inspection.run = () => new Promise(resolve => { release = resolve })
  const pending = library.importDeviceFiles([new File(['audio'], 'song.m4a')])
  await new Promise(resolve => setImmediate(resolve))
  changeAccount(2); release(7); await pending
  assert.equal(library.deviceTracks.value.length, 0)
})
test('a rejected file does not prevent later valid files from importing', async t => {
  const { library, inspection } = setup(t)
  inspection.run = async file => { if (file.name === 'broken.m4a') throw new Error('解码失败'); return 7 }
  await assert.rejects(library.importDeviceFiles([new File(['bad'], 'broken.m4a'), new File(['valid'], 'good.m4a')]), /解码失败/)
  assert.equal(library.deviceTracks.value.length, 1)
  assert.equal(library.deviceTracks.value[0].filename, 'good.m4a')
})
