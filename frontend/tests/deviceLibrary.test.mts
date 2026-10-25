import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
function storageFixture() {
  const rows = new Map<string, any>()
  const fixture = { rows, failWrites: false, failReads: false, api: { open() {
    const request: any = {}
    queueMicrotask(() => {
      request.result = { transaction(_store: string, mode: string) {
        const transaction: any = { objectStore() { return {
          getAll() { return { result: [...rows.values()].map(entry => ({ ...entry })) } },
          get(key: string) { const entry = rows.get(key); return { result: entry && { ...entry } } },
          put(entry: any) { if (!fixture.failWrites) rows.set(entry.key, { ...entry }); return { result: entry.key } },
          delete(key: string) { rows.delete(key); return { result: undefined } },
        } } }
        setImmediate(() => {
          if (mode === 'readwrite' && fixture.failWrites || mode === 'readonly' && fixture.failReads) {
            transaction.error = new DOMException('Storage unavailable', 'QuotaExceededError'); transaction.onerror?.()
          } else transaction.oncomplete?.()
        })
        return transaction
      } }
      request.onsuccess?.()
    })
    return request
  } } }
  return fixture
}
function setup(t: any, storage = storageFixture()) {
  const account: any = { value: { id: 1 } }
  const inspection = { run: async (_file: File) => 7 }
  let changed = () => {}, device = 'a'
  const stubs: Record<string, any> = {
    vue: { ref: (value: any) => ({ value }), watch: (_: any, callback: any) => { changed = callback } },
    '../accounts/auth': { account },
    '../../shared/device': { currentDeviceId: () => device, deviceContext: async () => ({ device_id: device, client_kind: 'web' }) },
    '../../shared/native/bridge': { isAndroidApp: () => false },
    './musicLibrary': { trackKey: (track: any) => `${track.provider}:${track.identifier}`, trackPayload: (track: any) => ({ ...track }) },
    './deviceAudio': { inspectDeviceAudio: (file: File) => inspection.run(file) },
  }
  const source = fs.readFileSync(new URL('../src/features/music/deviceLibrary.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const library: any = {}
  new Function('require', 'exports', 'indexedDB', compiled)((id: string) => { assert.ok(id in stubs, id); return stubs[id] }, library, storage.api)
  return { library, account, inspection, storage, changeAccount(id: number) { account.value = { id }; changed() }, changeDevice() { device = 'b'; changed() } }
}
const track = { title: 'local song', provider: 'qq', identifier: 'one', webpage_url: 'https://y.qq.com/one' }
test('Chrome local import remains playable after reload when picker permission returns to prompt', async t => {
  const previousWindow = globalThis.window
  const { library, storage } = setup(t)
  const file = new File(['actual audio bytes'], 'Chrome 导入.mp3', { lastModified: 1 })
  let permission = 'granted', fileReads = 0
  const handle = { getFile: async () => { fileReads++; if (permission !== 'granted') throw new DOMException('Permission required', 'NotAllowedError'); return file }, queryPermission: async () => permission }
  globalThis.window = { showOpenFilePicker: async () => [handle] } as any
  t.after(() => { globalThis.window = previousWindow })
  await library.chooseDeviceFiles(() => assert.fail('Chrome uses its native picker'))
  const imported = library.deviceTracks.value[0]
  assert.ok(imported)
  permission = 'prompt'
  globalThis.window = previousWindow
  const reloaded = setup(t, storage).library
  await reloaded.refreshDeviceLibrary()
  assert.equal(reloaded.deviceTracks.value.length, 1)
  assert.equal(reloaded.unavailableDeviceTracks.value.length, 0)
  const readsAfterImport = fileReads
  assert.equal(await (await fetch(await reloaded.devicePlaybackUrl(imported))).text(), 'actual audio bytes')
  assert.equal(fileReads, readsAfterImport, 'the saved copy must not reopen the picker handle')
})
test('file input survives a new module instance with empty memory and plays without a server', async t => {
  const { library, storage } = setup(t)
  await library.registerDeviceFile(track, new File(['audio'], 'song.mp3'))
  const reloaded = setup(t, storage).library
  assert.equal(reloaded.deviceTracks.value.length, 0)
  await reloaded.refreshDeviceLibrary()
  assert.equal(reloaded.deviceTracks.value.length, 1)
  assert.equal(reloaded.hasDeviceFile(track), true)
  const url = await reloaded.devicePlaybackUrl(track)
  assert.equal(await (await fetch(url)).text(), 'audio')
  await reloaded.removeDeviceTrack(track)
  assert.equal(reloaded.hasDeviceFile(track), false)
  assert.equal(storage.rows.size, 0)
})
test('refresh retains a permission-blocked handle and reauthorization restores it', async t => {
  const { library, storage } = setup(t)
  let permission = 'granted'
  const file = new File(['audio'], 'song.mp3')
  await library.registerDeviceFile(track, { getFile: async () => file, queryPermission: async () => permission,
    requestPermission: async () => { permission = 'granted'; return permission } })
  permission = 'prompt'
  const reloaded = setup(t, storage).library
  await reloaded.refreshDeviceLibrary()
  assert.equal(reloaded.deviceTracks.value.length, 0)
  assert.equal(reloaded.unavailableDeviceTracks.value.length, 1)
  assert.equal(reloaded.unavailableDeviceTracks.value[0].canAuthorize, true)
  assert.equal(storage.rows.size, 1)
  await reloaded.authorizeDeviceTrack(track)
  assert.equal(reloaded.deviceTracks.value.length, 1)
  assert.equal(reloaded.unavailableDeviceTracks.value.length, 0)
})
test('reauthorizing a legacy local import upgrades it to a copy that survives the next reload', async t => {
  const { library, storage } = setup(t)
  const imported = { ...track, provider: 'device' }
  let permission = 'granted'
  const file = new File(['legacy audio'], '旧导入.mp3')
  await library.registerDeviceFile(imported, { getFile: async () => file, queryPermission: async () => permission,
    requestPermission: async () => { permission = 'granted'; return permission } })
  permission = 'prompt'
  const reloaded = setup(t, storage).library
  await reloaded.refreshDeviceLibrary()
  assert.equal(reloaded.unavailableDeviceTracks.value.length, 1)
  await reloaded.authorizeDeviceTrack(imported)
  permission = 'prompt'
  const nextLoad = setup(t, storage).library
  await nextLoad.refreshDeviceLibrary()
  assert.equal(nextLoad.deviceTracks.value.length, 1)
  assert.equal(await (await fetch(await nextLoad.devicePlaybackUrl(imported))).text(), 'legacy audio')
  assert.equal([...storage.rows.values()][0].handle, undefined)
})
test('storage failures remain visible after refresh without marking a file as persisted', async t => {
  const { library, storage } = setup(t)
  storage.failWrites = true
  await library.registerDeviceFile(track, new File(['audio'], 'song.mp3'))
  await library.refreshDeviceLibrary()
  assert.match(library.deviceLibraryError.value, /仅可在本页播放/)
  assert.equal(storage.rows.size, 0)
  storage.failReads = true
  await library.refreshDeviceLibrary()
  assert.match(library.deviceLibraryError.value, /读取失败/)
})
test('persisted files do not become readable for another account or device', async t => {
  const { library, storage } = setup(t)
  await library.registerDeviceFile(track, new File(['audio'], 'song.mp3'))
  const other = setup(t, storage)
  other.changeAccount(2)
  await other.library.refreshDeviceLibrary()
  assert.equal(other.library.deviceTracks.value.length, 0)
  other.changeAccount(1); other.changeDevice()
  await other.library.refreshDeviceLibrary()
  assert.equal(other.library.deviceTracks.value.length, 0)
})
test('download artwork and lyrics persist across reload and manual LRC wins over later matching', async t => {
  const { library, storage } = setup(t)
  const withCover = { ...track, thumbnail: 'https://example.test/cover.jpg' }
  const lyrics = { lines: [{ time: 1, text: 'matched lyric' }], plain: '', source: 'fixture' }
  await library.registerDeviceFile(withCover, new File(['audio'], 'song.mp3'))
  t.mock.method(globalThis, 'fetch', async (url: string) => url.startsWith('/api/image-proxy')
    ? new Response('cover-bytes', { headers: { 'Content-Type': 'image/jpeg' } }) : Response.json(lyrics))
  await library.cacheDeviceAssets(withCover)
  const reloaded = setup(t, storage).library
  await reloaded.refreshDeviceLibrary()
  assert.match(reloaded.deviceTracks.value[0].device_thumbnail, /^blob:/)
  assert.equal(reloaded.deviceTracks.value[0].device_assets_notice, '')
  assert.deepEqual((await reloaded.readDeviceLyrics(track)).lines, lyrics.lines)
  const manual = { ...lyrics, lines: [{ time: 2, text: 'manual lyric' }] }
  await reloaded.saveDeviceLyrics(track, manual, true)
  await reloaded.saveDeviceLyrics(track, lyrics)
  assert.deepEqual((await reloaded.readDeviceLyrics(track)).lines, manual.lines)
})
test('failed companion downloads preserve the playable audio and expose a retryable notice', async t => {
  const { library } = setup(t)
  await library.registerDeviceFile(track, new File(['audio'], 'song.mp3'))
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: '歌词服务暂不可用' }, { status: 502 }))
  await library.cacheDeviceAssets(track)
  assert.equal(library.hasDeviceFile(track), true)
  assert.match(library.deviceTracks.value[0].device_assets_notice, /暂无可用封面.*歌词服务暂不可用/)
})
test('late companion responses preserve a replacement file and manually imported lyrics', async t => {
  const { library, storage } = setup(t)
  const originalFetch = globalThis.fetch
  const withCover = { ...track, thumbnail: '/fixture-cover.png' }
  await library.registerDeviceFile(withCover, new File(['original'], 'song.mp3'))
  let release!: (response: Response) => void, began!: () => void
  const started = new Promise<void>(resolve => { began = resolve })
  t.mock.method(globalThis, 'fetch', async (url: string) => url === '/fixture-cover.png'
    ? new Response('cover', { headers: { 'Content-Type': 'image/png' } })
    : new Promise<Response>(resolve => { release = resolve; began() }))
  const pending = library.cacheDeviceAssets(withCover)
  await started
  await library.registerDeviceFile(withCover, new File(['replacement'], 'song.mp3'))
  const manual = { lines: [{ time: 0, text: 'manual wins' }], plain: '', source: '本地 LRC' }
  await library.saveDeviceLyrics(track, manual, true)
  release(Response.json({ ...manual, lines: [{ time: 0, text: 'late automatic lyric' }] }))
  await pending
  const reloaded = setup(t, storage).library
  await reloaded.refreshDeviceLibrary()
  assert.deepEqual((await reloaded.readDeviceLyrics(track)).lines, manual.lines)
  assert.equal(await (await originalFetch(await reloaded.devicePlaybackUrl(track))).text(), 'replacement')
  assert.match(reloaded.deviceTracks.value[0].device_thumbnail, /^blob:/)
})
test('an empty lookup preserves cached lyrics and saving lyrics cannot restore a removed song', async t => {
  const { library, storage } = setup(t)
  await library.registerDeviceFile(track, new File(['audio'], 'song.mp3'))
  const saved = { lines: [{ time: 0, text: 'saved' }], plain: '', source: 'fixture' }
  await library.saveDeviceLyrics(track, saved)
  await library.saveDeviceLyrics(track, { lines: [], plain: '', source: '' })
  assert.deepEqual((await library.readDeviceLyrics(track)).lines, saved.lines)
  await library.removeDeviceTrack(track)
  assert.equal(await library.saveDeviceLyrics(track, saved), false)
  assert.equal(storage.rows.size, 0)
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
  let began!: () => void
  const started = new Promise<void>(resolve => { began = resolve })
  inspection.run = () => new Promise(resolve => { release = resolve; began() })
  const pending = library.importDeviceFiles([new File(['audio'], 'song.m4a')])
  await started
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
