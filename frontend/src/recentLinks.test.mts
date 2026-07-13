import assert from 'node:assert/strict'
import { test } from 'node:test'

import { getRecentLinks, saveRecentLink } from './recentLinks.ts'

class MemoryStorage {
  private values = new Map<string, string>()

  getItem(key: string) {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string) {
    this.values.set(key, value)
  }
}

test('saveRecentLink trims links and ignores empty input', () => {
  const storage = new MemoryStorage()

  assert.deepEqual(saveRecentLink(storage, '   '), [])
  assert.deepEqual(saveRecentLink(storage, ' https://example.com/video '), ['https://example.com/video'])
})

test('saveRecentLink moves duplicate links to the front', () => {
  const storage = new MemoryStorage()

  saveRecentLink(storage, 'https://example.com/one')
  saveRecentLink(storage, 'https://example.com/two')

  assert.deepEqual(saveRecentLink(storage, 'https://example.com/one'), [
    'https://example.com/one',
    'https://example.com/two',
  ])
})

test('getRecentLinks returns at most five valid recent links', () => {
  const storage = new MemoryStorage()

  for (const item of ['one', 'two', 'three', 'four', 'five', 'six']) {
    saveRecentLink(storage, `https://example.com/${item}`)
  }

  assert.deepEqual(getRecentLinks(storage), [
    'https://example.com/six',
    'https://example.com/five',
    'https://example.com/four',
    'https://example.com/three',
    'https://example.com/two',
  ])
})
