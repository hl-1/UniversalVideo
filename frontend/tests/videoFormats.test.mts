import assert from 'node:assert/strict'
import test from 'node:test'
import { matchRefreshedFormat, refreshDownloadFormats } from '../src/features/video/videoFormats.ts'
import type { ParseResult } from '../src/features/video/types.ts'

const current: ParseResult = {
  record_id: 'original-record', title: '原始标题', thumbnail: '/api/thumbnails/original.jpg',
  uploader: '作者', duration: 90, webpage_url: 'https://example.com/video',
  formats: [
    { format_id: '360', label: 'MP4', ext: 'mp4', resolution: '640x360' },
    { format_id: '720', label: 'MP4', ext: 'mp4', resolution: '1280x720' },
  ],
}

test('download refresh keeps metadata and exact selection despite duplicate labels and reordering', () => {
  const fresh: ParseResult = { title: '缺失信息的刷新结果', thumbnail: null, formats: [...current.formats].reverse() }
  const refreshed = refreshDownloadFormats(current, fresh, '720')
  assert.deepEqual(refreshed.result, { ...current, formats: fresh.formats })
  assert.equal(refreshed.selectedId, '720')
  assert.equal(current.formats[0]?.format_id, '360')
})

test('automatic download selection survives a refresh including an empty format list', () => {
  for (const formats of [current.formats, []]) {
    assert.equal(refreshDownloadFormats(current, { title: '', formats }, '').selectedId, '')
  }
})

test('saved and expired signed IDs map to the same quality without depending on list position', () => {
  const fresh = current.formats.map(item => ({ ...item, format_id: `fresh:${item.format_id}`, label: `新的 ${item.label}` })).reverse()
  for (const format_id of ['saved:1', 'old-signed-id']) {
    assert.equal(matchRefreshedFormat(fresh, { ...current.formats[1]!, format_id })?.format_id, 'fresh:720')
  }
})

test('matching excludes different containers, audio-only streams, resolutions and sniff kinds', () => {
  const selected = { ...current.formats[1]!, format_id: 'sniff:old', has_video: true }
  const wrong = [
    { ...selected, format_id: 'sniff:webm', ext: 'webm' },
    { ...selected, format_id: 'sniff:audio', has_video: false },
    { ...selected, format_id: 'sniff:360', resolution: '640x360' },
    { ...selected, format_id: 'sniffpair:pair' },
  ]
  assert.equal(matchRefreshedFormat(wrong, selected), null)
  assert.equal(matchRefreshedFormat([...wrong, { ...selected, format_id: 'sniff:new', ext: 'MP4' }], selected)?.format_id, 'sniff:new')
})

test('an unavailable or ambiguous explicit choice never silently downloads the first format', () => {
  const saved = { ...current, formats: [{ ...current.formats[1]!, format_id: 'saved:1' }] }
  for (const formats of [[current.formats[0]!], [
    { ...current.formats[1]!, format_id: 'one' }, { ...current.formats[1]!, format_id: 'two' },
  ]]) {
    assert.throws(() => refreshDownloadFormats(saved, { title: '', formats }, 'saved:1'), /原先选择的下载格式已失效/)
    assert.equal(saved.thumbnail, current.thumbnail)
    assert.equal(saved.formats[0]?.format_id, 'saved:1')
  }
})
