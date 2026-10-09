export type WatermarkRegion = { x: number; y: number; width: number; height: number }
export type Point = { x: number; y: number }

export function clampRegion(region: WatermarkRegion, width: number, height: number): WatermarkRegion {
  const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, Math.round(value)))
  const w = clamp(region.width, 4, width - 2)
  const h = clamp(region.height, 4, height - 2)
  return { x: clamp(region.x, 1, width - w - 1), y: clamp(region.y, 1, height - h - 1), width: w, height: h }
}

export function regionFromPoints(start: Point, end: Point, width: number, height: number): WatermarkRegion | null {
  const x1 = Math.max(1, Math.min(width - 1, Math.round(Math.min(start.x, end.x))))
  const y1 = Math.max(1, Math.min(height - 1, Math.round(Math.min(start.y, end.y))))
  const x2 = Math.max(1, Math.min(width - 1, Math.round(Math.max(start.x, end.x))))
  const y2 = Math.max(1, Math.min(height - 1, Math.round(Math.max(start.y, end.y))))
  if (x2 - x1 < 4 || y2 - y1 < 4) return null
  return { x: x1, y: y1, width: x2 - x1, height: y2 - y1 }
}
