import MindElixir, { type MindElixirInstance, type Theme } from 'mind-elixir'
import { toMindElixirData, type MindMapResult, type MindMapDisplayOptions } from './mindMap'
import { serializeMindMapSvg, type MindMapSvgBox, type MindMapSvgScene, type MindMapSvgText } from './mindMapSvg'

export type MindMapImageFormat = 'svg' | 'png'

function captureMindMapScene(nodes: HTMLElement, title: string): MindMapSvgScene {
  const bounds = nodes.getBoundingClientRect()
  const scale = bounds.width / Number.parseFloat(getComputedStyle(nodes).width)
  if (!Number.isFinite(scale) || scale <= 0) throw new Error('Mind map layout has no size.')
  const canvas = document.createElement('canvas')
  const context = canvas.getContext('2d')
  if (!context) throw new Error('Text measurement is unavailable.')
  const point = (x: number, y: number) => ({ x: (x - bounds.left) / scale, y: (y - bounds.top) / scale })
  const box = (element: Element): MindMapSvgBox | null => {
    const style = getComputedStyle(element)
    const fill = style.backgroundColor
    const strokeWidth = Number.parseFloat(style.borderTopWidth) || 0
    if ((fill === 'rgba(0, 0, 0, 0)' || fill === 'transparent') && !strokeWidth) return null
    const rect = element.getBoundingClientRect()
    // SVG strokes straddle the rectangle; CSS borders are inside its border box.
    const inset = strokeWidth / 2
    const origin = point(rect.left, rect.top)
    return { x: origin.x + inset, y: origin.y + inset,
      width: Math.max(0, rect.width / scale - strokeWidth), height: Math.max(0, rect.height / scale - strokeWidth),
      radius: Math.max(0, (Number.parseFloat(style.borderTopLeftRadius) || 0) - inset),
      fill, stroke: strokeWidth ? style.borderTopColor : 'none', strokeWidth }
  }
  const text = (element: Element): MindMapSvgText => {
    const style = getComputedStyle(element)
    const fontSize = Number.parseFloat(style.fontSize)
    context.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
    const metrics = context.measureText('Hg国')
    const ascent = Number.isFinite(metrics.fontBoundingBoxAscent) ? metrics.fontBoundingBoxAscent : fontSize
    const lines: MindMapSvgText['lines'] = []
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
    const range = document.createRange()
    let node: Node | null
    let lineTop = Number.NEGATIVE_INFINITY
    while ((node = walker.nextNode())) {
      let offset = 0
      for (const character of node.textContent || '') {
        range.setStart(node, offset)
        offset += character.length
        range.setEnd(node, offset)
        const rect = range.getBoundingClientRect()
        if (!rect.height || character === '\n' || character === '\r') continue
        const origin = point(rect.left, rect.top)
        let line = lines[lines.length - 1]
        if (!line || Math.abs(rect.top - lineTop) > fontSize * scale / 2) {
          line = { text: '', x: origin.x, y: origin.y + ascent, width: 0 }
          lines.push(line)
          lineTop = rect.top
        }
        line.text += character
        line.width = Math.max(line.width, (rect.right - bounds.left) / scale - line.x)
      }
    }
    return { fontFamily: style.fontFamily, fontSize, fontWeight: style.fontWeight,
      fontStyle: style.fontStyle, fill: style.color, lines }
  }
  return {
    title, width: Math.ceil(bounds.width / scale), height: Math.ceil(bounds.height / scale),
    paths: Array.from(nodes.querySelectorAll<SVGPathElement>('.lines path, .subLines path')).map(path => {
      const matrix = path.getScreenCTM()
      if (!matrix) throw new Error('Mind map connection has no coordinates.')
      const style = getComputedStyle(path)
      return { d: path.getAttribute('d') || '', stroke: style.stroke,
        strokeWidth: Number.parseFloat(style.strokeWidth),
        matrix: [matrix.a / scale, matrix.b / scale, matrix.c / scale, matrix.d / scale,
          (matrix.e - bounds.left) / scale, (matrix.f - bounds.top) / scale] }
    }),
    nodes: Array.from(nodes.querySelectorAll<HTMLElement>('me-tpc')).map(topic => {
      const elements = Array.from(topic.querySelectorAll('.video-mind-root, .video-mind-label, .video-mind-time, .video-mind-node p'))
      if (!elements.length) elements.push(topic.querySelector('.text') || topic)
      return { id: topic.dataset.nodeid || '',
        boxes: [topic, ...topic.querySelectorAll('.video-mind-time')].map(box).filter((item): item is MindMapSvgBox => item !== null),
        texts: elements.map(text) }
    }),
  }
}

async function mindMapPng(svg: Blob, width: number, height: number): Promise<Blob> {
  const canvas = document.createElement('canvas')
  canvas.width = Math.ceil(width * 2)
  canvas.height = Math.ceil(height * 2)
  const context = canvas.getContext('2d')
  if (!context) throw new Error('PNG export is unavailable.')
  const url = URL.createObjectURL(svg)
  try {
    const image = new Image()
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve()
      image.onerror = () => reject(new Error('Could not render the mind map image.'))
      image.src = url
    })
    context.fillStyle = '#ffffff'
    context.fillRect(0, 0, canvas.width, canvas.height)
    context.drawImage(image, 0, 0, canvas.width, canvas.height)
    return await new Promise<Blob>((resolve, reject) => canvas.toBlob(
      blob => blob?.size ? resolve(blob) : reject(new Error('PNG export is empty.')), 'image/png'))
  } finally {
    URL.revokeObjectURL(url)
    canvas.width = canvas.height = 0
  }
}

export async function createMindMapImage(result: MindMapResult, theme: Theme, format: MindMapImageFormat,
  display: MindMapDisplayOptions = { mode: 'detail', depth: 4 }): Promise<Blob> {
  const data = toMindElixirData(result, display)
  const host = document.createElement('div')
  host.className = `video-mind-map-canvas video-mind-map-export ${display.mode === 'overview' ? 'is-overview' : ''}`
  host.setAttribute('aria-hidden', 'true')
  host.inert = true
  document.body.appendChild(host)
  let map: MindElixirInstance | undefined
  try {
    await document.fonts.ready
    // A separate map exports either the current overview or the complete tree.
    map = new MindElixir({ el: host, direction: MindElixir.SIDE, editable: false,
      contextMenu: false, toolBar: false, keypress: false, allowUndo: false,
      compact: false, alignment: 'root', theme })
    map.init(data)
    await document.fonts.ready
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    const scene = captureMindMapScene(map.nodes, result.title)
    const svg = new Blob([serializeMindMapSvg(scene)], { type: 'image/svg+xml;charset=utf-8' })
    return format === 'svg' ? svg : await mindMapPng(svg, scene.width, scene.height)
  } finally {
    map?.destroy()
    host.remove()
  }
}

export function mindMapImageFilename(title: string, format: MindMapImageFormat): string {
  const name = title.normalize('NFC').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 120).replace(/[. ]+$/g, '').trim()
  return `${name || 'mind-map'}.${format}`
}
