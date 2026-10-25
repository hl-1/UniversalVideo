import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import { activeLyricIndex, audioFilename, parseImportedLrc } from '../src/features/music/lyrics.ts'

const song = { title: '发如雪', artist: '周杰伦', provider: 'qq', identifier: 'one', webpage_url: 'https://example.test/song/one' }
const result = { lines: [{ time: 0, text: '第一句' }, { time: 3, text: '第二句' }], plain: '', source: 'fixture' }
const settle = async () => { await vue.nextTick(); await new Promise(resolve => setImmediate(resolve)) }

function mount(t: any, fetcher: typeof fetch, cached?: any) {
  const previous = { fetch: globalThis.fetch, document: globalThis.document }
  globalThis.fetch = fetcher
  globalThis.document = { removeEventListener() {}, body: { style: { overflow: '' } } } as any
  const hooks: Array<() => void> = []
  const { descriptor } = parse(fs.readFileSync(new URL('../src/features/music/components/LyricsView.vue', import.meta.url), 'utf8'))
  const script = compileScript(descriptor, { id: 'lyrics-loading-regression' })
  const compiled = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const stubs: Record<string, any> = {
    '../discoveryEvents': { beginListening() {}, sampleListening() {}, pauseListening() {}, seekListening() {}, endListening() {} },
    vue: { ...vue, onMounted() {}, onBeforeUnmount: (hook: () => void) => hooks.push(hook) },
    'lucide-vue-next': {}, '../lyrics': { activeLyricIndex, audioFilename, parseImportedLrc },
    '../deviceLibrary': { hasDeviceFile: () => false, readDeviceLyrics: async () => cached, saveDeviceLyrics: async () => true,
      requestTrackLyrics: async (track: any, signal: AbortSignal) => {
        const response = await fetch('/api/music/lyrics/tracks/lookup', { method: 'POST', signal, body: JSON.stringify({ track }) })
        if (!response.ok) throw new Error((await response.json()).detail)
        return response.json()
      } },
    '../../../shared/downloads': {},
    '../../../shared/native/diagnostics': { diagnosticEvent() {} },
  }
  const module: any = {}
  new Function('require', 'exports', compiled)((id: string) => { assert.ok(id in stubs, id); return stubs[id] }, module)
  const props = vue.reactive({ track: { ...song }, currentTime: 0, lyricsEndpoint: undefined, resultVersion: undefined })
  const scope = vue.effectScope()
  const state = scope.run(() => module.default.setup(props, { expose() {} }))!
  t.after(() => { hooks.forEach(hook => hook()); scope.stop(); Object.assign(globalThis, previous) })
  return { props, state }
}

test('native progress snapshots do not cancel or reload lyrics for the same song', async t => {
  const requests: string[] = []
  const { props, state } = mount(t, async input => {
    requests.push(String(input))
    return Response.json(String(input).endsWith('/parse') ? { token: 'fixture', webpage_url: song.webpage_url } : result)
  })
  await settle()
  for (let tick = 1; tick <= 100; tick++) {
    props.track = JSON.parse(JSON.stringify(song))
    props.currentTime = tick * .75
    await settle()
  }
  assert.deepEqual(requests, ['/api/music/lyrics/tracks/lookup'])
  assert.deepEqual(state.lyrics.value.lines, result.lines)
  assert.equal(state.loading.value, false)
  props.track = { ...song, identifier: 'two', webpage_url: 'https://example.test/song/two' }
  await settle()
  assert.equal(requests.length, 2, 'switching songs must still load the new lyrics')
})

test('a slow lyrics request survives progress updates and repeated manual refresh clicks', async t => {
  let resolveLyrics!: (response: Response) => void
  let lyricsSignal: AbortSignal | undefined
  const requests: string[] = []
  const { props, state } = mount(t, async (input, options) => {
    requests.push(String(input))
    if (String(input).endsWith('/parse')) return Response.json({ token: 'fixture', webpage_url: song.webpage_url })
    lyricsSignal = options?.signal as AbortSignal
    return new Promise(resolve => { resolveLyrics = resolve })
  })
  await settle()
  for (let tick = 0; tick < 30; tick++) {
    props.track = { ...song }; props.currentTime++
    if (state.refreshLyrics) state.refreshLyrics()
    await settle()
  }
  assert.equal(requests.length, 1)
  assert.equal(lyricsSignal?.aborted, false)
  resolveLyrics(Response.json(result))
  await settle()
  assert.equal(state.loading.value, false)
  assert.equal(state.lyrics.value.lines[0].text, '第一句')
})

test('lyrics timeout stops the spinner, preserves visible lyrics and allows a manual retry', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let stall = false
  const { state } = mount(t, async (input, options) => {
    if (String(input).endsWith('/parse')) return Response.json({ token: 'fixture', webpage_url: song.webpage_url })
    if (!stall) return Response.json(result)
    return new Promise((_, reject) => options?.signal?.addEventListener('abort', () => reject(options.signal!.reason), { once: true }))
  })
  await settle()
  stall = true
  state.refreshLyrics()
  await settle()
  t.mock.timers.tick(30000)
  await settle()
  assert.equal(state.loading.value, false)
  assert.match(state.error.value, /超时/)
  assert.equal(state.lyrics.value.lines[0].text, '第一句')
  stall = false
  state.refreshLyrics()
  await settle()
  assert.equal(state.error.value, '')
  assert.equal(state.loading.value, false)
})

test('a late lookup cannot replace lyrics after switching songs', async t => {
  let resolveOld!: (response: Response) => void
  const requests: string[] = []
  const { props, state } = mount(t, async input => {
    requests.push(String(input))
    if (requests.length === 1) return new Promise(resolve => { resolveOld = resolve })
    return Response.json(String(input).endsWith('/parse') ? { token: 'new', webpage_url: 'https://example.test/song/two' } : result)
  })
  await settle()
  props.track = { ...song, identifier: 'two', webpage_url: 'https://example.test/song/two' }
  await settle()
  resolveOld(Response.json({ ...result, plain: '旧歌曲', lines: [] }))
  await settle()
  assert.equal(requests.length, 2)
  assert.deepEqual(state.lyrics.value.lines, result.lines)
  assert.equal(state.loading.value, false)
})

test('a device filename never becomes a server file lyrics lookup', async t => {
  const urls: string[] = []
  const { props, state } = mount(t, async (input, options) => {
    urls.push(String(input))
    const body = JSON.parse(String(options?.body))
    assert.equal(body.track.identifier, 'one')
    return Response.json(result)
  })
  props.track = { ...song, filename: '稻香.mp3', device_file: true } as any
  await settle()
  assert.ok(urls.every(url => url === '/api/music/lyrics/tracks/lookup'))
  assert.deepEqual(state.lyrics.value.lines, result.lines)
})

test('saved device lyrics remain visible with no network after reopening', async t => {
  const { state } = mount(t, async () => { assert.fail('cached lyrics must not require a network request') }, result)
  await settle()
  assert.deepEqual(state.lyrics.value.lines, result.lines)
  assert.equal(state.loading.value, false)
  assert.equal(state.error.value, '')
})

test('an unsuccessful explicit rematch keeps the saved lyrics visible', async t => {
  const { state } = mount(t, async () => Response.json({ lines: [], plain: '', source: '', message: '未找到新歌词' }), result)
  await settle()
  state.refreshLyrics()
  await settle()
  assert.deepEqual(state.lyrics.value.lines, result.lines)
  assert.match(state.error.value, /未找到新歌词/)
})

test('native snapshots retain song metadata and object identity while updating progress', async t => {
  const previous = globalThis.window
  const listeners = new Map<string, (event: any) => void>()
  globalThis.window = { addEventListener: (name: string, handler: any) => listeners.set(name, handler) } as any
  const account = { value: { id: 1 } }
  const scope = vue.effectScope()
  const stubs: Record<string, any> = {
    '../discoveryEvents': { beginListening() {}, sampleListening() {}, pauseListening() {}, seekListening() {}, endListening() {} },
    vue, '../musicLibrary': { trackKey: (track: any) => `${track.provider}:${track.identifier}` },
    '../../../shared/api/client': {}, '../../../shared/media': {},
    '../../../shared/native/bridge': { isAndroidApp: () => true },
    '../../accounts/auth': { account }, '../deviceLibrary': {}, '../deviceAudio': {},
  }
  const source = fs.readFileSync(new URL('../src/features/music/player/musicPlayer.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const player: any = {}
  scope.run(() => new Function('require', 'exports', compiled)((id: string) => { assert.ok(id in stubs, id); return stubs[id] }, player))
  t.after(() => { scope.stop(); globalThis.window = previous })
  player.currentTrack.value = { ...song, filename: '发如雪.mp3', custom_thumbnail: '/api/covers/custom' }
  const original = player.currentTrack.value
  for (let tick = 0; tick < 100; tick++) listeners.get('xianyue:player.state')!({ detail: {
    uid: 1, state: 'playing', track: { ...song }, position: tick * .75, duration: 299,
  } })
  assert.equal(player.currentTrack.value, original)
  assert.equal(player.currentTrack.value.filename, '发如雪.mp3')
  assert.equal(player.elapsed.value, 74.25)
  listeners.get('xianyue:player.state')!({ detail: { uid: 1, state: 'playing', track: { ...song, artist: '更新后的歌手' } } })
  assert.equal(player.currentTrack.value.artist, '更新后的歌手')
  assert.equal(player.currentTrack.value.custom_thumbnail, '/api/covers/custom')
  listeners.get('xianyue:player.state')!({ detail: { uid: 2, state: 'playing', track: { ...song, title: '其他账号的歌曲' } } })
  assert.equal(player.currentTrack.value.title, '发如雪')
})
