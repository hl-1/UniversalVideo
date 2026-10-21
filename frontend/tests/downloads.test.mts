import assert from 'node:assert/strict'
import test from 'node:test'
import { activeDownloads, chooseFile, downloadFilename, downloadMedia, downloadNotice, progressText, saveRemoteFile, type DeliveryPlan } from '../src/shared/downloads.ts'

function setup(t: any) {
  const previous = { window: globalThis.window, document: globalThis.document, fetch: globalThis.fetch }
  const events: string[] = [], chunks: Uint8Array[] = []
  const handle = { async createWritable() { events.push('writer'); return {
    async write(data: Uint8Array) { chunks.push(new Uint8Array(data)) },
    async close() { events.push('close') }, async abort() { events.push('abort') },
  } } }
  const browser = { showSaveFilePicker: async () => { events.push('picker'); return handle }, setTimeout }
  globalThis.window = browser as any
  globalThis.document = { body: { appendChild() {} }, createElement: () => ({ href: '', download: '', click() { events.push('native') }, remove() {} }) } as any
  t.after(() => Object.assign(globalThis, previous))
  return { events, chunks, handle, browser }
}

test('browser fallback delegates directly without buffering the file', async t => {
  const { events, browser } = setup(t)
  browser.showSaveFilePicker = undefined as any
  globalThis.fetch = async () => { assert.fail('native downloads must not buffer through fetch') }
  const result = await saveRemoteFile('/api/files/file.mp4', 'file.mp4')
  assert.deepEqual(events, ['native'])
  assert.equal(result.status, 'handed_off')
})

test('save picker receives a readable name without stripping ordinary numbered titles', async t => {
  const { browser } = setup(t)
  const names: string[] = []
  browser.showSaveFilePicker = async (options: any) => { names.push(options.suggestedName); assert.equal(options.startIn, 'downloads'); return {} as any }
  await chooseFile('964ba09f4629.Here For It All.m4a')
  await chooseFile('2026. My song.mp3')
  assert.deepEqual(names, ['Here For It All.m4a', '2026. My song.mp3'])
  assert.equal(downloadFilename('import-964ba09f4629.歌曲.mp3'), '歌曲.mp3')
  assert.equal(downloadFilename('964ba09f4629.mp3'), '964ba09f4629.mp3', 'a legacy ID-only file keeps its extension')
})

test('picker cancellation never creates a plan or a local success notice', async t => {
  const { browser } = setup(t)
  browser.showSaveFilePicker = async () => { throw new DOMException('cancelled', 'AbortError') }
  globalThis.fetch = async () => { assert.fail('cancelled selection must not start a request') }
  await assert.rejects(downloadMedia({ url: 'https://example.com/song' }), { name: 'AbortError' })
  assert.equal(activeDownloads.value.size, 0)
  assert.equal(downloadNotice.value, '下载已取消。')
})

test('progress text reports actual low speeds and keeps unknown file sizes indeterminate', () => {
  const text = progressText({ phase: 'transferring', bytes: 1048576, total: 2097152, speed: 32768, eta: 32 })
  assert.match(text, /50%/)
  assert.match(text, /32 KB\/s/)
  assert.match(text, /剩余约 32 秒/)
  assert.doesNotMatch(progressText({ phase: 'transferring', bytes: 20, total: null, speed: 0, eta: null }), /%|剩余/)
})

test('prepared song names and visible progress never expose server task identifiers', async t => {
  setup(t)
  const seen: any[] = []
  globalThis.fetch = async (input, options) => {
    const url = String(input)
    if (url.endsWith('/plan')) {
      assert.equal(activeDownloads.value.size, 1)
      return Response.json({ download_id: 'id', mode: 'task', task_id: 'task', destination: 'device', filename: 'Here For It All.m4a' })
    }
    if (url === '/api/tasks/task') return Response.json({ status: 'finished', filename: '964ba09f4629.Here For It All.m4a', download_url: '/file' })
    if (url.endsWith('/complete')) return Response.json({ status: JSON.parse(String(options?.body)).status })
    return new Response('song bytes', { headers: { 'Content-Length': '10' } })
  }
  const result = await downloadMedia({ url: 'https://example.com/song', title: 'Here For It All', media_type: 'audio', audio_format: 'm4a' }, {
    onProgress(progress) { seen.push([...activeDownloads.value.values()].map(row => ({ label: row.label, detail: row.detail }))) },
  })
  assert.equal(result.plan.filename, 'Here For It All.m4a')
  const transferring = seen.flat().find(row => row.detail?.bytes === 10)
  assert.equal(transferring.label, 'Here For It All.m4a')
  assert.equal(transferring.detail.total, 10)
  assert.ok(transferring.detail.speed > 0)
  assert.equal(activeDownloads.value.size, 0)
})

test('a rejected direct request uses the authenticated relay and commits matching bytes', async t => {
  const { events, chunks } = setup(t)
  const requests: string[] = []
  globalThis.fetch = async url => {
    requests.push(String(url))
    if (String(url).startsWith('https:')) throw new TypeError('CORS')
    return new Response('media bytes', { headers: { 'Content-Length': '11' } })
  }
  const result = await saveRemoteFile('https://example.com/video', 'video.mp4', { fallbackUrl: '/api/downloads/id/content' })
  assert.equal(result.status, 'saved')
  assert.equal(result.bytes, 11)
  assert.deepEqual(requests, ['https://example.com/video', '/api/downloads/id/content'])
  assert.equal(Buffer.concat(chunks).toString(), 'media bytes')
  assert.deepEqual(events, ['picker', 'writer', 'close'])
})

test('media selection precedes planning and completion is reported only after file close', async t => {
  const { events } = setup(t)
  globalThis.fetch = async (input, options) => {
    const url = String(input)
    if (url.endsWith('/plan')) { events.push('plan'); return Response.json({ download_id: 'id', mode: 'relay', destination: 'device', filename: 'a.mp4', url: '/content', fallback_url: '/content' }) }
    if (url.endsWith('/complete')) {
      assert.equal(events.at(-1), 'close')
      assert.equal(JSON.parse(String(options?.body)).completion_source, 'filesystem')
      events.push('ack'); return Response.json({ status: 'completed' })
    }
    return new Response('media')
  }
  assert.equal((await downloadMedia({ url: 'https://example.com/video' })).status, 'saved')
  assert.deepEqual(events, ['picker', 'plan', 'writer', 'close', 'ack'])
})

test('a truncated audio stream restarts once from byte zero through compatibility processing', async t => {
  const { events } = setup(t)
  let plans = 0
  globalThis.fetch = async (input, options) => {
    const url = String(input)
    if (url.endsWith('/plan')) {
      plans++
      assert.equal(Boolean(JSON.parse(String(options?.body)).compatibility), plans === 2)
      return Response.json({ download_id: `id${plans}`, mode: 'relay', filename: 'a.mp3', url: `/content${plans}`, fallback_url: `/content${plans}` })
    }
    if (url.endsWith('/complete')) return Response.json({ status: 'completed' })
    return url.endsWith('1') ? new Response('bad', { headers: { 'Content-Length': '100' } }) : new Response('complete')
  }
  const result = await downloadMedia({ url: 'https://example.com/song', media_type: 'audio', audio_format: 'mp3' })
  assert.equal(result.status, 'saved')
  assert.equal(plans, 2)
  assert.equal(events.filter(event => event === 'abort').length, 1)
  assert.equal(events.filter(event => event === 'close').length, 1)
  assert.equal(events.filter(event => event === 'picker').length, 1)
})

test('cancellation during planning never starts a file write', async t => {
  const { events } = setup(t)
  const controller = new AbortController()
  globalThis.fetch = async () => { controller.abort(new DOMException('cancelled', 'AbortError')); controller.signal.throwIfAborted(); throw new Error('unreachable') }
  await assert.rejects(downloadMedia({ url: 'https://example.com/video' }, { controller }), { name: 'AbortError' })
  assert.deepEqual(events, ['picker'])
})

test('cloud retention confirms its server task without opening a local picker', async t => {
  const { events } = setup(t)
  globalThis.fetch = async (input, options) => {
    const url = String(input)
    if (url.endsWith('/plan')) return Response.json({ download_id: 'cloud', mode: 'task', task_id: 'task', destination: 'library', filename: 'song.mp3' })
    if (url === '/api/tasks/task') return Response.json({ status: 'finished', filename: 'song.mp3', download_url: '/api/files/song.mp3' })
    if (url.endsWith('/complete')) {
      assert.equal(JSON.parse(String(options?.body)).status, 'library_saved')
      events.push('cloud-ack'); return Response.json({ status: 'library_saved' })
    }
    assert.fail(`unexpected file request: ${url}`)
  }
  const result = await downloadMedia({ url: 'https://example.com/song', destination: 'library', media_type: 'audio', audio_format: 'mp3' })
  assert.equal(result.status, 'library')
  assert.deepEqual(events, ['cloud-ack'])
})

test('batch-provided validated plan and file handle are reused without another picker or plan', async t => {
  const { events, chunks, handle } = setup(t)
  const plan: DeliveryPlan = { download_id: 'prepared', destination: 'device', mode: 'task', filename: 'a.mp3', url: '/ready-file', fallback_url: '/ready-file' }
  globalThis.fetch = async (input, options) => {
    if (String(input) === '/ready-file') return new Response('validated media')
    assert.equal(String(input), '/api/downloads/prepared/complete')
    assert.equal(JSON.parse(String(options?.body)).status, 'completed')
    assert.equal(events.at(-1), 'close')
    return Response.json({ status: 'completed' })
  }
  const result = await downloadMedia({ url: 'https://example.com/song', media_type: 'audio' }, { handle, preparedPlan: plan })
  assert.equal(result.plan, plan)
  assert.equal(result.status, 'saved')
  assert.deepEqual(events, ['writer', 'close'])
  assert.equal(Buffer.concat(chunks).toString(), 'validated media')
})

test('a server-confirmed failure overrides a successful file-close result', async t => {
  const { events } = setup(t)
  globalThis.fetch = async input => {
    if (String(input).endsWith('/plan')) return Response.json({ download_id: 'failed', mode: 'relay', destination: 'device', filename: 'a.mp3', url: '/content', fallback_url: '/content' })
    if (String(input).endsWith('/complete')) return Response.json({ status: 'failed' })
    return new Response('partial data that the browser accepted')
  }
  await assert.rejects(downloadMedia({ url: 'https://example.com/song', media_type: 'audio' }), /不能记为已保存/)
  assert.ok(events.includes('close'))
  assert.match(downloadNotice.value, /不能记为已保存/)
})

test('failed completion acknowledgement is not swallowed and cannot produce a saved result', async t => {
  setup(t)
  globalThis.fetch = async input => {
    if (String(input).endsWith('/plan')) return Response.json({ download_id: 'unknown', mode: 'relay', destination: 'device', filename: 'a.mp3', url: '/content', fallback_url: '/content' })
    if (String(input).endsWith('/complete')) return Response.json({ detail: '无法确认保存状态' }, { status: 503 })
    return new Response('media')
  }
  await assert.rejects(downloadMedia({ url: 'https://example.com/song', media_type: 'audio' }), /无法确认保存状态/)
})
