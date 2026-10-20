import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'
import { saveResponseToFile } from '../src/features/video/fileSave.ts'

test('a 750 MB stream is saved in bounded batches with identical bytes', async () => {
  const total = 750_503_209
  const sourceHash = createHash('sha256')
  const savedHash = createHash('sha256')
  let produced = 0
  let written = 0
  let writes = 0
  let closed = false
  let lastProgress = 0
  const response = new Response(new ReadableStream({
    pull(stream) {
      if (produced === total) { stream.close(); return }
      const chunk = new Uint8Array(Math.min(64 * 1024, total - produced)).fill(produced % 251)
      sourceHash.update(chunk)
      produced += chunk.byteLength
      stream.enqueue(chunk)
    },
  }), { headers: { 'Content-Length': String(total) } })
  // A whole-file blob would invalidate the streaming guarantee.
  response.blob = async () => { throw new Error('whole-file buffering') }
  await saveResponseToFile(response, {
    async createWritable() {
      return {
        async write(chunk) {
          assert(chunk instanceof Uint8Array)
          assert(chunk.byteLength <= 1024 * 1024)
          savedHash.update(chunk)
          written += chunk.byteLength
          writes++
        },
        async close() { closed = true },
      }
    },
  }, new AbortController(), (bytes, length) => {
    assert(bytes >= lastProgress)
    assert.equal(length, total)
    lastProgress = bytes
  }, () => { assert.equal(closed, false) })
  assert.equal(written, total)
  assert.equal(writes, Math.ceil(total / (1024 * 1024)))
  assert.equal(sourceHash.digest('hex'), savedHash.digest('hex'))
  assert.equal(closed, true)
  assert.equal(lastProgress, total)
})

test('truncated and empty responses abort without committing the file', async () => {
  for (const body of ['short', '']) {
    let closed = false
    let aborted = false
    await assert.rejects(saveResponseToFile(new Response(body, { headers: { 'Content-Length': '20' } }), {
      async createWritable() {
        return { async write() {}, async close() { closed = true }, async abort() { aborted = true } }
      },
    }, new AbortController(), () => {}, () => {}), /不完整/)
    assert.equal(closed, false)
    assert.equal(aborted, true)
  }
})

test('a stalled read times out and cancels its reader and writer', async () => {
  let cancelled = false
  let aborted = false
  const response = new Response(new ReadableStream({ cancel() { cancelled = true } }))
  await assert.rejects(saveResponseToFile(response, {
    async createWritable() {
      return { async write() {}, async close() {}, async abort() { aborted = true } }
    },
  }, new AbortController(), () => {}, () => {}, 10), /没有进展/)
  assert.equal(cancelled, true)
  assert.equal(aborted, true)
})

test('stalled writes and close release the save even when abort cannot settle', async () => {
  for (const stage of ['write', 'close']) {
    let aborted = false
    const never = () => new Promise<void>(() => {})
    await assert.rejects(saveResponseToFile(new Response('video'), {
      async createWritable() {
        return {
          write: stage === 'write' ? never : async () => {},
          close: stage === 'close' ? never : async () => {},
          abort() { aborted = true; return never() },
        }
      },
    }, new AbortController(), () => {}, () => {}, 10), /没有进展/)
    assert.equal(aborted, true)
  }
})

test('a writer that arrives after cancellation is aborted', async () => {
  let open!: (value: any) => void
  let aborted = false
  const controller = new AbortController()
  const save = saveResponseToFile(new Response('video'), {
    createWritable: () => new Promise(resolve => { open = resolve }),
  }, controller, () => {}, () => {})
  await new Promise(resolve => setImmediate(resolve))
  controller.abort(new DOMException('cancelled', 'AbortError'))
  await assert.rejects(save, { name: 'AbortError' })
  open({ async write() { assert.fail('cancelled writer must not write') }, async close() {}, async abort() { aborted = true } })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(aborted, true)
})
