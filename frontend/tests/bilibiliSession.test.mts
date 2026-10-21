import assert from 'node:assert/strict'
import test from 'node:test'
import { createRenderer, ref, computed } from 'vue'
import { useBilibiliSession } from '../src/features/video/useBilibiliSession.ts'
import { useVideoSummary } from '../src/features/video/useVideoSummary.ts'

const flush = () => new Promise(resolve => setImmediate(resolve))
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function mounted<T>(setup: () => T) {
  let value: T
  const renderer = createRenderer({
    createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null,
    insert() {}, remove() {}, patchProp() {},
  })
  const app = renderer.createApp({ setup() { value = setup(); return () => null } })
  app.mount({})
  return { value: value!, unmount: () => app.unmount() }
}

test('cancel during QR creation discards the late QR and never resumes generation', async t => {
  let release: (value: Response) => void
  const calls: string[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push(`${options?.method || 'GET'} ${url}`)
    if (options?.method === 'POST') return new Promise<Response>(resolve => { release = resolve })
    return response({ cancelled: true })
  })
  const { value: session, unmount } = mounted(useBilibiliSession)
  t.after(unmount)
  const choice = session.requestBilibiliLogin()
  const creating = session.refreshBilibiliQr()
  session.closeBilibiliLogin()
  release!(response({ login_id: 'late', status: 'waiting_scan', qr_image: 'data:image/png;base64,test' }))
  await creating
  await flush()
  assert.equal(await choice, 'cancel')
  assert.equal(session.bilibiliDialog.value, false)
  assert.equal(session.bilibiliQr.value, null)
  assert.ok(calls.includes('DELETE /api/videos/bilibili/qr/late'))
})

test('confirmation resumes the waiting choice and displays only the bound identity', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 10000 })
  t.mock.method(globalThis, 'fetch', async (_url, options) => options?.method === 'POST'
    ? response({ login_id: 'qr-1', status: 'waiting_scan', expires_at: 180 })
    : response({ login_id: 'qr-1', status: 'confirmed', bound: true, nickname: '测试用户' }))
  const { value: session, unmount } = mounted(useBilibiliSession)
  t.after(unmount)
  const choice = session.requestBilibiliLogin()
  await session.refreshBilibiliQr()
  t.mock.timers.tick(3000)
  await flush()
  assert.equal(await choice, 'confirmed')
  assert.deepEqual(session.bilibiliStatus.value, { bound: true, nickname: '测试用户' })
  assert.equal(session.bilibiliDialog.value, false)
  assert.equal(session.bilibiliQr.value, null)
})

test('expired QR stops polling and can be refreshed without settling the generation choice', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 10000 })
  let posts = 0
  const fetch = t.mock.method(globalThis, 'fetch', async (_url, options) => {
    if (options?.method === 'POST') return response({ login_id: `qr-${++posts}`, status: 'waiting_scan', expires_at: 11 })
    return response({ cancelled: true })
  })
  const { value: session, unmount } = mounted(useBilibiliSession)
  t.after(unmount)
  const choice = session.requestBilibiliLogin()
  await session.refreshBilibiliQr()
  t.mock.timers.tick(3000)
  await flush()
  assert.equal(session.bilibiliQr.value?.status, 'expired')
  assert.equal(fetch.mock.callCount(), 1)
  await session.refreshBilibiliQr()
  assert.equal(session.bilibiliQr.value?.login_id, 'qr-2')
  session.closeBilibiliLogin('speech')
  assert.equal(await choice, 'speech')
})

test('account reset discards a late bound-status response', async t => {
  let release: (value: Response) => void
  t.mock.method(globalThis, 'fetch', () => new Promise<Response>(resolve => { release = resolve }))
  const { value: session, unmount } = mounted(useBilibiliSession)
  t.after(unmount)
  const pending = session.refreshBilibiliStatus()
  session.resetBilibiliSession()
  release!(response({ bound: true, nickname: '之前的账号' }))
  await pending
  assert.equal(session.bilibiliStatus.value.bound, false)
})

function summaryContext(login: () => Promise<'confirmed' | 'speech' | 'cancel'>,
  confirmSpeech: () => Promise<'speech' | 'cancel'> = async () => 'cancel') {
  const summaryTask = ref(null)
  return {
    url: ref('https://www.bilibili.com/video/BV1tDsgzxECr/?p=9'), parseResult: ref(null), activeRecordId: ref('record-9'),
    revisions: { record: 1, summary: 0, map: 0 }, errorMessage: ref(''), saveMessage: ref(''),
    loadingSummary: ref(false), loadingMindMap: ref(false), generateMindMap: ref(true), statusFetchMs: ref(null),
    summaryTask, mindMapTask: ref(null), previousMindMap: ref(null), currentMapView: ref(null), summaryTab: ref('summary'),
    summaryTone: computed(() => summaryTask.value?.status || 'idle'), renderedMindMap: computed(() => null),
    summaryPollingTimer: ref(undefined), mindMapPollingTimer: ref(undefined),
    refreshTaskReferences: async () => {}, scheduleVideoView: () => {}, requestBilibiliLogin: login,
    requestBilibiliSpeechConfirmation: confirmSpeech,
  } as Parameters<typeof useVideoSummary>[0]
}

test('login retry preserves the video, record and original map selection', async t => {
  const bodies: any[] = []
  let context: ReturnType<typeof summaryContext>
  context = summaryContext(async () => { context.generateMindMap.value = false; return 'confirmed' })
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (options?.method === 'POST') {
      bodies.push(JSON.parse(String(options.body)))
      return bodies.length === 1
        ? response({ detail: { code: 'bilibili_login_required', message: '需要登录' } }, 409)
        : response({ task_id: 'summary-1' })
    }
    return response({ task_id: 'summary-1', status: 'finished', progress: 100 })
  })
  await useVideoSummary(context).startSummary()
  assert.equal(bodies.length, 2)
  assert.deepEqual(bodies[0], bodies[1])
  assert.equal(bodies[1].generate_mind_map, true)
  assert.equal(bodies[1].parse_record_id, 'record-9')
  assert.equal(context.loadingSummary.value, false)
})

test('continue speech submits an explicit policy and cancellation submits no second task', async t => {
  for (const choice of ['speech', 'cancel'] as const) {
    const bodies: any[] = []
    t.mock.method(globalThis, 'fetch', async (_url, options) => {
      if (options?.method !== 'POST') return response({ task_id: 'summary-1', status: 'finished', progress: 100 })
      bodies.push(JSON.parse(String(options.body)))
      return bodies.length === 1 ? response({ detail: { code: 'bilibili_login_required' } }, 409) : response({ task_id: 'summary-1' })
    })
    const context = summaryContext(async () => choice)
    await useVideoSummary(context).startSummary()
    assert.equal(bodies.length, choice === 'speech' ? 2 : 1)
    if (choice === 'speech') assert.equal(bodies[1].bilibili_subtitle_policy, 'speech')
    assert.equal(context.loadingSummary.value, false)
  }
})

test('record changes during login prevent a stale retry', async t => {
  const context = summaryContext(async () => { context.revisions.record++; return 'confirmed' })
  const fetch = t.mock.method(globalThis, 'fetch', async () => response({ detail: { code: 'bilibili_login_required' } }, 409))
  await useVideoSummary(context).startSummary()
  assert.equal(fetch.mock.callCount(), 1)
})

test('speech confirmation preserves binding and never creates a QR', async t => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => response({}))
  const { value: session, unmount } = mounted(useBilibiliSession)
  t.after(unmount)
  session.bilibiliStatus.value = { bound: true, nickname: '已连接用户' }
  const choice = session.requestBilibiliSpeechConfirmation()
  assert.equal(session.bilibiliDialog.value, true)
  assert.equal(session.bilibiliSpeechOnly.value, true)
  assert.equal(session.bilibiliStatus.value.bound, true)
  assert.equal(session.bilibiliQr.value, null)
  session.closeBilibiliLogin('speech')
  assert.equal(await choice, 'speech')
  assert.equal(fetch.mock.callCount(), 0)
  const cancelled = session.requestBilibiliSpeechConfirmation()
  session.resetBilibiliSession()
  assert.equal(await cancelled, 'cancel')
})

test('no-subtitle confirmation starts speech only after consent, preserving original parameters', async t => {
  for (const choice of ['speech', 'cancel'] as const) {
    const bodies: any[] = []
    let prompts = 0
    const context = summaryContext(async () => { throw Error('不应要求再次登录') }, async () => {
      prompts++
      context.generateMindMap.value = false
      return choice
    })
    t.mock.method(globalThis, 'fetch', async (_url, options) => {
      if (options?.method !== 'POST') return response({ task_id: 'summary-1', status: 'finished', progress: 100 })
      bodies.push(JSON.parse(String(options.body)))
      return bodies.length === 1 ? response({ detail: { code: 'bilibili_speech_confirmation_required' } }, 409)
        : response({ task_id: 'summary-1' })
    })
    await useVideoSummary(context).startSummary()
    assert.equal(prompts, 1)
    assert.equal(bodies.length, choice === 'speech' ? 2 : 1)
    if (choice === 'speech') {
      assert.deepEqual(bodies[1], { ...bodies[0], bilibili_subtitle_policy: 'speech' })
      assert.equal(bodies[1].generate_mind_map, true)
    }
    assert.equal(context.loadingSummary.value, false)
  }
})

test('login success with no subtitles prompts for speech before a third submission', async t => {
  const bodies: any[] = []
  const prompts: string[] = []
  const context = summaryContext(async () => { prompts.push('login'); return 'confirmed' },
    async () => { prompts.push('speech'); return 'speech' })
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    if (options?.method !== 'POST') return response({ task_id: 'summary-1', status: 'finished', progress: 100 })
    bodies.push(JSON.parse(String(options.body)))
    return bodies.length < 3 ? response({ detail: { code: bodies.length === 1 ? 'bilibili_login_required' : 'bilibili_speech_confirmation_required' } }, 409)
      : response({ task_id: 'summary-1' })
  })
  await useVideoSummary(context).startSummary()
  assert.deepEqual(prompts, ['login', 'speech'])
  assert.deepEqual(bodies[0], bodies[1])
  assert.deepEqual(bodies[2], { ...bodies[0], bilibili_subtitle_policy: 'speech' })
})

test('record changes during speech confirmation prevent stale task creation', async t => {
  const context = summaryContext(async () => 'cancel', async () => { context.revisions.record++; return 'speech' })
  const fetch = t.mock.method(globalThis, 'fetch', async () => response({ detail: { code: 'bilibili_speech_confirmation_required' } }, 409))
  await useVideoSummary(context).startSummary()
  assert.equal(fetch.mock.callCount(), 1)
})

test('platform failures show an error without offering login or automatic speech', async t => {
  let prompts = 0
  const context = summaryContext(async () => { prompts++; return 'speech' }, async () => { prompts++; return 'speech' })
  t.mock.method(globalThis, 'fetch', async () => response({ detail: { code: 'bilibili_subtitle_unavailable', message: '字幕检查失败' } }, 502))
  await useVideoSummary(context).startSummary()
  assert.equal(prompts, 0)
  assert.equal(context.errorMessage.value, '字幕检查失败')
  assert.equal(context.loadingSummary.value, false)
})
