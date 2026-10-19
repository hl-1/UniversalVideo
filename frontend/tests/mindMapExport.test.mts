import assert from 'node:assert/strict'
import test from 'node:test'
import { serializeMindMapSvg, type MindMapSvgScene } from '../src/features/video/mindMapSvg.ts'

function scene(): MindMapSvgScene {
  return {
    title: 'Python概述（Python到底强在哪？）', width: 1829, height: 2132,
    paths: [{ d: 'M 0 10 C 30 10 40 20 60 20', matrix: [1, 0, 0, 1, 700, 200], stroke: '#f59e0b', strokeWidth: 2 }],
    nodes: [{ id: 'deep-node', boxes: [{ x: 740, y: 220, width: 40, height: 18, radius: 4,
      fill: '#eff6ff', stroke: 'none', strokeWidth: 0 }], texts: [{
      fontFamily: 'Microsoft YaHei, sans-serif', fontSize: 14, fontWeight: '800', fontStyle: 'normal', fill: '#334155',
      lines: [{ text: '深层节点：Web开发', x: 1100, y: 240, width: 230 },
        { text: '中文 & <特殊> "字符"', x: 1100, y: 260, width: 200 }],
    }] }],
  }
}

test('exports positioned SVG text and paths without an HTML layout dependency', () => {
  const svg = serializeMindMapSvg(scene())
  assert.match(svg, /viewBox="0 0 1829 2132"/)
  assert.match(svg, /<rect width="1829" height="2132" fill="#ffffff"/)
  assert.match(svg, /transform="matrix\(1 0 0 1 700 200\)"/)
  assert.match(svg, /<tspan x="1100" y="240" textLength="230" lengthAdjust="spacingAndGlyphs">深层节点：Web开发<\/tspan>/)
  assert.match(svg, /x="1100" y="260"/)
  assert.doesNotMatch(svg, /foreignObject|<style|<div|zoom|inline-block/)
  assert.ok(svg.indexOf('<path') < svg.indexOf('data-node-id='))
})

test('escapes text, titles and attributes and removes invalid XML control characters', () => {
  const input = scene()
  input.title = '<script> & "标题"\u0000'
  input.nodes[0]!.id = '" onload="bad'
  const svg = serializeMindMapSvg(input)
  assert.match(svg, /<title>&lt;script&gt; &amp; &quot;标题&quot;<\/title>/)
  assert.match(svg, /中文 &amp; &lt;特殊&gt; &quot;字符&quot;/)
  assert.match(svg, /data-node-id="&quot; onload=&quot;bad"/)
  assert.doesNotMatch(svg, /<script>|\u0000/)
})

test('keeps all 29 nodes and their wrapped descriptions in the drawing', () => {
  const input = scene()
  input.nodes = Array.from({ length: 29 }, (_, index) => ({
    ...input.nodes[0]!, id: `node-${index}`,
    texts: [{ ...input.nodes[0]!.texts[0]!, lines: [
      { text: `标题${index}`, x: 1100, y: index * 50 + 20, width: 200 },
      { text: `说明${index}第一行`, x: 1100, y: index * 50 + 35, width: 200 },
      { text: `说明${index}第二行`, x: 1100, y: index * 50 + 50, width: 200 },
    ] }],
  }))
  const svg = serializeMindMapSvg(input)
  assert.equal((svg.match(/data-node-id=/g) || []).length, 29)
  assert.equal((svg.match(/<tspan /g) || []).length, 87)
  for (let index = 0; index < 29; index++) {
    assert.ok(svg.includes(`>标题${index}</tspan>`))
    assert.ok(svg.includes(`>说明${index}第二行</tspan>`))
  }
})

test('rejects empty or invalid dimensions and invalid geometry', () => {
  assert.throws(() => serializeMindMapSvg({ ...scene(), width: 0 }))
  assert.throws(() => serializeMindMapSvg({ ...scene(), height: Number.POSITIVE_INFINITY }))
  const input = scene()
  input.paths[0]!.matrix[4] = Number.NaN
  assert.throws(() => serializeMindMapSvg(input))
})
