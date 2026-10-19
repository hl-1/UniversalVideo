import assert from 'node:assert/strict'
import test from 'node:test'
import { elapsedAt, filterTiming, formatElapsed, generationTotal, stageDuration } from '../src/features/video/executionTiming.ts'
import { filterMindMapTask } from '../src/features/video/mindMap.ts'

const timing = (start: number, elapsed: number, finished = true) => ({ version: 1, accepted_at_ms: start,
  elapsed_ms: elapsed, server_now_ms: start + elapsed, finished_at_ms: finished ? start + elapsed : null, stages: [] })

test('parallel total counts wall duration and does not sum child durations', () => {
  assert.equal(generationTotal(timing(1000, 23000), timing(1002, 26998), true), 27000)
})
test('regenerating a map excludes days of user idle time', () => {
  assert.equal(generationTotal(timing(1000, 100), timing(86400000, 200), false), 200)
})
test('live clocks extrapolate monotonically and terminal clocks stay frozen', () => {
  assert.equal(elapsedAt(timing(1000, 500, false), 200), 700)
  assert.equal(elapsedAt(timing(1000, 500, false), -100), 500)
  assert.equal(elapsedAt(timing(1000, 500), 5000), 500)
})

test('summary duration excludes source preparation, persistence and overlapping child work', () => {
  const execution = { ...timing(1000, 84700), stages: [
    { id: 'source', label: '字幕准备', parent_id: null, status: 'finished', started_at_ms: 1100,
      finished_at_ms: 2500, elapsed_ms: 1400, attempts: 0, retry_wait_ms: 0, completed_units: 0, total_units: 0, reused: false },
    { id: 'summary_model', label: '总结', parent_id: null, status: 'finished', started_at_ms: 2500,
      finished_at_ms: 85600, elapsed_ms: 83100, attempts: 1, retry_wait_ms: 0, completed_units: 0, total_units: 0, reused: false },
  ] }
  assert.equal(stageDuration(execution, 'summary_model', 5000), 83100)
  assert.equal(stageDuration(execution, 'source'), 1400)
  assert.equal(stageDuration(execution, 'map_model'), null)
  assert.equal(stageDuration(null, 'summary_model'), null)
  execution.stages[1].status = 'running'
  assert.equal(stageDuration(execution, 'summary_model', 300), 83400)
  assert.equal(stageDuration(execution, 'summary_model', -300), 83100)
})

test('parallel tasks advance from their own polling receipt times', () => {
  const summary = timing(1000, 500, false)
  const map = timing(1100, 350, false)
  assert.equal(generationTotal(summary, map, true, 1000, 50), 1500)
  assert.equal(generationTotal(summary, map, true, 50, 1000), 1450)
  assert.equal(generationTotal(timing(1000, 800), map, true, 1000, 50), 800)
})
test('legacy and corrupt timing data stays unavailable', () => {
  assert.equal(filterTiming(undefined), null)
  assert.equal(filterTiming({ ...timing(0, 1), elapsed_ms: NaN }), null)
  assert.equal(generationTotal(timing(1000, 100), null, true), null)
  assert.equal(formatElapsed(null), '未记录')
})
test('mind map runtime filter preserves timing and actual model metadata', () => {
  const task = filterMindMapTask({ task_id: 'map', status: 'running', timing: timing(1, 2, false), model_info: { model: 'actual-model', api_key: 'must-not-copy' } })
  assert.equal(task?.timing?.elapsed_ms, 2)
  assert.deepEqual(task?.model_info, { model: 'actual-model', service: null })
})
