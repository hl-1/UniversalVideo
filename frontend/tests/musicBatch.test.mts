import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

const track = (id: number) => ({ title: `song ${id}`, artist: 'artist', provider: 'qq', identifier: String(id), webpage_url: `https://example.com/song/${id}` })
const idOf = (request: any) => Number(request.url.split('/').at(-1))
const tick = () => new Promise(resolve => setImmediate(resolve))

function setup(t: any, directory = true) {
  const previousWindow = globalThis.window
  const events: string[] = [], saved: number[] = [], handedOff: number[] = [], plans: any[] = []
  const failedPrepare = new Set<number>(), failedSave = new Set<number>(), timedOut = new Set<number>()
  const names = new Set(['song 1.mp3'])
  const hooks = { onPrepare: (_: any) => {}, active: 0, maxActive: 0, sameName: false, android: false, nativeStates: [] as any[], cached: [] as number[] }
  const account = { value: { id: 1 } }
  const watchers: Array<() => void> = []
  const stubs: Record<string, any> = {
    '../../shared/native/bridge': { isAndroidApp: () => hooks.android, nativeCall: async (action: string, payload: any) => {
      assert.equal(hooks.android, true, 'browser batches must not invoke Android')
      if (action === 'download.enqueueBatch') return { items: payload.tracks.map((row: any, index: number) => ({ id: row.identifier, index })) }
      if (action === 'download.states') return { items: hooks.nativeStates }
      assert.fail(`unexpected native action: ${action}`)
    } },
    './deviceLibrary': { refreshDeviceLibrary: async () => { events.push('refresh-device') }, cacheDeviceAssets: async (row: any) => { hooks.cached.push(Number(row.identifier)) } },
    vue: { ref: (value: any) => ({ value }), watch: (_: any, callback: () => void) => watchers.push(callback) },
    '../accounts/auth': { account },
    './musicLibrary': { trackKey: (row: any) => `qq:${row.identifier}`, trackPayload: (row: any) => row },
    './musicDownloads': {
      isMusicDownloaded: (row: any) => saved.includes(Number(row.identifier)) || row.file_available === true,
      refreshMusicDownloadStatus: async () => { events.push('status'); return new Map() },
      rememberMusicDownloads: (rows: any[], uid: number) => { if (uid === account.value.id) saved.push(...rows.map(row => Number(row.identifier))) },
    },
    '../../shared/api/client': {
      api: async (url: string, options: any) => {
        events.push(url)
        assert.equal(url, '/api/music/parse', 'local batches must never request a ZIP or cloud batch')
        options.signal.throwIfAborted()
        const id = idOf(JSON.parse(options.body))
        return { ...track(id), token: `token-${id}` }
      },
      jsonRequest: (method: string, body: any) => ({ method, body: JSON.stringify(body) }),
    },
    '../../shared/downloads': {
      progressText: () => 'saving',
      deliveryComplete: async (id: string, status: string) => { events.push(`ack:${id}:${status}`) },
      prepareMedia: async (request: any, controller: AbortController) => {
        const id = idOf(request)
        hooks.onPrepare(request)
        controller.signal.throwIfAborted()
        plans.push(request)
        events.push(`prepare:${id}`)
        if (failedPrepare.has(id)) throw new Error('audio validation failed')
        return { download_id: `id-${id}`, destination: 'device', mode: request.compatibility ? 'task' : 'relay', filename: hooks.sameName ? 'same song.mp3' : `song ${id}.mp3`, url: `/files/${id}`, fallback_url: `/files/${id}` }
      },
      downloadMedia: async (request: any, options: any) => {
        const id = idOf(request)
        hooks.active++; hooks.maxActive = Math.max(hooks.maxActive, hooks.active)
        try {
          await tick()
          if (timedOut.has(id)) options.controller.abort(new Error('file stalled'))
          options.controller.signal.throwIfAborted()
          assert.equal(options.preparedPlan.download_id, `id-${id}`)
          if (failedSave.has(id)) throw new Error('audio incomplete')
          options.onProgress({ phase: 'transferring', bytes: 5, total: 10, speed: 2048, eta: .1 })
          if (options.handle) { events.push(`close:${options.handle.name}`) }
          else { assert.equal(options.handle, null); handedOff.push(id) }
          return { status: options.handle ? 'saved' : 'handed_off', plan: options.preparedPlan, bytes: options.handle ? 10 : 0 }
        } finally { hooks.active-- }
      },
    },
  }
  globalThis.window = {
    showDirectoryPicker: directory ? async () => {
      events.push('directory')
      return {
        getFileHandle: async (name: string, options: any) => {
          if (!options?.create && !names.has(name)) throw new DOMException('missing', 'NotFoundError')
          if (options?.create) { names.add(name); events.push(`create:${name}`) }
          return { name }
        },
        removeEntry: async (name: string) => { names.delete(name); events.push(`remove:${name}`) },
      }
    } : undefined,
  } as any
  const source = fs.readFileSync(new URL('../src/features/music/musicBatch.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const batch: any = {}
  new Function('require', 'exports', compiled)((id: string) => { assert.ok(id in stubs, id); return stubs[id] }, batch)
  t.after(() => { globalThis.window = previousWindow })
  return { batch, events, saved, handedOff, plans, names, failedPrepare, failedSave, timedOut, hooks, account, watchers }
}

test('explicit directory batch chooses once before requests, keeps successes and skips a failed song', async t => {
  const fixture = setup(t)
  fixture.failedSave.add(2)
  await fixture.batch.createMusicBatch([track(1), track(2), track(3), track(1)], 'mp3', undefined, { local_mode: 'directory' })
  const result = fixture.batch.deviceBatch.value
  assert.equal(fixture.events[0], 'directory')
  assert.equal(fixture.events.filter(event => event === 'directory').length, 1)
  assert.deepEqual(fixture.saved.sort(), [1, 3])
  assert.deepEqual(result.items.map((item: any) => item.status), ['finished', 'failed', 'finished'])
  assert.equal(result.total, 3)
  assert.equal(result.finished, 2)
  assert.equal(result.failed, 1)
  assert.equal(result.progress, 100)
  assert.equal(fixture.hooks.maxActive, 2)
  assert.ok(fixture.names.has('song 1.mp3'), 'existing file preserved')
  assert.ok(fixture.names.has('song 1 (2).mp3'))
  assert.ok(fixture.names.has('song 3.mp3'))
  assert.ok(!fixture.names.has('song 2.mp3'), 'failed new file removed')
  fixture.failedSave.clear()
  await fixture.batch.retryDeviceBatch()
  assert.equal(fixture.batch.deviceBatch.value.total, 1)
  assert.deepEqual(fixture.plans.map(idOf), [1, 2, 3, 2])
})

test('native batch completion caches companions only for successfully saved songs', async t => {
  const fixture = setup(t)
  fixture.hooks.android = true
  fixture.hooks.nativeStates = [1, 2, 3].map(id => ({ id: String(id), status: id === 2 ? 'failed' : 'completed', bytes: 10, total: 10, error: id === 2 ? 'failed audio' : '', plan: { download_id: `id-${id}` } }))
  await fixture.batch.createMusicBatch([track(1), track(2), track(3)], 'mp3', undefined, { local_mode: 'files' })
  assert.equal(fixture.batch.deviceBatch.value.finished, 2)
  assert.deepEqual(fixture.hooks.cached, [1, 3])
  assert.ok(fixture.events.includes('refresh-device'))
})

test('unsupported directory API validates and hands off separate songs, continuing after a failure', async t => {
  const fixture = setup(t, false)
  fixture.failedPrepare.add(2)
  await fixture.batch.createMusicBatch([track(1), track(2), track(3)], 'mp3')
  const result = fixture.batch.deviceBatch.value
  assert.deepEqual(fixture.handedOff, [1, 3])
  assert.deepEqual(fixture.saved, [], 'native handoff never claims a successful local save')
  assert.ok(fixture.plans.every(request => request.compatibility === true))
  assert.equal(result.finished, 0)
  assert.equal(result.handed_off, 2)
  assert.equal(result.failed, 1)
  assert.deepEqual(result.items.map((item: any) => item.download_url), ['/files/1', undefined, '/files/3'])
  assert.equal(fixture.hooks.maxActive, 1)
})

test('default batch chooses a local folder before requests and exposes real transfer details', async t => {
  const fixture = setup(t)
  await fixture.batch.createMusicBatch([track(1), track(2)], 'mp3')
  assert.equal(fixture.events[0], 'directory')
  assert.deepEqual(fixture.saved.sort(), [1, 2])
  assert.deepEqual(fixture.handedOff, [])
  assert.equal(fixture.batch.deviceBatch.value.items[0].transfer.speed, 2048)
  assert.equal(fixture.batch.deviceBatch.value.items[0].transfer.bytes, 5)
})

test('explicit browser mode works even when a directory picker is available', async t => {
  const fixture = setup(t)
  await fixture.batch.createMusicBatch([track(1), track(2)], 'mp3', undefined, { local_mode: 'files' })
  assert.ok(!fixture.events.includes('directory'))
  assert.ok(fixture.plans.every(request => request.compatibility === true))
  assert.deepEqual(fixture.handedOff, [1, 2])
  assert.deepEqual(fixture.saved, [])
  assert.equal(fixture.batch.deviceBatch.value.handed_off, 2)
})

test('cancelling the folder picker starts no status, preparation or media request', async t => {
  const fixture = setup(t)
  window.showDirectoryPicker = async () => { throw new DOMException('cancelled', 'AbortError') }
  await assert.rejects(fixture.batch.createMusicBatch([track(1)], 'mp3'), { name: 'AbortError' })
  assert.deepEqual(fixture.events, [])
  assert.deepEqual(fixture.plans, [])
  assert.equal(fixture.batch.deviceBatch.value, null)
})

test('concurrent songs with matching names reserve separate local files', async t => {
  const fixture = setup(t)
  fixture.hooks.sameName = true
  await fixture.batch.createMusicBatch([track(1), track(2)], 'mp3')
  assert.ok(fixture.names.has('same song.mp3'))
  assert.ok(fixture.names.has('same song (2).mp3'))
  assert.deepEqual(fixture.saved.sort(), [1, 2])
})

test('a single file stall fails that song and leaves later downloads running', async t => {
  const fixture = setup(t)
  fixture.timedOut.add(2)
  await fixture.batch.createMusicBatch([track(1), track(2), track(3), track(4)], 'mp3', undefined, { local_mode: 'directory' })
  assert.deepEqual(fixture.saved.sort(), [1, 3, 4])
  assert.equal(fixture.batch.deviceBatch.value.failed, 1)
  assert.equal(fixture.batch.deviceBatch.value.cancelled, 0)
})

test('explicit cancellation stops new songs and allows retrying only unfinished items', async t => {
  const fixture = setup(t, false)
  fixture.hooks.onPrepare = request => { if (idOf(request) === 2) fixture.batch.cancelDeviceBatch() }
  await fixture.batch.createMusicBatch([track(1), track(2), track(3)], 'mp3')
  assert.deepEqual(fixture.handedOff, [1])
  assert.equal(fixture.batch.deviceBatch.value.status, 'cancelled')
  assert.equal(fixture.batch.deviceBatch.value.cancelled, 2)
  assert.deepEqual(fixture.batch.deviceBatch.value.items.map((item: any) => item.status), ['handed_off', 'cancelled', 'cancelled'])
  fixture.hooks.onPrepare = () => {}
  await fixture.batch.retryDeviceBatch()
  assert.deepEqual(fixture.handedOff, [1, 2, 3])
})

test('account switch cancels the old queue and does not publish old results to the new account', async t => {
  const fixture = setup(t, false)
  fixture.hooks.onPrepare = () => { fixture.account.value.id = 2; fixture.watchers.forEach(callback => callback()) }
  await fixture.batch.createMusicBatch([track(1), track(2)], 'mp3')
  assert.equal(fixture.batch.deviceBatch.value, null)
  assert.deepEqual(fixture.saved, [])
  assert.deepEqual(fixture.handedOff, [])
})
