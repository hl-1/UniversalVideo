import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

function setup(t: any) {
  const previousWindow = globalThis.window
  const previousFetch = globalThis.fetch
  const history: any[] = []
  const requests: string[] = []
  const stubs: Record<string, any> = {
    vue: { ref: (value: any) => ({ value }), nextTick: async () => {} },
    '../musicLibrary': { libraryError: { value: '' }, refreshLibrary: async () => {}, trackKey: (track: any) => track.webpage_url, trackPayload: (track: any) => track },
    '../../../shared/api/client': {
      api: async (url: string, options: any) => { if (url === '/api/library/history') history.push(JSON.parse(options.body)); return {} },
      jsonRequest: (method: string, body: any) => ({ method, body: JSON.stringify(body) }),
    },
    '../../../shared/media': {},
  }
  const source = fs.readFileSync(new URL('../src/features/music/player/musicPlayer.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const player: any = {}
  new Function('require', 'exports', compiled)((id: string) => { assert.ok(id in stubs, id); return stubs[id] }, player)
  globalThis.window = { location: { href: 'http://localhost:5174/' }, setTimeout: () => 1, clearTimeout() {} } as any
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
  return { player, element, event, track, history, requests }
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

test('short local copy is stopped and replaced with verified remote audio', async t => {
  const { player, element, event, track, history, requests } = setup(t)
  await player.playTrack(track)
  element.duration = 60
  player.onMetadata(event)
  assert.equal(player.playbackState.value, 'loading')
  assert.equal(history.length, 0)
  await settle()
  assert.deepEqual(requests, ['/api/music/playback'])
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
  assert.equal(requests.length, 1)
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
  assert.equal(requests.length, 1)
})
