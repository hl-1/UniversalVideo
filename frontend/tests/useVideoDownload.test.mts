import assert from 'node:assert/strict'
import test from 'node:test'
import { ref } from 'vue'
import { useVideoDownload } from '../src/features/video/useVideoDownload.ts'
import type { FilePickerHandle, TaskResult, ParseResult } from '../src/features/video/types.ts'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const settle = () => new Promise(resolve => setImmediate(resolve))
const finished = (taskId = 'first'): TaskResult => ({ task_id: taskId, status: 'finished', progress: 100, message: '文件已准备好', filename: `${taskId}.mp4`, download_url: `/api/files/${taskId}.mp4` })
const running = (taskId = 'first'): TaskResult => ({ task_id: taskId, status: 'running', progress: 50, message: '下载中' })

function setup(t: any, refreshTaskReferences = async () => {}) {
  const previousFetch = globalThis.fetch
  const previousWindow = globalThis.window
  let timerId = 0
  const timers = new Map<number, () => void>()
  const storage = new Map<string, string>()
  const browser = {
    localStorage: { getItem(key: string) { return storage.get(key) ?? null }, setItem(key: string, value: string) { storage.set(key, value) } },
    showSaveFilePicker: undefined as undefined | (() => Promise<FilePickerHandle>),
    setTimeout(callback: () => void) { const id = ++timerId; timers.set(id, callback); return id },
    clearTimeout(id: number) { timers.delete(id) },
  }
  globalThis.window = browser as unknown as Window & typeof globalThis
  const context = {
    url: ref('https://www.bilibili.com/video/sample'),
    parseResult: ref<ParseResult | null>({ title: '测试视频', formats: [] }),
    activeRecordId: ref('record'), revisions: { record: 0 }, errorMessage: ref(''), saveMessage: ref(''),
    loadingDownload: ref(false), task: ref<TaskResult | null>(finished()), selectedFormat: ref(''),
    saveHandle: ref<FilePickerHandle | null>(null), savingFile: ref(false), pollingTimer: ref<number>(), localVideoUrl: ref(''),
    prepareFormats: async () => true, suggestedExtension: () => 'mp4', refreshTaskReferences,
  }
  const download = useVideoDownload(context)
  t.after(() => { download.resetDownload(); globalThis.fetch = previousFetch; globalThis.window = previousWindow })
  return { context, download, timers, browser, storage }
}

test('overlapping finished polls and repeated terminal responses save once', async t => {
  const { context, download, timers } = setup(t, async () => { await new Promise(() => {}) })
  const response = deferred<Response>()
  let polls = 0
  let writers = 0
  context.task.value = running()
  context.saveHandle.value = { async createWritable() { writers++; return { async write() {}, async close() {} } } }
  globalThis.fetch = async input => {
    if (String(input).startsWith('/api/tasks/')) { polls++; return response.promise }
    return new Response('video')
  }
  const first = download.pollTask('first')
  await download.pollTask('first')
  assert.equal(polls, 1)
  response.resolve(Response.json(finished()))
  await first
  await download.pollTask('first')
  assert.equal(writers, 1)
  assert.equal(context.saveHandle.value, null)
  assert.equal(context.savingFile.value, false)
  assert.equal(download.fileSaved.value, true)
  assert.equal(timers.size, 0)
})

test('polling schedules the next request only after the current response', async t => {
  const { context, download, timers } = setup(t)
  context.task.value = running()
  const response = deferred<Response>()
  globalThis.fetch = async () => response.promise
  const start = download.startPolling('first')
  assert.equal(timers.size, 0)
  response.resolve(Response.json(running()))
  await start
  assert.equal(timers.size, 1)
  download.stopPolling()
  assert.equal(timers.size, 0)
})

test('replacing polling clears its old timer and ignores an old in-flight response', async t => {
  const { context, download, timers } = setup(t)
  const oldResponse = deferred<Response>()
  context.task.value = running()
  globalThis.fetch = async input => String(input).endsWith('/first') ? oldResponse.promise : Response.json(running('second'))
  const old = download.startPolling('first')
  context.task.value = null
  await download.startPolling('second')
  const [newTimer] = timers.keys()
  oldResponse.resolve(Response.json(finished()))
  await old
  assert.equal(context.task.value?.task_id, 'second')
  assert.deepEqual([...timers.keys()], [newTimer])
  context.task.value = null
  await download.startPolling('third')
  assert.equal(timers.has(newTimer), false)
})

test('a running task or save cannot submit another backend download', async t => {
  const { context, download } = setup(t)
  globalThis.fetch = async () => { assert.fail('duplicate backend download') }
  context.task.value = running()
  await download.startDownload()
  context.task.value = finished()
  context.savingFile.value = true
  await download.startDownload()
})

test('manual save has one writer and becomes saved only after close', async t => {
  const { context, download, storage } = setup(t)
  const closing = deferred<void>()
  let writers = 0
  globalThis.fetch = async () => new Response('video')
  const handle = { async createWritable() { writers++; return { async write() {}, close: () => closing.promise } } }
  const first = download.saveFinishedFile(handle)
  await settle()
  await download.saveFinishedFile(handle)
  assert.equal(writers, 1)
  assert.equal(context.savingFile.value, true)
  assert.equal(download.fileSaved.value, false)
  assert.match(download.saveProgress.value, /完成文件写入/)
  closing.resolve()
  await first
  assert.equal(context.savingFile.value, false)
  assert.equal(download.fileSaved.value, true)
  assert.equal(storage.get('videodream.video.saved.first'), '1')
  assert.match(context.saveMessage.value, /已保存/)
  context.task.value = finished('other')
  assert.equal(download.fileSaved.value, false)
  context.task.value = finished()
  assert.equal(download.fileSaved.value, true)
  const restored = useVideoDownload(context)
  assert.equal(restored.fileSaved.value, true)
})

test('unavailable browser storage does not turn a completed save into an error', async t => {
  const { context, download, browser } = setup(t)
  browser.localStorage.setItem = () => { throw new Error('storage unavailable') }
  browser.localStorage.getItem = () => { throw new Error('storage unavailable') }
  globalThis.fetch = async () => new Response('video')
  await download.saveFinishedFile({ async createWritable() { return { async write() {}, async close() {} } } })
  assert.equal(download.fileSaved.value, true)
  assert.equal(context.errorMessage.value, '')
})

for (const stage of ['write', 'close']) {
  test(`${stage} failure clears saving and supports retrying the existing file`, async t => {
    const { context, download } = setup(t)
    let aborted = 0
    globalThis.fetch = async () => new Response('video')
    await download.saveFinishedFile({ async createWritable() {
      return {
        async write() { if (stage === 'write') throw new Error('disk full') },
        async close() { if (stage === 'close') throw new Error('disk full') },
        async abort() { aborted++ },
      }
    } })
    assert.equal(aborted, 1)
    assert.equal(context.savingFile.value, false)
    assert.equal(download.fileSaved.value, false)
    assert.match(context.errorMessage.value, /disk full/)
    await download.saveFinishedFile({ async createWritable() { return { async write() {}, async close() {} } } })
    assert.equal(download.fileSaved.value, true)
  })
}

test('cancelling a stalled save clears saving without claiming success', async t => {
  const { context, download } = setup(t)
  let cancelled = false
  let aborted = false
  globalThis.fetch = async () => new Response(new ReadableStream({ cancel() { cancelled = true } }))
  const save = download.saveFinishedFile({ async createWritable() { return { async write() {}, async close() {}, async abort() { aborted = true } } } })
  await settle()
  download.cancelSave()
  await save
  assert.equal(cancelled, true)
  assert.equal(aborted, true)
  assert.equal(context.savingFile.value, false)
  assert.equal(download.fileSaved.value, false)
  assert.equal(context.errorMessage.value, '')
  assert.match(context.saveMessage.value, /已取消/)
})

test('a cancelled old save cannot change a new task or clear its saving indicator', async t => {
  const { context, download } = setup(t)
  const firstClose = deferred<void>()
  const secondClose = deferred<void>()
  globalThis.fetch = async () => new Response('video')
  const first = download.saveFinishedFile({ async createWritable() { return { async write() {}, close: () => firstClose.promise, async abort() {} } } })
  await settle()
  context.revisions.record++
  download.resetDownload()
  context.task.value = finished('second')
  const second = download.saveFinishedFile({ async createWritable() { return { async write() {}, close: () => secondClose.promise } } })
  await first
  await settle()
  firstClose.resolve()
  await settle()
  assert.equal(context.savingFile.value, true)
  assert.equal(download.fileSaved.value, false)
  assert.equal(context.saveMessage.value, '')
  secondClose.resolve()
  await second
  assert.equal(download.fileSaved.value, true)
  assert.equal(context.savingFile.value, false)
  context.task.value = finished()
  assert.equal(download.fileSaved.value, false)
})

test('cancelling the file picker leaves no saved or error state', async t => {
  const { context, download, browser } = setup(t)
  browser.showSaveFilePicker = async () => { throw new DOMException('cancelled', 'AbortError') }
  globalThis.fetch = async () => { assert.fail('cancelled picker must not fetch') }
  await download.saveFinishedFile()
  assert.equal(context.savingFile.value, false)
  assert.equal(download.fileSaved.value, false)
  assert.equal(context.errorMessage.value, '')
})

test('an existing finished file can be saved without its original parse formats', async t => {
  const { context, download, browser } = setup(t)
  context.parseResult.value = null
  context.task.value = { ...finished(), filename: '已完成音频.mp3' }
  let pickerOptions: any
  browser.showSaveFilePicker = async function (options?: any) {
    pickerOptions = options
    return { async createWritable() { return { async write() {}, async close() {} } } }
  }
  globalThis.fetch = async () => new Response('audio')
  await download.saveFinishedFile()
  assert.equal(pickerOptions.suggestedName, '已完成音频.mp3')
  assert.equal(pickerOptions.types[0].description, 'Audio file')
  assert.equal(download.fileSaved.value, true)
})

test('browser fallback reports download handoff without claiming a successful local save', async t => {
  const { context, download, storage } = setup(t)
  const previousDocument = globalThis.document
  let clicked = false
  const anchor = { href: '', download: '', click() { clicked = true }, remove() {} }
  globalThis.document = { body: { appendChild() {} }, createElement: () => anchor } as unknown as Document
  t.after(() => { globalThis.document = previousDocument; if (anchor.href) URL.revokeObjectURL(anchor.href) })
  globalThis.fetch = async () => new Response('video')
  await download.saveFinishedFile()
  assert.equal(clicked, true)
  assert.equal(anchor.download, 'first.mp4')
  assert.equal(download.fileSaved.value, false)
  assert.equal(storage.size, 0)
  assert.equal(context.savingFile.value, false)
  assert.match(context.saveMessage.value, /已交给浏览器/)
})
