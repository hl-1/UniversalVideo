<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowLeft, BookOpen, Check, CircleStop, RefreshCw, Workflow, X } from 'lucide-vue-next'
import { api, jsonRequest } from '../../shared/api/client'
import { kindLabels, requestKey, sourceFile, statusLabels, subscribe, terminal, versionRequest, type AgentTask } from './tasks'
import './agent-workspace.css'

const props = defineProps<{ taskId?: string }>()
const emit = defineEmits<{ back: []; knowledge: [] }>()
const tasks = ref<AgentTask[]>([]), selected = ref<AgentTask | null>(null)
const status = ref(''), kind = ref(''), error = ref(''), busy = ref(false), page = ref(1), total = ref(0)
const goal = ref(''), playlistName = ref(''), createOpen = ref(false)
const events = ref<Array<{ id: number; message: string; status: string }>>([])
const takeover = ref<any>(null), inputValue = ref(''), element = ref(0)
let controller: AbortController | undefined, timer: ReturnType<typeof setInterval> | undefined, disposed = false
const label = (value: string) => statusLabels[value] || value
async function load() {
  const data = await api<{ items: AgentTask[]; total: number }>(`/api/agent/tasks?status=${status.value}&kind=${kind.value}&page=${page.value}`)
  tasks.value = data.items; total.value = data.total
}
async function refreshSelected() {
  if (selected.value) selected.value = await api<AgentTask>(`/api/agent/tasks/${selected.value.id}`)
}
async function open(id: string) {
  controller?.abort(); takeover.value = null; error.value = ''; events.value = []
  selected.value = await api<AgentTask>(`/api/agent/tasks/${id}`)
  const abort = controller = new AbortController()
  let last = 0
  void (async () => {
    while (!abort.signal.aborted && !disposed) {
      try {
        await subscribe(id, last, abort.signal, (sequence, value) => {
          last = sequence
          events.value = [...events.value, { id: sequence, message: value.message || value.node || '状态已更新', status: value.status || '' }].slice(-60)
          void refreshSelected().catch(e => { error.value = e.message })
        })
        if (selected.value && terminal(selected.value.status)) return
      } catch { if (abort.signal.aborted) return }
      await new Promise(resolve => setTimeout(resolve, 2000))
    }
  })()
}
async function act(action: string) {
  if (!selected.value) return
  await run(async () => { await versionRequest(selected.value!, action); await refreshSelected(); await load() })
}
async function decide(actionId: string, hash: string, approved: boolean) {
  await run(async () => {
    await api(`/api/agent/actions/${actionId}/decision`, jsonRequest('POST', { revision: selected.value!.revision, payload_hash: hash, approved }))
    await refreshSelected(); await load()
  })
}
async function create() {
  await run(async () => {
    const data = await api<{ task_id: string }>('/api/agent/tasks', { ...jsonRequest('POST', { kind: 'browser_playlist', request: { goal: goal.value, playlist_name: playlistName.value, site: 'netease' } }),
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestKey() } })
    createOpen.value = false; await load(); await open(data.task_id)
  })
}
async function take(action: string) {
  if (!selected.value?.observation) return
  await run(async () => {
    const data = await api<any>(`/api/agent/tasks/${selected.value!.id}/takeover`, jsonRequest('POST', {
      revision: selected.value!.revision, observation_revision: selected.value!.observation!.revision,
      session: takeover.value?.session || '', view_revision: takeover.value?.view_revision || '', action, element: element.value, value: inputValue.value,
    }))
    inputValue.value = ''
    takeover.value = data.closed || data.task ? null : data
    await refreshSelected()
  })
}
async function run(callback: () => Promise<void>) {
  busy.value = true; error.value = ''
  try { await callback() } catch (e: any) { error.value = e.message; await refreshSelected().catch(() => {}) }
  finally { busy.value = false }
}
function close() { controller?.abort(); selected.value = null; takeover.value = null }
watch([status, kind], () => { page.value = 1; void run(load) })
watch(() => props.taskId, id => { if (id) void run(() => open(id)) })
onMounted(() => {
  void run(async () => { await load(); if (props.taskId) await open(props.taskId) })
  timer = setInterval(() => { if (!document.hidden && !busy.value) void load().catch(() => {}) }, 5000)
})
onBeforeUnmount(() => { disposed = true; controller?.abort(); clearInterval(timer) })
</script>

<template>
  <section class="agent-workspace">
    <header class="aw-header"><button class="aw-icon" aria-label="返回音乐" @click="emit('back')"><ArrowLeft :size="21" /></button><div><small>VIDEODREAM / AGENTS</small><h1>任务中心</h1></div><button class="aw-secondary" @click="emit('knowledge')"><BookOpen :size="17" />知识库</button></header>
    <div class="aw-intro"><div><h2>让任务有进度，也有依据</h2><p>查看执行过程、处理审批，随时取消或恢复任务。</p></div><button class="aw-primary" @click="createOpen = !createOpen"><Workflow :size="18" />浏览器导入歌单</button></div>
    <p v-if="error" class="aw-error" role="alert">{{ error }}</p>
    <form v-if="createOpen" class="aw-panel aw-form" @submit.prevent="create"><h3>从网易云公开歌单导入</h3><label>搜索目标<input v-model="goal" required maxlength="1000" placeholder="例如：适合雨天阅读的纯音乐歌单" /></label><label>保存为<input v-model="playlistName" required maxlength="80" placeholder="我的阅读歌单" /></label><p class="aw-muted">提取曲目后先展示预览，经你确认才保存。</p><button class="aw-primary" :disabled="busy">开始搜索</button></form>
    <div class="aw-filters"><label>状态<select v-model="status"><option value="">全部状态</option><option v-for="(text,key) in statusLabels" :key="key" :value="key">{{ text }}</option></select></label><label>类型<select v-model="kind"><option value="">全部类型</option><option v-for="(text,key) in kindLabels" :key="key" :value="key">{{ text }}</option></select></label><button class="aw-icon" aria-label="刷新任务" @click="run(load)"><RefreshCw :size="18" /></button><span class="aw-muted">共 {{ total }} 个任务</span></div>
    <div v-if="!tasks.length" class="aw-empty"><Workflow :size="36" /><h3>任务会在这里留下记录</h3><p>创建知识问答、上传文档，或开始一次歌单导入。</p></div>
    <div class="aw-cards"><button v-for="task in tasks" :key="task.id" class="aw-task" @click="run(() => open(task.id))"><div><span class="aw-badge" :data-status="task.status">{{ label(task.status) }}</span><small>{{ new Date(task.created_at).toLocaleString() }}</small></div><h3>{{ kindLabels[task.kind] || task.kind }}</h3><p>{{ task.message || task.node || '等待执行' }}</p><footer><span>{{ (task.usage.actual || 0).toLocaleString() }} tokens</span><span>{{ Math.round(task.usage.seconds || 0) }} 秒</span></footer></button></div>
    <div v-if="total > 20" class="aw-buttons"><button :disabled="page <= 1" @click="page--; run(load)">上一页</button><span>{{ page }}</span><button :disabled="page * 20 >= total" @click="page++; run(load)">下一页</button></div>
    <div v-if="selected" class="aw-overlay" @click.self="close"><section class="aw-drawer" role="dialog" aria-modal="true" aria-label="任务详情"><header><div><small>{{ kindLabels[selected.kind] }}</small><h2>任务详情 <span class="aw-badge" :data-status="selected.status">{{ label(selected.status) }}</span></h2></div><button class="aw-icon" aria-label="关闭详情" @click="close"><X :size="21" /></button></header>
      <p>{{ selected.message }}</p><p v-if="selected.error" class="aw-error">{{ selected.error }}</p>
      <div class="aw-metrics"><div><small>实际用量</small><strong>{{ (selected.usage.actual || 0).toLocaleString() }}</strong><span> / {{ selected.limits.tokens.toLocaleString() }} tokens</span></div><div><small>预留用量</small><strong>{{ (selected.usage.reserved || 0).toLocaleString() }}</strong></div><div><small>自动执行</small><strong>{{ Math.round(selected.usage.seconds || 0) }}</strong><span> 秒</span></div></div>
      <div class="aw-buttons"><button v-if="!terminal(selected.status)" class="aw-danger" :disabled="busy" @click="act('cancel')"><CircleStop :size="17" />取消任务</button><button v-if="['interrupted','waiting_input','waiting_auth'].includes(selected.status) && !selected.kind.startsWith('music_')" :disabled="busy" @click="act('resume')">继续执行</button><button v-if="selected.status === 'failed' && !selected.kind.startsWith('music_')" :disabled="busy" @click="act('retry')">恢复未完成步骤</button><button v-if="['waiting_auth','waiting_input'].includes(selected.status) && selected.kind === 'browser_playlist'" :disabled="busy" @click="take('open')">接管浏览器</button></div>
      <p v-if="selected.status === 'waiting_usage'" class="aw-notice">模型用量待核对，后续付费调用已暂停。请核对供应商记录后由管理员处理。</p>
      <template v-for="action in selected.actions || []" :key="action.id"><section v-if="action.kind === 'write'" class="aw-panel aw-approval"><h3>保存歌单审批 · {{ action.arguments.name }}</h3><p>影响：新增歌单与 {{ action.arguments.tracks?.length || 0 }} 首歌曲</p><p class="aw-muted">审批有效期：{{ action.approval_expires_at ? new Date(action.approval_expires_at).toLocaleString() : '—' }}</p><ol class="aw-track-preview"><li v-for="(track,index) in action.arguments.tracks" :key="index"><strong>{{ track.title }}</strong><span>{{ track.artist || '未知歌手' }}</span></li></ol><div v-if="action.approval_state === 'pending' && selected.status === 'waiting_approval'" class="aw-buttons"><button class="aw-primary" :disabled="busy" @click="decide(action.id, action.payload_hash, true)"><Check :size="17" />同意保存</button><button :disabled="busy" @click="decide(action.id, action.payload_hash, false)">拒绝</button></div><p v-else class="aw-muted">{{ {approved:'已同意',rejected:'已拒绝'}[action.approval_state] || action.approval_state }}</p></section></template>
      <section v-if="takeover" class="aw-panel aw-form"><h3>浏览器接管</h3><p class="aw-muted">{{ takeover.url }}</p><img :src="takeover.screenshot" class="aw-browser-image" alt="当前浏览器页面" /><label>选择页面元素<select v-model.number="element"><option v-for="item in takeover.elements" :key="item.id" :value="item.id">{{ item.id }} · {{ item.label }}</option></select></label><label>填写内容<input v-model="inputValue" type="password" autocomplete="off" placeholder="输入只交给浏览器执行器" /></label><div class="aw-buttons"><button :disabled="busy" @click="take('fill')">填写</button><button :disabled="busy" @click="take('click')">点击</button><button class="aw-primary" :disabled="busy" @click="take('continue')">完成接管并继续</button><button :disabled="busy" @click="take('close')">关闭接管</button></div></section>
      <section v-if="selected.result" class="aw-panel"><h3>执行结果</h3><p v-if="selected.result.answer" class="aw-answer">{{ selected.result.answer }}</p><p v-if="selected.result.playlist_id">已核验保存 {{ selected.result.track_count }} 首歌曲，返回音乐中心查看歌单。</p><p v-if="selected.result.chunk_count">已发布 {{ selected.result.chunk_count }} 个文档片段。</p><article v-for="(citation,index) in selected.result.citations" :key="citation.chunk_id" class="aw-citation"><strong>来源 {{ index + 1 }} · {{ citation.locator.page ? `第 ${citation.locator.page} 页` : citation.locator.section || (citation.locator.start_ms !== undefined ? `${Math.floor(citation.locator.start_ms / 1000)} 秒` : '原文片段') }}</strong><p>{{ citation.content }}</p><button @click="run(() => sourceFile(citation))">查看原文</button></article></section>
      <section><h3>操作记录</h3><ol class="aw-timeline"><li v-for="event in events" :key="event.id"><span>#{{ event.id }}</span><p>{{ event.message }} <small>{{ label(event.status) }}</small></p></li></ol></section>
      <details v-if="selected.model_calls?.length"><summary>模型用量明细</summary><div class="aw-scroll"><table><thead><tr><th>阶段</th><th>模型</th><th>状态</th><th>用量</th></tr></thead><tbody><tr v-for="call in selected.model_calls" :key="call.id"><td>{{ call.phase }}</td><td>{{ call.model }}</td><td>{{ call.state }}</td><td>{{ call.actual_tokens ?? call.reserved_tokens }}</td></tr></tbody></table></div></details>
      <section v-if="selected.children?.length"><h3>子任务</h3><button v-for="child in selected.children" :key="child.id" @click="run(() => open(child.id))">{{ kindLabels[child.kind] }} · {{ label(child.status) }}</button></section>
      <p class="aw-muted aw-task-id">{{ selected.id }} · {{ selected.graph_version }}</p>
    </section></div>
  </section>
</template>
