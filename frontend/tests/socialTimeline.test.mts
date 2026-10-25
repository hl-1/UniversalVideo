import assert from 'node:assert/strict'
import test from 'node:test'
import { desiredPosition } from '../src/features/social/timeline.ts'

test('a late join uses the server timeline and the saved seek position', () => {
  assert.equal(desiredPosition({ state: 'playing', position_ms: 12500, started_at: 100 }, 108), 20.5)
})

test('a scheduled start does not advance audio before the shared start time', () => {
  assert.equal(desiredPosition({ state: 'playing', position_ms: 5000, started_at: 102 }, 100), 5)
  assert.equal(desiredPosition({ state: 'playing', position_ms: 5000, started_at: 102 }, 103), 6)
})

test('paused state stays fixed across reconnect and missing start times', () => {
  assert.equal(desiredPosition({ state: 'paused', position_ms: 9000, started_at: 100 }, 1000), 9)
  assert.equal(desiredPosition({ state: 'playing', position_ms: 9000, started_at: null }, 1000), 9)
})
