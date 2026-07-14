import assert from 'node:assert/strict'
import test from 'node:test'

import { canSeekMedia, clampSeekTime, filterMindMapTask, mindMapPanDelta, toMindElixirData } from './mindMap.ts'

test('filters recursive mind map data and limits depth', () => {
  const payload = {
    task_id: 'map123',
    status: 'finished',
    progress: 100,
    message: 'done',
    result: {
      title: '学习地图',
      generated_at: '2026-07-13T00:00:00Z',
      nodes: [
        {
          id: 'node-1',
          title: '根节点',
          summary: '说明',
          timestamp: 15,
          segment_ids: [1],
          children: [
            {
              id: 'node-2',
              title: '第二层',
              summary: '',
              timestamp: 20,
              segment_ids: [2],
              children: [{ id: 'bad', title: '', timestamp: -1, segment_ids: [], children: [] }],
            },
          ],
        },
      ],
    },
  }

  const result = filterMindMapTask(payload)

  assert.equal(result?.status, 'finished')
  assert.equal(result?.result?.nodes[0]?.children[0]?.children.length, 0)
})

test('rejects invalid task unions and malformed finished results', () => {
  assert.equal(filterMindMapTask({ task_id: 'x', status: 'unknown', progress: 0, message: '' }), null)
  assert.equal(filterMindMapTask({ task_id: 'x', status: 'finished', progress: 100, message: '', result: { nodes: [] } }), null)
})

test('clamps timestamp seeking to loaded media duration', () => {
  assert.equal(clampSeekTime(30, 120), 30)
  assert.equal(clampSeekTime(-5, 120), 0)
  assert.equal(clampSeekTime(999, 120), 120)
  assert.equal(clampSeekTime(Number.NaN, 120), 0)
})

test('limits every mind map level to ten nodes', () => {
  const nodes = Array.from({ length: 20 }, (_, index) => ({
    id: `node-${index}`,
    title: `节点 ${index}`,
    summary: '',
    timestamp: index,
    segment_ids: [index],
    children: Array.from({ length: 20 }, (_, child) => ({
      id: `node-${index}-${child}`,
      title: `子节点 ${child}`,
      summary: '',
      timestamp: child,
      segment_ids: [child],
      children: [],
    })),
  }))
  const result = filterMindMapTask({
    task_id: 'map123',
    status: 'finished',
    progress: 100,
    message: '',
    result: { title: '地图', generated_at: '', nodes },
  })

  assert.equal(result?.result?.nodes.length, 10)
  assert.equal(result?.result?.nodes[0]?.children.length, 10)
})

test('keeps valid nodes that follow malformed siblings', () => {
  const nodes = [
    ...Array.from({ length: 10 }, () => null),
    { id: 'valid', title: '有效节点', summary: '', timestamp: 1, segment_ids: [1], children: [] },
  ]
  const result = filterMindMapTask({
    task_id: 'map123',
    status: 'finished',
    progress: 100,
    message: '',
    result: { title: '地图', generated_at: '', nodes },
  })

  assert.equal(result?.result?.nodes[0]?.id, 'valid')
})

test('seeks only after media metadata is available', () => {
  assert.equal(canSeekMedia({ readyState: 0, duration: Number.NaN }), false)
  assert.equal(canSeekMedia({ readyState: 1, duration: 120 }), true)
})

test('converts result into a right-facing colored mind map', () => {
  const data = toMindElixirData({
    title: 'AI 视频总结',
    generated_at: '',
    nodes: [
      {
        id: 'node-1',
        title: '核心 <结论>',
        summary: '不能信任 <script>',
        timestamp: 138,
        segment_ids: [1],
        children: [
          {
            id: 'node-2',
            title: '子主题',
            summary: '',
            timestamp: 166,
            segment_ids: [2],
            children: [
              { id: 'node-3', title: '深层主题', summary: '', timestamp: 188, segment_ids: [3], children: [] },
            ],
          },
        ],
      },
    ],
  })

  assert.equal(data.nodeData.topic, 'AI 视频总结')
  assert.equal(data.nodeData.children?.[0]?.direction, 1)
  assert.equal(data.nodeData.children?.[0]?.branchColor, '#2563eb')
  assert.equal(data.nodeData.children?.[0]?.children?.[0]?.branchColor, '#2563eb')
  assert.deepEqual(data.nodeData.children?.[0]?.metadata, { timestamp: 138 })
  assert.equal(data.nodeData.children?.[0]?.children?.[0]?.children?.[0]?.expanded, true)
  assert.match(data.nodeData.children?.[0]?.dangerouslySetInnerHTML || '', /02:18/)
  assert.doesNotMatch(data.nodeData.children?.[0]?.dangerouslySetInnerHTML || '', /<script>/)
  assert.match(data.nodeData.children?.[0]?.dangerouslySetInnerHTML || '', /&lt;结论&gt;/)
})

test('maps horizontal slider movement to inverse canvas movement', () => {
  assert.equal(mindMapPanDelta(0, 25, 800), -200)
  assert.equal(mindMapPanDelta(75, 50, 800), 200)
  assert.equal(mindMapPanDelta(0, 100, -1), 0)
})
