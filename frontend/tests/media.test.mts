import assert from 'node:assert/strict'
import test from 'node:test'
import { downloadUrl, mediaFailureMessage, probeMediaWith } from '../src/shared/mediaPolicy.ts'

test('native media authorization refreshes once and releases diagnostic response bodies', async () => {
  let calls = 0, refreshes = 0, cancelled = 0
  const result = await probeMediaWith('/api/files/song.m4a', {
    fetch: async (_url, options) => {
      assert.deepEqual(options.headers, { Range: 'bytes=0-0' })
      return new Response(new ReadableStream({ cancel() { cancelled++ } }), { status: ++calls === 1 ? 401 : 206 })
    },
    refresh: async () => { refreshes++; return true },
  })
  assert.deepEqual(result, { status: 206, recovered: true })
  assert.equal(calls, 2)
  assert.equal(refreshes, 1)
  assert.equal(cancelled, 2)
})

test('a repeated 401 stops after one refresh and does not loop', async () => {
  let calls = 0, refreshes = 0
  const result = await probeMediaWith('/api/files/song.m4a', {
    fetch: async () => { calls++; return new Response('', { status: 401 }) },
    refresh: async () => { refreshes++; return true },
  })
  assert.deepEqual(result, { status: 401, recovered: false })
  assert.equal(calls, 2)
  assert.equal(refreshes, 1)
})

test('expired source and upstream/service failures do not refresh login', async () => {
  for (const status of [404, 410, 502, 503]) {
    const result = await probeMediaWith('/api/music/playback/id/content', {
      fetch: async () => new Response('', { status }),
      refresh: async () => { assert.fail('unexpected login refresh') },
    })
    assert.deepEqual(result, { status, recovered: false })
    assert.ok(mediaFailureMessage(status))
  }
})

test('explicit downloads preserve query parameters and reject external origins', () => {
  assert.equal(downloadUrl('/api/files/song.m4a?x=1', 'https://example.com/'), '/api/files/song.m4a?x=1&download=1')
  assert.throws(() => downloadUrl('https://other.example/song.mp3', 'https://example.com/'))
})
