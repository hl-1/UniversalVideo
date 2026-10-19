import type { ComputedRef, Ref } from 'vue'
import { extractHttpUrl } from '../../shared/utils/downloadInput'
import { readApiError } from '../../shared/api/client'
import { filterMindMapTask, type MindMapTask } from './mindMap'
import type { MapView } from './videoHistory'
import type { ParseResult, SummaryTaskResult, SummaryTab } from './types'

type SummaryContext = {
  url: Ref<string>
  parseResult: Ref<ParseResult | null>
  activeRecordId: Ref<string>
  revisions: { record: number; summary: number; map: number }
  errorMessage: Ref<string>
  saveMessage: Ref<string>
  loadingSummary: Ref<boolean>
  loadingMindMap: Ref<boolean>
  generateMindMap: Ref<boolean>
  statusFetchMs: Ref<number | null>
  summaryTask: Ref<SummaryTaskResult | null>
  mindMapTask: Ref<MindMapTask | null>
  previousMindMap: Ref<MindMapTask | null>
  currentMapView: Ref<MapView | null>
  summaryTab: Ref<SummaryTab>
  summaryTone: ComputedRef<string>
  renderedMindMap: ComputedRef<MindMapTask | null>
  summaryPollingTimer: Ref<number | undefined>
  mindMapPollingTimer: Ref<number | undefined>
  refreshTaskReferences: () => Promise<void>
  scheduleVideoView: () => void
}

export function useVideoSummary(context: SummaryContext) {
  const {
    url,
    parseResult,
    activeRecordId,
    revisions,
    errorMessage,
    saveMessage,
    loadingSummary,
    loadingMindMap,
    generateMindMap,
    statusFetchMs,
    summaryTask,
    mindMapTask,
    previousMindMap,
    currentMapView,
    summaryTab,
    summaryTone,
    renderedMindMap,
    summaryPollingTimer,
    mindMapPollingTimer,
    refreshTaskReferences,
    scheduleVideoView,
  } = context
  let summaryPolling = false
  let mapPolling = false

  function stopSummaryPolling() {
    if (summaryPollingTimer.value) {
      window.clearInterval(summaryPollingTimer.value)
      summaryPollingTimer.value = undefined
    }
  }

  function stopMindMapPolling() {
    if (mindMapPollingTimer.value) {
      window.clearInterval(mindMapPollingTimer.value)
      mindMapPollingTimer.value = undefined
    }
  }

  async function startSummary() {
    const target = parseResult.value?.webpage_url || extractHttpUrl(url.value)
    if (!target || loadingSummary.value) return
    const revision = revisions.record
    const selection = ++revisions.summary
    revisions.map++
    const recordId = activeRecordId.value
    errorMessage.value = ''
    saveMessage.value = ''
    loadingSummary.value = true
    summaryTask.value = null
    mindMapTask.value = null
    previousMindMap.value = null; currentMapView.value = null
    summaryTab.value = 'summary'
    stopSummaryPolling()
    stopMindMapPolling()

    try {
      const response = await fetch('/api/summaries', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: target, parse_record_id: recordId || undefined, generate_mind_map: generateMindMap.value }),
      })
      if (!response.ok) throw new Error(await readApiError(response))
      const data: { task_id: string; mind_map_task_id?: string } = await response.json()
      if (revision !== revisions.record || selection !== revisions.summary) return
      await pollSummary(data.task_id)
      if (revision !== revisions.record || selection !== revisions.summary) return
      if (['pending', 'running'].includes(summaryTone.value)) summaryPollingTimer.value = window.setInterval(() => pollSummary(data.task_id), 1400)
      if (data.mind_map_task_id) {
        await pollMindMap(data.task_id, data.mind_map_task_id)
        if (revision !== revisions.record || selection !== revisions.summary) return
        if (['pending', 'running'].includes((mindMapTask.value as MindMapTask | null)?.status || '')) {
          mindMapPollingTimer.value = window.setInterval(() => pollMindMap(data.task_id, data.mind_map_task_id), 1400)
        }
      }
      await refreshTaskReferences()
      if (revision === revisions.record && selection === revisions.summary) scheduleVideoView()
    } catch (error) {
      if (revision === revisions.record && selection === revisions.summary) errorMessage.value = error instanceof Error ? error.message : '总结任务创建失败。'
    } finally {
      if (revision === revisions.record && selection === revisions.summary) loadingSummary.value = false
    }
  }

  async function pollSummary(taskId: string) {
    if (summaryPolling) return
    summaryPolling = true
    const revision = revisions.record
    const selection = revisions.summary
    const started = performance.now()
    try {
      const response = await fetch(`/api/summaries/${taskId}`)
      if (!response.ok) throw new Error(await readApiError(response))
      const data = await response.json()
      if (revision !== revisions.record || selection !== revisions.summary || (summaryTask.value && summaryTask.value.task_id !== taskId)) return
      summaryTask.value = data
      statusFetchMs.value = performance.now() - started
      if (['finished', 'failed', 'cancelled'].includes(summaryTask.value?.status || '')) {
        stopSummaryPolling()
        await refreshTaskReferences()
      }
    } catch (error) {
      if (revision !== revisions.record || selection !== revisions.summary || (summaryTask.value && summaryTask.value.task_id !== taskId)) return
      stopSummaryPolling()
      errorMessage.value = error instanceof Error ? error.message : '总结任务状态查询失败。'
    } finally {
      summaryPolling = false
    }
  }

  async function startMindMap(regenerate = false) {
    const summaryId = summaryTask.value?.task_id
    if (!summaryId || summaryTask.value?.status !== 'finished' || loadingMindMap.value) return
    const revision = revisions.record
    const selection = ++revisions.map
    loadingMindMap.value = true
    errorMessage.value = ''
    stopMindMapPolling()
    previousMindMap.value = renderedMindMap.value
    try {
      const suffix = regenerate ? '?regenerate=true' : ''
      const response = await fetch(`/api/summaries/${summaryId}/mind-map${suffix}`, { method: 'POST' })
      if (!response.ok) throw new Error(await readApiError(response))
      const created = await response.json()
      if (revision !== revisions.record || selection !== revisions.map || summaryTask.value?.task_id !== summaryId) return
      await pollMindMap(summaryId, created.task_id)
      if (revision !== revisions.record || selection !== revisions.map || summaryTask.value?.task_id !== summaryId) return
      if (['pending', 'running'].includes(mindMapTask.value?.status || '')) {
        mindMapPollingTimer.value = window.setInterval(() => pollMindMap(summaryId, created.task_id), 1400)
      }
      await refreshTaskReferences()
      if (revision === revisions.record && selection === revisions.map) scheduleVideoView()
    } catch (error) {
      if (revision === revisions.record && selection === revisions.map) errorMessage.value = error instanceof Error ? error.message : '思维导图任务创建失败。'
    } finally {
      if (revision === revisions.record && selection === revisions.map) loadingMindMap.value = false
    }
  }

  async function pollMindMap(summaryId: string, mapId?: string) {
    if (mapPolling) return
    mapPolling = true
    const revision = revisions.record
    const selection = revisions.map
    const started = performance.now()
    try {
      const response = await fetch(`/api/summaries/${summaryId}/mind-map${mapId ? `?map_id=${mapId}` : ''}`)
      if (!response.ok) throw new Error(await readApiError(response))
      const filtered = filterMindMapTask(await response.json())
      if (!filtered) throw new Error('思维导图接口返回了无效数据。')
      if (revision !== revisions.record || selection !== revisions.map || summaryTask.value?.task_id !== summaryId) return
      if (filtered.result && renderedMindMap.value && renderedMindMap.value.task_id !== filtered.task_id) currentMapView.value = null
      mindMapTask.value = filtered
      statusFetchMs.value = performance.now() - started
      if (filtered.result) previousMindMap.value = null
      if (['finished', 'failed', 'cancelled'].includes(filtered.status)) {
        stopMindMapPolling()
        await refreshTaskReferences()
        scheduleVideoView()
      }
    } catch (error) {
      if (revision !== revisions.record || selection !== revisions.map || summaryTask.value?.task_id !== summaryId) return
      stopMindMapPolling()
      errorMessage.value = error instanceof Error ? error.message : '思维导图状态查询失败。'
    } finally {
      mapPolling = false
    }
  }

  return { stopSummaryPolling, stopMindMapPolling, startSummary, pollSummary, startMindMap, pollMindMap }
}
