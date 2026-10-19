export type MindMapLayout = 'auto' | 'balanced' | 'right' | 'left'
export type ResolvedMindMapLayout = Exclude<MindMapLayout, 'auto'>
type Rect = { left: number; top: number; width: number; height: number }

export function mindMapFitScale(layout: ResolvedMindMapLayout, bounds: Rect, root: Rect,
  viewport: { width: number; height: number }, scale: number, padding: number): number {
  const centerX = root.left + root.width / 2
  const centerY = root.top + root.height / 2
  const width = layout === 'balanced'
    ? 2 * Math.max(centerX - bounds.left, bounds.left + bounds.width - centerX) : bounds.width
  const height = layout === 'balanced'
    ? 2 * Math.max(centerY - bounds.top, bounds.top + bounds.height - centerY) : bounds.height
  return Math.max(0, Math.min((viewport.width - padding * 2) * scale / Math.max(1, width),
    (viewport.height - padding * 2) * scale / Math.max(1, height)))
}

export function chooseMindMapLayout(balancedScale: number, rightScale: number): ResolvedMindMapLayout {
  // Prefer the familiar two-sided map when the layouts offer similar readable space.
  return rightScale > balancedScale * 1.08 ? 'right' : 'balanced'
}
