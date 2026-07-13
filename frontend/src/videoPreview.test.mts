import assert from 'node:assert/strict'
import test from 'node:test'

import { readPreviewUrl, selectPreviewFormat } from './videoPreview.ts'

const formats = [
  { format_id: 'mp4-1', label: 'MP4 · 线路 1', ext: 'mp4' },
  { format_id: 'hls-1', label: 'HLS · 线路 2', ext: 'm3u8' },
  { format_id: 'mp4-2', label: 'MP4 · 线路 3', ext: 'MP4' },
]

test('selectPreviewFormat keeps the selected MP4 format', () => {
  assert.equal(selectPreviewFormat(formats, 'mp4-2')?.format_id, 'mp4-2')
})

test('selectPreviewFormat falls back to the first MP4 format', () => {
  assert.equal(selectPreviewFormat(formats, 'hls-1')?.format_id, 'mp4-1')
})

test('selectPreviewFormat returns null when MP4 is unavailable', () => {
  assert.equal(
    selectPreviewFormat([{ format_id: 'hls-1', label: 'HLS · 线路', ext: 'm3u8' }], 'hls-1'),
    null,
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
