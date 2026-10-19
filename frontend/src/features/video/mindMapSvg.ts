export type MindMapSvgBox = {
  x: number
  y: number
  width: number
  height: number
  radius: number
  fill: string
  stroke: string
  strokeWidth: number
}

export type MindMapSvgLine = { text: string; x: number; y: number; width: number }

export type MindMapSvgText = {
  fontFamily: string
  fontSize: number
  fontWeight: string
  fontStyle: string
  fill: string
  lines: MindMapSvgLine[]
}

export type MindMapSvgPath = {
  d: string
  matrix: [number, number, number, number, number, number]
  stroke: string
  strokeWidth: number
}

export type MindMapSvgScene = {
  title: string
  width: number
  height: number
  paths: MindMapSvgPath[]
  nodes: { id: string; boxes: MindMapSvgBox[]; texts: MindMapSvgText[] }[]
}

function escapeXml(value: string): string {
  return value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffe\uffff]/g, '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&apos;')
}

function number(value: number): string {
  if (!Number.isFinite(value)) throw new Error('Invalid mind map image coordinates.')
  return String(Math.round(value * 1000) / 1000)
}

// Only SVG drawing primitives leave the browser; no HTML or CSS layout is embedded.
export function serializeMindMapSvg(scene: MindMapSvgScene): string {
  if (scene.width <= 0 || scene.height <= 0) throw new Error('Mind map image has no size.')
  const width = number(scene.width)
  const height = number(scene.height)
  const paths = scene.paths.map(path =>
    `<path d="${escapeXml(path.d)}" transform="matrix(${path.matrix.map(number).join(' ')})" fill="none" stroke="${escapeXml(path.stroke)}" stroke-width="${number(path.strokeWidth)}"/>`,
  ).join('')
  const nodes = scene.nodes.map(node => {
    const boxes = node.boxes.map(box =>
      `<rect x="${number(box.x)}" y="${number(box.y)}" width="${number(box.width)}" height="${number(box.height)}" rx="${number(box.radius)}" fill="${escapeXml(box.fill)}" stroke="${escapeXml(box.stroke)}" stroke-width="${number(box.strokeWidth)}"/>`,
    ).join('')
    const texts = node.texts.map(text => {
      const lines = text.lines.map(line =>
        `<tspan x="${number(line.x)}" y="${number(line.y)}"${line.width > 0 ? ` textLength="${number(line.width)}" lengthAdjust="spacingAndGlyphs"` : ''}>${escapeXml(line.text)}</tspan>`,
      ).join('')
      return `<text font-family="${escapeXml(text.fontFamily)}" font-size="${number(text.fontSize)}" font-weight="${escapeXml(text.fontWeight)}" font-style="${escapeXml(text.fontStyle)}" fill="${escapeXml(text.fill)}" xml:space="preserve">${lines}</text>`
    }).join('')
    return `<g data-node-id="${escapeXml(node.id)}">${boxes}${texts}</g>`
  }).join('')
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><title>${escapeXml(scene.title)}</title><rect width="${width}" height="${height}" fill="#ffffff"/>${paths}${nodes}</svg>`
}
