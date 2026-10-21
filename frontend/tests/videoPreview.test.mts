import assert from 'node:assert/strict'
import test from 'node:test'

import { downloadedPreviewUrl, monitorPreview, readPreviewUrl, readPreviewStatusUrl, selectPreviewFormat, waitForPreview } from '../src/features/video/videoPreview.ts'

const formats = [
  { format_id: 'sniff:mp4-1', label: 'MP4 · 线路 1', ext: 'mp4' },
  { format_id: 'hls-1', label: 'HLS · 线路 2', ext: 'm3u8' },
  { format_id: 'sniff:mp4-2', label: 'MP4 · 线路 3', ext: 'MP4' },
]

test('downloaded previews require a finished browser container and local file URL', () => {
  const file = { status: 'finished', filename: 'saved.mp4', download_url: '/api/files/saved.mp4' }
  assert.equal(downloadedPreviewUrl(file), file.download_url)
  assert.equal(downloadedPreviewUrl({ ...file, filename: 'saved.WEBM' }), file.download_url)
  for (const patch of [{ status: 'running' }, { filename: 'saved.mkv' }, { filename: 'sound.m4a' },
    { download_url: 'https://cdn.example.com/saved.mp4' }, { download_url: '//example.com/video.mp4' }]) {
    assert.equal(downloadedPreviewUrl({ ...file, ...patch }), null)
  }
  assert.equal(downloadedPreviewUrl(null), null)
})

test('selectPreviewFormat keeps the selected MP4 format', () => {
  assert.equal(selectPreviewFormat(formats, 'sniff:mp4-2')?.format_id, 'sniff:mp4-2')
})

test('selectPreviewFormat supports the selected HLS format', () => {
  assert.equal(selectPreviewFormat(formats, 'hls-1')?.format_id, 'hls-1')
})

test('selectPreviewFormat supports an HLS only source', () => {
  assert.deepEqual(
    selectPreviewFormat([{ format_id: 'hls-1', label: 'HLS · 线路', ext: 'm3u8' }], 'hls-1'),
    { format_id: 'hls-1', label: 'HLS · 线路', ext: 'm3u8' },
  )
})

test('readPreviewUrl accepts a local preview content URL', () => {
  assert.equal(
    readPreviewUrl({ preview_url: '/api/previews/abc_123/content', expires_in: 600 }),
    '/api/previews/abc_123/content',
  )
})

test('readPreviewUrl rejects malformed or external values', () => {
  assert.equal(readPreviewUrl({ preview_url: 'https://example.com/video.mp4' }), null)
  assert.equal(readPreviewUrl({ preview_url: 123 }), null)
  assert.equal(readPreviewUrl(null), null)
})

test('preview prefers a complete pair while leaving the download selection alone', () => {
  const pair = { format_id: 'sniffpair:video|audio', ext: 'mp4' }
  assert.equal(selectPreviewFormat([...formats, pair], 'sniff:mp4-1'), pair)
  assert.equal(selectPreviewFormat([...formats, pair], pair.format_id), pair)
})

test('ordinary yt-dlp IDs and containers use the shared preview', () => {
  for (const ext of ['mp4', 'webm', 'flv', 'mkv', 'mpd']) {
    assert.equal(selectPreviewFormat([{ format_id: '137+bestaudio/best', ext }], '137+bestaudio/best')?.ext, ext)
  }
})

test('audio only formats do not become video preview candidates', () => {
  assert.equal(selectPreviewFormat([{ format_id: 'audio', ext: 'webm', has_video: false }], 'audio'), null)
  assert.equal(selectPreviewFormat([{ format_id: 'audio', ext: 'm4a' }], 'audio'), null)
  assert.equal(selectPreviewFormat([{ format_id: 'music:123', ext: 'mp4' }], 'music:123'), null)
  assert.equal(selectPreviewFormat(formats, 'unavailable')?.format_id, 'sniff:mp4-1')
})

test('preparation waits for ready and preserves the audio result', async () => {
  const statusUrl = '/api/previews/test-token'
  let polls = 0
  const result = await waitForPreview({ status: 'preparing', status_url: statusUrl }, async url => {
    assert.equal(url, statusUrl)
    polls++
    return { status: 'ready', preview_url: `${statusUrl}/content`, has_audio: true }
  }, new AbortController().signal, () => {}, 0)
  assert.equal(polls, 1)
  assert.equal(result.has_audio, true)
})

test('preparation propagates failure without accepting a video URL', async () => {
  await assert.rejects(waitForPreview({ status: 'failed', message: '配套音频失效' }, async () => ({}), new AbortController().signal, () => {}), /配套音频失效/)
})

test('cancelling preparation prevents further polling', async () => {
  const controller = new AbortController()
  let polls = 0
  const pending = waitForPreview({ status: 'preparing', status_url: '/api/previews/test-token' }, async () => { polls++; return {} }, controller.signal, () => {}, 1000)
  controller.abort()
  await assert.rejects(pending, { name: 'AbortError' })
  assert.equal(polls, 0)
})

test('preparation refuses foreign status URLs and mismatched content tokens', async () => {
  assert.equal(readPreviewStatusUrl({ status_url: 'https://example.com/api/previews/a' }), null)
  await assert.rejects(waitForPreview({ status: 'ready', status_url: '/api/previews/one', preview_url: '/api/previews/two/content' }, async () => ({}), new AbortController().signal, () => {}), /无效地址/)
})

test('fragmented playback stops status polling when the file completes', async () => {
  let polls = 0
  await monitorPreview({ status: 'ready', status_url: '/api/previews/stream', delivery: 'fragmented', complete: false }, async () => {
    polls++
    return { status: 'ready', delivery: 'fragmented', complete: true, preview_url: '/api/previews/stream/content' }
  }, new AbortController().signal, 0)
  assert.equal(polls, 1)
})

test('fragmented playback reports a later producer failure', async () => {
  await assert.rejects(monitorPreview({ status_url: '/api/previews/stream', delivery: 'fragmented', complete: false }, async () => (
    { status: 'failed', message: '视频源连接中断' }
  ), new AbortController().signal, 0), /视频源连接中断/)
})

test('cancellation stops fragmented playback status polling', async () => {
  const controller = new AbortController()
  let polls = 0
  const pending = monitorPreview({ status_url: '/api/previews/stream', delivery: 'fragmented', complete: false }, async () => {
    polls++
    return {}
  }, controller.signal, 1000)
  controller.abort()
  await assert.rejects(pending, { name: 'AbortError' })
  assert.equal(polls, 0)
})
