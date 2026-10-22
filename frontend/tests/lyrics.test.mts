import assert from 'node:assert/strict'
import { test } from 'node:test'
import { activeLyricIndex, audioFilename, parseImportedLrc } from '../src/features/music/lyrics.ts'

test('lyric selection follows seeks, exact timestamps, gaps and the end', () => {
  const lines = [{ time: 1, text: 'a' }, { time: 3, text: 'b' }, { time: 9, text: 'c' }]
  assert.equal(activeLyricIndex([], 1), -1)
  assert.equal(activeLyricIndex(lines, 0), -1)
  assert.equal(activeLyricIndex(lines, 3), 1)
  assert.equal(activeLyricIndex(lines, 8), 1)
  assert.equal(activeLyricIndex(lines, 20), 2)
  assert.equal(activeLyricIndex(lines, 1), 0)
})

test('offline LRC import retains offsets, multiple timestamps and translated lines', () => {
  assert.deepEqual(parseImportedLrc('[offset:100]\n[00:01.23][00:03.45]Hello\n[00:01.23]你好').lines,
    [{ time: 1.33, text: 'Hello\n你好' }, { time: 3.55, text: 'Hello' }])
  assert.throws(() => parseImportedLrc('[ar:Artist]\ninvalid'), /有效/)
})

test('downloaded audio names are decoded without treating remote streams as local files', () => {
  assert.equal(audioFilename('/api/files/a.%E6%99%B4%E5%A4%A9.m4a'), 'a.晴天.m4a')
  assert.equal(audioFilename('https://example.com/song.mp3'), '')
  assert.equal(audioFilename('/api/music/playback/token/content'), '')
  assert.equal(audioFilename('/api/files/%bad.m4a'), '')
})
