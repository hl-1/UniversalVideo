type Interval = [number, number]
export class ListeningCoverage {
  intervals: Interval[] = []
  listened = 0
  lastPosition: number | null = null
  lastTime: number | null = null
  sample(position: number, now: number, playing: boolean) {
    if (playing && this.lastPosition !== null && this.lastTime !== null) {
      const wall = now - this.lastTime, delta = position - this.lastPosition
      if (wall > 0 && wall <= 2500 && delta > 0 && delta <= wall * 1.5 + 100) {
        this.listened += Math.min(wall, delta)
        this.intervals.push([this.lastPosition, position])
        this.intervals.sort((a, b) => a[0] - b[0])
        const merged: Interval[] = []
        for (const item of this.intervals) {
          const previous = merged.at(-1)
          if (previous && item[0] <= previous[1]) previous[1] = Math.max(previous[1], item[1])
          else merged.push([...item])
        }
        this.intervals = merged
      }
    }
    this.lastPosition = playing ? position : null
    this.lastTime = playing ? now : null
  }
  discontinuity() { this.lastPosition = null; this.lastTime = null }
  get covered() { return Math.min(this.listened, this.intervals.reduce((sum, [a, b]) => sum + b - a, 0)) }
}
