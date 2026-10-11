import MindElixir, { type MindElixirInstance, type Theme } from 'mind-elixir'
import { toMindElixirData, type MindMapResult } from './mindMap'

export type MindMapImageFormat = 'svg' | 'png'

export async function createMindMapImage(result: MindMapResult, theme: Theme, format: MindMapImageFormat): Promise<Blob> {
  const data = toMindElixirData(result)
  const { snapdom } = await import('@zumer/snapdom')
  const host = document.createElement('div')
  host.className = 'video-mind-map-canvas video-mind-map-export'
  host.setAttribute('aria-hidden', 'true')
  host.inert = true
  document.body.appendChild(host)
  let map: MindElixirInstance | undefined
  try {
    await document.fonts.ready
    // A separate, fully expanded map keeps exports independent of the saved view.
    map = new MindElixir({ el: host, direction: MindElixir.RIGHT, editable: false,
      contextMenu: false, toolBar: false, keypress: false, allowUndo: false,
      compact: false, alignment: 'root', theme })
    map.init(data)
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    const capture = await snapdom(map.nodes, { backgroundColor: '#ffffff', dpr: 1,
      fast: false, reconcile: true, exclude: 'me-epd, .linkcontroller' })
    const blob = await capture.toBlob({ format, scale: format === 'png' ? 2 : 1, dpr: 1 })
    if (!blob.size) throw new Error('Image export is empty.')
    return blob
  } finally {
    map?.destroy()
    host.remove()
  }
}

export function mindMapImageFilename(title: string, format: MindMapImageFormat): string {
  const name = title.normalize('NFC').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 120).replace(/[. ]+$/g, '').trim()
  return `${name || 'mind-map'}.${format}`
}
