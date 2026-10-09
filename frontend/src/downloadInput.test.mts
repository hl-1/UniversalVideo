import assert from 'node:assert/strict'
import { test } from 'node:test'
import { extractHttpUrl } from './downloadInput.ts'

test('extracts a Douyin link from pasted share text', () => {
  assert.equal(extractHttpUrl('7.43 pda:/ video https://v.douyin.com/AbC123/ copy'), 'https://v.douyin.com/AbC123/')
})
test('preserves music fragments and strips share punctuation', () => {
  assert.equal(extractHttpUrl('https://music.163.com/#/song?id=123\u3002'), 'https://music.163.com/#/song?id=123')
})
test('rejects song names, unsupported protocols and malformed URLs', () => {
  for (const value of ['a song', 'file:///secret', 'https://']) assert.equal(extractHttpUrl(value), null)
})
