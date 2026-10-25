import assert from 'node:assert/strict'
import test from 'node:test'
import { ListeningCoverage } from '../src/features/music/listeningCoverage.ts'

test('seeking to the end never credits the skipped range', () => {
  const c = new ListeningCoverage()
  c.sample(0, 0, true); c.sample(1000, 1000, true)
  c.discontinuity(); c.sample(179000, 2000, true); c.sample(180000, 3000, true)
  assert.equal(c.covered, 2000); assert.equal(c.listened, 2000)
})
test('replaying a fragment counts time but not duplicate coverage', () => {
  const c = new ListeningCoverage()
  for (let n = 0; n < 2; n++) {
    c.discontinuity(); c.sample(0, n * 2000, true); c.sample(1000, n * 2000 + 1000, true)
  }
  assert.equal(c.listened, 2000); assert.equal(c.covered, 1000)
})
test('pause buffering and long scheduler gaps are unknown, not listening', () => {
  const c = new ListeningCoverage()
  c.sample(0, 0, true); c.sample(1000, 1000, false); c.sample(1000, 2000, true)
  c.sample(61000, 62000, true)
  assert.equal(c.listened, 0)
})
