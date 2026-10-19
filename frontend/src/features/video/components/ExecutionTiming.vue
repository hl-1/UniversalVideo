<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Clock3, ChevronDown, Check, LoaderCircle, CircleX, CircleSlash } from 'lucide-vue-next'
import { elapsedAt, filterTiming, formatElapsed, generationTotal, stageDuration, stageElapsedAt, type ExecutionTiming, type TimingStage } from '../executionTiming'
import type { SummaryTaskResult } from '../types'
import type { MindMapTask } from '../mindMap'

const props = defineProps<{ summary: SummaryTaskResult; map?: MindMapTask | null; renderMs?: number | null; fetchMs?: number | null }>()
const expanded = ref(false)
const now = ref(performance.now())
const summaryReceived = ref(now.value)
const mapReceived = ref(now.value)
watch(() => props.summary, () => { summaryReceived.value = performance.now() })
watch(() => props.map, () => { mapReceived.value = performance.now() })
const ticker = window.setInterval(() => { now.value = performance.now() }, 500)
onBeforeUnmount(() => clearInterval(ticker))
const summaryDelta = computed(() => Math.max(0, now.value - summaryReceived.value))
const mapDelta = computed(() => Math.max(0, now.value - mapReceived.value))
const summaryTiming = computed(() => filterTiming(props.summary.timing))
const mapTiming = computed(() => filterTiming(props.map?.timing))
const paired = computed(() => !!props.summary.mind_map_task_id && props.summary.mind_map_task_id === props.map?.task_id)
const waitingMap = computed(() => !!props.summary.mind_map_task_id && !props.map)
const mapOnly = computed(() => !!props.map && !paired.value)
const pending = computed(() => ['pending', 'running'].includes(mapOnly.value ? props.map!.status : props.summary.status)
  || paired.value && ['pending', 'running'].includes(props.map!.status))
const total = computed(() => waitingMap.value ? null : generationTotal(summaryTiming.value, mapTiming.value, paired.value, summaryDelta.value, mapDelta.value))
const scope = computed(() => mapOnly.value ? '本次仅生成思维导图' : props.summary.mind_map_task_id ? '本次生成总结与思维导图' : '本次仅生成 AI 总结')
const taskStates = computed(() => mapOnly.value ? [props.map!.status] : [props.summary.status, ...(paired.value ? [props.map!.status] : [])])
const taskState = computed(() => pending.value || waitingMap.value ? 'running' : taskStates.value.includes('failed')
  ? paired.value && taskStates.value.includes('finished') ? 'partial' : 'failed' : taskStates.value.includes('cancelled') ? 'cancelled' : 'finished')
const stateLabels: Record<string, string> = { pending: '等待执行', running: '执行中', finished: '已完成', failed: '执行失败', cancelled: '已取消', partial: '部分完成' }
const status = computed(() => stateLabels[taskState.value])

function metric(timing: ExecutionTiming | null, id: string, taskStatus: string, delta: number) {
  const stage = timing?.stages.find(item => item.id === id)
  const duration = stageDuration(timing, id, delta)
  return {
    value: duration !== null ? formatElapsed(duration) : !timing ? '未记录' : ['pending', 'running'].includes(taskStatus) ? '尚未开始' : '未执行',
    state: stage ? stateLabels[stage.status] : '',
    status: stage?.status || '',
  }
}
const metrics = computed(() => [
  { id: 'summary', label: 'AI 总结耗时', ...metric(summaryTiming.value, 'summary_model', props.summary.status, summaryDelta.value),
    note: mapOnly.value ? '所选总结的生成用时，未计入本次总耗时' : 'AI 总结阶段，含接口等待与重试' },
  { id: 'map', label: '思维导图耗时',
    ...(props.map ? metric(mapTiming.value, 'map_model', props.map.status, mapDelta.value) : { value: waitingMap.value ? '正在获取' : '未生成', state: '', status: '' }),
    note: '导图生成阶段，含分块处理与重试' },
  { id: 'source', label: '字幕 / 转写准备耗时', ...metric(summaryTiming.value, 'source', props.summary.status, summaryDelta.value),
    note: mapOnly.value ? '来自所选总结，本次直接复用字幕' : '字幕获取，或音轨下载、转换与转写' },
])

const groups = computed(() => [
  { id: 'summary', label: '总结流程', timing: summaryTiming.value, taskStatus: props.summary.status, delta: summaryDelta.value, previous: mapOnly.value },
  ...(props.map ? [{ id: 'map', label: '思维导图流程', timing: mapTiming.value, taskStatus: props.map.status, delta: mapDelta.value, previous: false }] : []),
].map(group => ({ ...group, stages: (group.timing?.stages || [])
  .filter(stage => expanded.value || !stage.parent_id && stage.id !== 'waiting_source')
  .map(stage => ({ ...stage, progress: stageProgress(stage), detail: stageDetail(stage) })) })))
function stageProgress(stage: TimingStage): number | undefined {
  if (stage.status === 'finished') return 100
  if (stage.status === 'running' && stage.total_units > 1 && stage.completed_units < stage.total_units) {
    return Math.max(0, stage.completed_units / stage.total_units * 100)
  }
  return undefined
}
function stageDetail(stage: TimingStage): string {
  return [stage.total_units ? `分段 ${stage.completed_units} / ${stage.total_units}` : '',
    expanded.value && stage.attempts ? `请求 ${stage.attempts} 次` : '',
    stage.retry_wait_ms ? `重试等待 ${formatElapsed(stage.retry_wait_ms)}` : ''].filter(Boolean).join(' · ')
}
const currentTiming = computed(() => mapOnly.value ? mapTiming.value : summaryTiming.value)
const startDate = computed(() => currentTiming.value ? new Date(currentTiming.value.accepted_at_ms).toLocaleString() : null)
const endDate = computed(() => {
  if (waitingMap.value || pending.value || !currentTiming.value?.finished_at_ms) return null
  const end = paired.value ? Math.max(currentTiming.value.finished_at_ms, mapTiming.value?.finished_at_ms || 0) : currentTiming.value.finished_at_ms
  return new Date(end).toLocaleString()
})
const models = computed(() => [...new Set([props.summary.model_info?.model, props.map?.model_info?.model].filter(Boolean))].join(' / '))
</script>

<template>
  <section class="execution-timing" aria-label="执行耗时">
    <div class="execution-heading"><span><Clock3 :size="17" />执行耗时</span><span class="execution-status" :data-state="taskState" aria-live="polite">{{ status }}</span></div>
    <div class="execution-total" role="group" aria-label="本次执行总耗时">
      <div><span class="execution-total-label">本次执行总耗时</span><strong>{{ waitingMap ? '正在获取' : formatElapsed(total) }}</strong></div>
      <span class="execution-scope">{{ scope }}</span>
      <p>{{ mapOnly ? '从本次导图任务受理到结束，不含之前生成总结的时间。' : '从任务受理到本次所有生成任务结束，并行阶段按实际等待时间计算。' }}</p>
    </div>
    <div class="execution-metrics">
      <div v-for="item in metrics" :key="item.id" class="execution-metric" role="group" :aria-label="item.label">
        <span class="execution-metric-label">{{ item.label }}</span>
        <strong>{{ item.value }}</strong>
        <span v-if="item.state" class="execution-metric-state execution-status" :data-state="item.status">{{ item.state }}</span>
        <small>{{ item.note }}</small>
      </div>
    </div>
    <p v-if="paired" class="execution-note">总结与导图并行生成；总耗时包含准备阶段，以较晚完成的任务为准。</p>
    <p v-if="!summaryTiming && !mapTiming" class="execution-note">此历史任务未记录执行时间</p>
    <div class="execution-detail-heading"><span>阶段明细</span><button type="button" :aria-expanded="expanded" @click="expanded = !expanded">{{ expanded ? '收起分段详情' : '展开分段详情' }}<ChevronDown :size="15" :class="{ flipped: expanded }" /></button></div>
    <div class="execution-groups">
      <section v-for="group in groups" :key="group.id" class="execution-group" :aria-label="group.label">
        <div class="execution-group-heading"><span>{{ group.label }}<small v-if="group.previous">之前执行</small></span><span class="execution-group-summary">任务全程 {{ group.timing ? formatElapsed(elapsedAt(group.timing, group.delta)) : '未记录' }}<span class="execution-status" :data-state="group.taskStatus">{{ stateLabels[group.taskStatus] }}</span></span></div>
        <div class="execution-stages">
          <div v-for="stage in group.stages" :key="stage.id" class="execution-stage" :class="{ nested: stage.parent_id }" :data-state="stage.status">
            <span class="execution-stage-name"><LoaderCircle v-if="stage.status === 'running'" class="execution-stage-icon spin" :size="15" aria-hidden="true" /><Check v-else-if="stage.status === 'finished'" class="execution-stage-icon" :size="15" aria-hidden="true" /><CircleX v-else-if="stage.status === 'failed'" class="execution-stage-icon" :size="15" aria-hidden="true" /><CircleSlash v-else class="execution-stage-icon" :size="15" aria-hidden="true" /><span>{{ stage.label }}</span></span>
            <span class="execution-stage-result"><span class="execution-status" :data-state="stage.status">{{ stateLabels[stage.status] }}</span><span class="execution-stage-duration">{{ formatElapsed(stageElapsedAt(stage, group.delta)) }}</span></span>
            <div v-if="stage.status === 'finished' || stage.status === 'running'" class="execution-lane" :class="{ indeterminate: stage.progress === undefined }" role="progressbar" :aria-label="stage.label" :aria-valuenow="stage.progress" :aria-valuemin="0" :aria-valuemax="100" :aria-valuetext="stage.status === 'finished' ? '已完成' : stage.progress === undefined ? '执行中，进度未知' : `已完成 ${stage.completed_units} / ${stage.total_units} 段`"><span :style="stage.progress === undefined ? undefined : { width: `${stage.progress}%` }"></span></div>
            <small v-if="stage.detail" class="execution-unit">{{ stage.detail }}</small>
          </div>
          <p v-if="!group.stages.length" class="execution-note">阶段耗时未记录</p>
        </div>
      </section>
    </div>
    <div class="execution-dates"><span v-if="startDate">本次开始 {{ startDate }}</span><span v-if="endDate">本次结束 {{ endDate }}</span></div>
    <div class="execution-meta"><span v-if="models">{{ models }}</span><span v-if="summary.result?.source">{{ summary.result.source }}</span><span v-if="fetchMs != null">最近状态获取 {{ formatElapsed(fetchMs) }}</span><span v-if="renderMs != null">导图布局 {{ formatElapsed(renderMs) }}</span></div>
  </section>
</template>

<style scoped>
.execution-timing{padding:20px;margin-top:20px;border:1px solid #dce4ef;border-radius:16px;background:#f8fafd;color:#172033}
.execution-heading,.execution-heading>span,.execution-detail-heading,.execution-detail-heading button{display:flex;align-items:center;gap:8px}
.execution-heading,.execution-detail-heading{justify-content:space-between;flex-wrap:wrap}.execution-heading{font-weight:600}
.execution-note,.execution-meta,.execution-dates{font-size:12px;color:#52647c}
.execution-status{display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;padding:3px 8px;border-radius:6px;background:#edf0f5;color:#52647c;font-size:11px;font-weight:500;line-height:1.4;white-space:nowrap}
.execution-status[data-state="finished"]{color:#23704b;background:#e7f4ed}.execution-status[data-state="running"]{color:#365fb5;background:#e9f0ff}.execution-status[data-state="failed"]{color:#ad3c3c;background:#fcecec}.execution-status[data-state="partial"]{color:#976128;background:#fcf2e4}
.execution-total{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px 16px;padding:18px;margin:16px 0 12px;border:1px solid #d6e2fb;border-radius:12px;background:#edf3ff}
.execution-total>div{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}.execution-total-label{font-size:14px;font-weight:600}.execution-total strong{font-size:29px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.2}.execution-scope{font-size:12px;color:#365d91}.execution-total p{width:100%;margin:0;font-size:12px;line-height:1.6;color:#52647c}
.execution-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:12px}
.execution-metric{display:flex;flex-direction:column;align-items:flex-start;gap:7px;padding:15px;border:1px solid #e0e7f1;border-radius:12px;background:#fff;min-width:0}
.execution-metric-label{font-size:13px;font-weight:600;color:#36506f}.execution-metric strong{font-size:23px;line-height:1.3;font-weight:650;font-variant-numeric:tabular-nums}.execution-metric small{font-size:11px;line-height:1.6;color:#64748b}
.execution-note{margin:0 0 16px;line-height:1.7}.execution-detail-heading{padding:5px 0 12px;font-size:13px;font-weight:600}.execution-detail-heading button{border:0;background:transparent;color:#365d91;padding:6px 0;cursor:pointer;font-size:12px;font-weight:400}.flipped{transform:rotate(180deg)}
.execution-groups{display:grid;gap:16px}.execution-group{min-width:0;padding:15px;border:1px solid #e0e7f1;border-radius:12px;background:#fff}.execution-group-heading{display:flex;justify-content:space-between;align-items:center;gap:8px 12px;flex-wrap:wrap;margin-bottom:6px;font-size:13px;font-weight:600}.execution-group-summary{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:400;color:#52647c}.execution-group-heading small{margin-left:8px;font-size:11px;font-weight:400;color:#64748b}
.execution-stages{display:grid}.execution-stage{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px 12px;padding:11px 0}.execution-stage+.execution-stage{border-top:1px solid #f0f3f7}.execution-stage-name{display:flex;gap:7px;align-items:center;font-size:13px;min-width:0;line-height:1.5}.execution-stage-name>span{overflow-wrap:anywhere}.execution-stage-icon{flex-shrink:0;color:#64748b}.execution-stage[data-state="finished"] .execution-stage-icon{color:#35845e}.execution-stage[data-state="running"] .execution-stage-icon{color:#426ed2}.execution-stage[data-state="failed"] .execution-stage-icon{color:#ad3c3c}.execution-stage-result{display:flex;align-items:center;gap:12px}.execution-stage-duration{min-width:58px;text-align:right;white-space:nowrap;font-size:13px;font-variant-numeric:tabular-nums}.execution-lane{grid-column:1/-1;height:5px;overflow:hidden;border-radius:3px;background:#e8edf5}.execution-lane>span{display:block;height:100%;background:#426ed2;border-radius:3px}.execution-stage[data-state="finished"] .execution-lane{background:#e7f4ed}.execution-stage[data-state="finished"] .execution-lane>span{background:#5c9e7c}.execution-lane.indeterminate>span{width:32%;animation:execution-progress 1.8s ease-in-out infinite}.execution-unit{grid-column:1/-1;color:#64748b;font-size:11px;line-height:1.6}.execution-stage.nested{margin-left:16px}
.execution-meta,.execution-dates{display:flex;gap:6px 16px;flex-wrap:wrap;overflow-wrap:anywhere;line-height:1.6}.execution-dates{margin-top:18px}.execution-meta{margin-top:6px}
@keyframes execution-progress{0%{transform:translateX(-100%)}100%{transform:translateX(315%)}}
@media(prefers-reduced-motion:reduce){.execution-lane.indeterminate>span,.execution-stage-icon.spin{animation:none}.execution-lane.indeterminate>span{transform:translateX(100%)}}
@media(max-width:640px){.execution-timing{padding:15px}.execution-total{padding:15px}.execution-total>div{display:block}.execution-total-label{display:block;margin-bottom:10px}.execution-total strong{font-size:27px}.execution-metrics{grid-template-columns:1fr}.execution-metric{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px 10px;padding:13px}.execution-metric strong{grid-column:2;grid-row:1/3;font-size:22px;align-self:center}.execution-metric-state{grid-column:1;grid-row:2;justify-self:start}.execution-metric small{grid-column:1/-1}.execution-group{padding:12px}.execution-stage{gap:7px 8px}.execution-stage-result{gap:6px;flex-wrap:wrap;justify-content:flex-end}.execution-stage-result .execution-status{padding:2px 5px}.execution-stage-duration{min-width:52px;font-size:12px}.execution-stage.nested{margin-left:8px}}
</style>
