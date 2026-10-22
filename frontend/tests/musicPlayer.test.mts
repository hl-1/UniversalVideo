import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { deviceAudioFailure } from '../src/features/music/deviceAudio.ts'

function setup(t: any) {
  const previousWindow = globalThis.window
  const previousFetch = globalThis.fetch
  const history: any[] = []
  const requests: string[] = []
  const removed: any[] = []
  const stubs: Record<string, any> = {
    vue: { ref: (value: any) => ({ value }), nextTick: async () => {}, watch() {} },
    '../../accounts/auth': { account: { value: { id: 1 } } },
    '../deviceLibrary': { devicePlaybackUrl: async (track: any) => track.device_url || '', releaseDevicePlaybackUrls() {}, removeDeviceTrack: async (track: any) => { removed.push(track) } },
    '../deviceAudio': { deviceAudioFailure },
    '../musicLibrary': { libraryError: { value: '' }, refreshLibrary: async () => {}, trackKey: (track: any) => track.webpage_url, trackPayload: (track: any) => track },
    '../../../shared/api/client': {
      api: async (url: string, options: any) => { if (url === '/api/library/history') history.push(JSON.parse(options.body)); return {} },
      jsonRequest: (method: string, body: any) => ({ method, body: JSON.stringify(body) }),
    },
    '../../../shared/media': { probeMedia: async () => { requests.push('probe'); return { status: 200, recovered: false } } },
  }
  const source = fs.readFileSync(new URL('../src/features/music/player/musicPlayer.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const player: any = {}
  new Function('require', 'exports', compiled)((id: string) => { assert.ok(id in stubs, id); return stubs[id] }, player)
  globalThis.window = { location: { href: 'http://localhost:5174/' }, setTimeout: () => 1, clearTimeout() {}, addEventListener() {} } as any
  globalThis.fetch = async url => {
    requests.push(String(url))
    return Response.json({ playback_url: '/api/music/playback/verified/content', expires_in: 1800, full: true, duration: 257 })
  }
  const element: any = { duration: 0, currentTime: 0, currentSrc: '', volume: 1, muted: false,
    pause() {}, async play() {}, load() { element.currentSrc = new URL(player.playbackUrl.value, window.location.href).href } }
  player.audio.value = element
  const event = { target: element }
  const track = { title: '天外来物', artist: '薛之谦', provider: 'qq', token: 'searched', duration: 257,
    webpage_url: 'https://y.qq.com/n/ryqq/songDetail/0013WPvt4fQH2b', local_url: '/api/files/song.m4a' }
  t.after(() => { globalThis.window = previousWindow; globalThis.fetch = previousFetch })
  return { player, element, event, track, history, requests, removed }
}
const settle = () => new Promise(resolve => setImmediate(resolve))

test('zero and unknown metadata preserve expected duration without a false preview', async t => {
  const { player, element, event, track, history } = setup(t)
  await player.playTrack(track)
  for (const duration of [0, NaN, Infinity]) {
    element.duration = duration
    player.onMetadata(event)
    assert.equal(player.total.value, 257)
    assert.equal(player.playbackError.value, '')
  }
  player.onPlaying(event)
  assert.equal(history.length, 0)
  element.duration = 257.2
  player.onMetadata(event)
  await settle()
  assert.equal(player.total.value, 257.2)
  assert.equal(history.length, 1)
  assert.equal(history[0].preview, false)
  player.onPlaying(event)
  assert.equal(history.length, 1)
})

test('short progressive source is stopped and replaced with verified remote audio', async t => {
  const { player, element, event, track, history, requests } = setup(t)
  await player.playTrack(track)
  element.duration = 60
  player.onMetadata(event)
  assert.equal(player.playbackState.value, 'loading')
  assert.equal(history.length, 0)
  await settle()
  assert.deepEqual(requests, ['/api/music/playback', '/api/music/playback'])
  assert.equal(player.playbackUrl.value, '/api/music/playback/verified/content')
  element.duration = 257.2
  player.onPlaying(event)
  await settle()
  assert.equal(player.playbackState.value, 'playing')
  assert.equal(history.length, 1)
  assert.equal(history[0].preview, false)
})

test('a repeatedly incomplete source stops without a playback or retry loop', async t => {
  const { player, element, event, track, history, requests } = setup(t)
  await player.playTrack(track)
  element.duration = 60
  player.onMetadata(event)
  await settle()
  player.onPlaying(event)
  await settle()
  assert.equal(requests.length, 2)
  assert.equal(player.playbackUrl.value, '')
  assert.equal(player.playbackState.value, 'error')
  assert.match(player.playbackError.value, /完整歌曲/)
  assert.equal(history.length, 0)
})

test('exhausted server sources leave an error and never record a trial', async t => {
  const { player, track, history } = setup(t)
  globalThis.fetch = async () => Response.json({ detail: '未找到可验证的完整歌曲' }, { status: 422 })
  await player.playTrack({ ...track, local_url: null })
  assert.equal(player.playbackState.value, 'error')
  assert.match(player.playbackError.value, /完整歌曲/)
  assert.equal(player.playbackUrl.value, '')
  assert.equal(history.length, 0)
})

test('a different longer version is rejected as well', async t => {
  const { player, track, element, event, requests } = setup(t)
  await player.playTrack(track)
  element.duration = 319
  player.onMetadata(event)
  await settle()
  assert.equal(requests.length, 2)
})
test('local decode errors report the file problem without network probing or a recovery loop', async t => {
  const { player, element, event, requests, removed, history } = setup(t)
  const track = { title: 'broken audio', provider: 'device', webpage_url: 'device:one', device_url: 'blob:http://localhost:5174/local' }
  await player.playTrack(track)
  element.error = { code: 3 }
  player.onError(event)
  await settle()
  assert.match(player.playbackError.value, /本地音频解码失败/)
  assert.equal(player.playbackState.value, 'error')
  assert.equal(removed.length, 1)
  assert.deepEqual(requests, [])
  assert.deepEqual(history, [])
})
test('an interrupted play promise does not trigger media recovery', async t => {
  const { player, element, requests, track } = setup(t)
  element.play = async () => { throw new DOMException('interrupted by load', 'AbortError') }
  await player.playTrack({ ...track, provider: 'device', device_url: 'blob:http://localhost:5174/local' })
  assert.equal(player.playbackError.value, '')
  assert.deepEqual(requests, [])
})
