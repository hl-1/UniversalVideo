import assert from 'node:assert/strict'
import { test } from 'node:test'
import { activeLyricIndex, audioFilename } from './lyrics.ts'

test('lyric selection follows seeks, exact timestamps, gaps and the end', () => {
  const lines = [{ time: 1, text: 'a' }, { time: 3, text: 'b' }, { time: 9, text: 'c' }]
  assert.equal(activeLyricIndex([], 1), -1)
  assert.equal(activeLyricIndex(lines, 0), -1)
  assert.equal(activeLyricIndex(lines, 3), 1)
  assert.equal(activeLyricIndex(lines, 8), 1)
  assert.equal(activeLyricIndex(lines, 20), 2)
  assert.equal(activeLyricIndex(lines, 1), 0)
})

test('downloaded audio names are decoded without treating remote streams as local files', () => {
  assert.equal(audioFilename('/api/files/a.%E6%99%B4%E5%A4%A9.m4a'), 'a.晴天.m4a')
  assert.equal(audioFilename('https://example.com/song.mp3'), '')
  assert.equal(audioFilename('/api/music/playback/token/content'), '')
  assert.equal(audioFilename('/api/files/%bad.m4a'), '')
})
