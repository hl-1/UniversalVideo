export type TimingStage = {
  id: string; label: string; parent_id?: string | null; status: string
  started_at_ms: number; finished_at_ms: number | null; elapsed_ms: number
  attempts: number; retry_wait_ms: number; completed_units: number; total_units: number; reused: boolean
}
export type ExecutionTiming = {
  version: number; accepted_at_ms: number; server_now_ms: number; finished_at_ms: number | null
  elapsed_ms: number; stages: TimingStage[]
}
export type ModelInfo = { model: string; service?: string | null }
const number = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)

export function filterTiming(value: unknown): ExecutionTiming | null {
  if (!record(value) || value.version !== 1 || !number(value.accepted_at_ms) || !number(value.server_now_ms)
    || !number(value.elapsed_ms) || !(value.finished_at_ms === null || number(value.finished_at_ms)) || !Array.isArray(value.stages)) return null
  const stages: TimingStage[] = []
  for (const item of value.stages) {
    if (!record(item) || typeof item.id !== 'string' || typeof item.label !== 'string' || !number(item.started_at_ms)
      || !number(item.elapsed_ms) || !(item.finished_at_ms === null || number(item.finished_at_ms))
      || !['running', 'finished', 'failed', 'cancelled'].includes(String(item.status))) continue
    stages.push({ id: item.id, label: item.label, parent_id: typeof item.parent_id === 'string' ? item.parent_id : null,
      status: String(item.status), started_at_ms: item.started_at_ms, finished_at_ms: item.finished_at_ms,
      elapsed_ms: item.elapsed_ms, attempts: number(item.attempts) ? item.attempts : 0,
      retry_wait_ms: number(item.retry_wait_ms) ? item.retry_wait_ms : 0,
      completed_units: number(item.completed_units) ? item.completed_units : 0,
      total_units: number(item.total_units) ? item.total_units : 0, reused: item.reused === true })
  }
  return { version: 1, accepted_at_ms: value.accepted_at_ms, server_now_ms: value.server_now_ms,
    finished_at_ms: value.finished_at_ms, elapsed_ms: value.elapsed_ms, stages }
}

export function elapsedAt(timing: ExecutionTiming, sinceReceived = 0) {
  return timing.elapsed_ms + (timing.finished_at_ms === null ? Math.max(0, sinceReceived) : 0)
}

export function stageElapsedAt(stage: TimingStage, sinceReceived = 0): number {
  return stage.elapsed_ms + (stage.status === 'running' ? Math.max(0, sinceReceived) : 0)
}

export function stageDuration(timing: ExecutionTiming | null, id: string, sinceReceived = 0): number | null {
  const stage = timing?.stages.find(item => item.id === id)
  return stage ? stageElapsedAt(stage, sinceReceived) : null
}

export function formatElapsed(milliseconds: number | null | undefined): string {
  if (milliseconds == null || !Number.isFinite(milliseconds)) return '未记录'
  const seconds = Math.max(0, milliseconds) / 1000
  return seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${Math.floor(seconds / 60)} 分 ${(seconds % 60).toFixed(1)} 秒`
}

export function generationTotal(summary: ExecutionTiming | null, map: ExecutionTiming | null, paired: boolean, summaryDelta = 0, mapDelta = summaryDelta): number | null {
  if (!paired) return map ? elapsedAt(map, mapDelta) : summary ? elapsedAt(summary, summaryDelta) : null
  if (!summary || !map) return null
  // End timestamps are reconstructed from monotonic elapsed values, not from the client's wall clock.
  return Math.max(summary.accepted_at_ms + elapsedAt(summary, summaryDelta), map.accepted_at_ms + elapsedAt(map, mapDelta))
    - Math.min(summary.accepted_at_ms, map.accepted_at_ms)
}
