import test from 'node:test'
import assert from 'node:assert/strict'
import { parseEvents, terminal } from '../src/features/agents/tasks.ts'

test('SSE parser preserves an incomplete event across network chunks', () => {
  const first = parseEvents('id: 41\nevent: status\ndata: {"status":"running"}\n\nid: 42\ndata: {"sta')
  assert.equal(first.events[0]?.id, 41)
  assert.equal(first.events[0]?.type, 'status')
  const second = parseEvents(first.rest + 'tus":"succeeded"}\n\n')
  assert.deepEqual(second.events[0]?.data, { status: 'succeeded' })
  assert.equal(second.events[0]?.id, 42)
})
test('SSE comments, invalid payloads and IDs do not advance replay cursor', () => {
  assert.deepEqual(parseEvents(': keepalive\n\nid: NaN\ndata: {}\n\nid: 3\ndata: malformed\n\n').events, [])
})
test('CRLF SSE framing is supported', () => {
  assert.equal(parseEvents('id: 7\r\ndata: {}\r\n\r\n').events[0]?.id, 7)
})
test('Human waits remain resumable states', () => {
  assert.equal(terminal('waiting_approval'), false)
  assert.equal(terminal('succeeded'), true)
  assert.equal(terminal('cancelled'), true)
})
