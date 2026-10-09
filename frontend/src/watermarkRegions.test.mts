import assert from 'node:assert/strict'
import test from 'node:test'
import { clampRegion, regionFromPoints } from './watermarkRegions.ts'

test('reverse dragging on portrait video preserves the selected pixel bounds', () => {
  assert.deepEqual(regionFromPoints({ x: 300, y: 700 }, { x: 100, y: 600 }, 720, 1280), { x: 100, y: 600, width: 200, height: 100 })
})

test('selection outside the frame stays inside the interpolation border', () => {
  assert.deepEqual(regionFromPoints({ x: -20, y: -50 }, { x: 2000, y: 1500 }, 1920, 1080), { x: 1, y: 1, width: 1918, height: 1078 })
})

test('clicks and tiny selections do not create a processing region', () => {
  assert.equal(regionFromPoints({ x: 20, y: 30 }, { x: 22, y: 33 }, 320, 180), null)
})

test('numeric edits fit a region within the video after resizing', () => {
  assert.deepEqual(clampRegion({ x: 310, y: 175, width: 50, height: 20 }, 320, 180), { x: 269, y: 159, width: 50, height: 20 })
})
