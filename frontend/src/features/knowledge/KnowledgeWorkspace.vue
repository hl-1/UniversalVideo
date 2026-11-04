<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowLeft, BookOpen, FileText, Plus, Send, Trash2, Upload, Users, Workflow } from 'lucide-vue-next'
import { api, jsonRequest } from '../../shared/api/client'
import { requestKey, sourceFile, statusLabels, subscribe, terminal, type AgentTask } from '../agents/tasks'
import '../agents/agent-workspace.css'

interface Base { id: string; name: string; owner_id: string; role: string; revision: number; document_count: number }
interface Version { version: number; status: string; chunk_count: number; error?: string; source_kind: string; source_locator?: string }
interface KnowledgeDocument { id: string; title: string; status: string; revision: number; active_version?: number; target_version: number; versions: Version[] }
const emit = defineEmits<{ back: []; tasks: [taskId?: string] }>()
const bases = ref<Base[]>([]), selected = ref<Base | null>(null), documents = ref<KnowledgeDocument[]>([])
const tab = ref<'ask' | 'documents' | 'members'>('ask'), busy = ref(false), error = ref(''), baseName = ref(''), createOpen = ref(false)
const question = ref(''), rerank = ref(false), answer = ref<AgentTask | null>(null), queryBusy = ref(false), history = ref<AgentTask[]>([])
const members = ref<Array<{ user_id: string; username: string; role: string }>>([]), memberId = ref(''), memberRole = ref('viewer'), newOwner = ref('')
const videoTask = ref(''), videoKind = ref('video_subtitle'), uploadTask = ref('')
const canEdit = computed(() => selected.value?.role === 'owner' || selected.value?.role === 'editor')
let stream: AbortController | undefined, timer: ReturnType<typeof setInterval> | undefined, disposed = false
async function loadBases() {
  const data = await api<{ items: Base[] }>('/api/knowledge/bases')
  bases.value = data.items
  selected.value = data.items.find(item => item.id === selected.value?.id) || data.items[0] || null
}
async function loadDocuments() {
  if (!selected.value) { documents.value = []; return }
  documents.value = (await api<{ items: KnowledgeDocument[] }>(`/api/knowledge/bases/${selected.value.id}/documents`)).items
}
async function loadMembers() {
  if (!selected.value) return
  const result = await api<any>(`/api/knowledge/bases/${selected.value.id}/members`)
  members.value = result.items; selected.value.revision = result.revision
}
async function select(base: Base) { selected.value = base; answer.value = null; await loadDocuments(); await loadMembers() }
async function run(callback: () => Promise<void>) {
  busy.value = true; error.value = ''
  try { await callback() } catch (e: any) { error.value = e.message } finally { busy.value = false }
}
async function create() {
  await run(async () => {
    const result = await api<{ id: string }>('/api/knowledge/bases', jsonRequest('POST', { name: baseName.value }))
    await loadBases(); const base = bases.value.find(item => item.id === result.id)
    if (base) await select(base)
    createOpen.value = false; baseName.value = ''
  })
}
async function updateBase(transfer = false) {
  if (!selected.value) return
  await run(async () => {
    await api(`/api/knowledge/bases/${selected.value!.id}`, jsonRequest('PATCH', {
      name: selected.value!.name, revision: selected.value!.revision, ...(transfer ? { owner_id: newOwner.value } : {}),
    }))
    newOwner.value = ''; await loadBases(); await loadMembers()
  })
}
async function removeBase() {
  if (!selected.value || !confirm(`删除“${selected.value.name}”及其文档？删除后将立即停止检索。`)) return
  await run(async () => { await api(`/api/knowledge/bases/${selected.value!.id}?revision=${selected.value!.revision}`, { method: 'DELETE' }); selected.value = null; await loadBases(); await loadDocuments() })
}
async function setMember() {
  await run(async () => {
    await api(`/api/knowledge/bases/${selected.value!.id}/members`, jsonRequest('PUT', { revision: selected.value!.revision, user_id: memberId.value, role: memberRole.value }))
    memberId.value = ''; await loadBases(); await loadMembers()
  })
}
async function removeMember(id: string) {
  await run(async () => { await api(`/api/knowledge/bases/${selected.value!.id}/members?user_id=${id}&revision=${selected.value!.revision}`, { method: 'DELETE' }); await loadBases(); await loadMembers() })
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file || !selected.value) return
  if (file.size > 20 * 1024 * 1024) { error.value = '单文件不能超过 20 MiB'; input.value = ''; return }
  await run(async () => {
    const body = new FormData(); body.append('file', file)
    const data = await api<{ task_id: string }>(`/api/knowledge/bases/${selected.value!.id}/documents`, { method: 'POST', headers: { 'Idempotency-Key': requestKey() }, body })
    uploadTask.value = data.task_id; await loadDocuments(); await loadBases()
  })
  input.value = ''
}
async function importVideo() {
  await run(async () => {
    const data = await api<{ task_id: string }>('/api/knowledge/documents/import', { ...jsonRequest('POST', { kb_id: selected.value!.id, summary_task_id: videoTask.value, kind: videoKind.value }), headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestKey() } })
    uploadTask.value = data.task_id; videoTask.value = ''; await loadDocuments(); await loadBases()
  })
}
async function reindex(doc: KnowledgeDocument) {
  await run(async () => {
    const data = await api<{ task_id: string }>(`/api/knowledge/documents/${doc.id}/reindex?revision=${doc.revision}`, { method: 'POST', headers: { 'Idempotency-Key': requestKey() } })
    uploadTask.value = data.task_id; await loadDocuments()
  })
}
async function removeDocument(doc: KnowledgeDocument) {
  if (!confirm(`删除文档“${doc.title}”？`)) return
  await run(async () => { await api(`/api/knowledge/documents/${doc.id}?revision=${doc.revision}`, { method: 'DELETE' }); await loadDocuments(); await loadBases() })
}
async function refreshHistory() {
  history.value = (await api<{ items: AgentTask[] }>('/api/agent/tasks?kind=knowledge_query')).items
}
async function showHistory(id: string) { await run(async () => { answer.value = await api<AgentTask>(`/api/agent/tasks/${id}`) }) }
async function ask() {
  if (!selected.value || queryBusy.value) return
  queryBusy.value = true; error.value = ''; stream?.abort(); const abort = stream = new AbortController()
  try {
    const data = await api<{ task_id: string }>('/api/knowledge/queries', { ...jsonRequest('POST', { question: question.value, kb_ids: [selected.value.id], rerank: rerank.value }), headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestKey() } })
    let sequence = 0
    while (!abort.signal.aborted && !disposed) {
      answer.value = await api<AgentTask>(`/api/agent/tasks/${data.task_id}`)
      if (terminal(answer.value.status) || answer.value.status.startsWith('waiting_')) break
      try { await subscribe(data.task_id, sequence, abort.signal, id => { sequence = id; void api<AgentTask>(`/api/agent/tasks/${data.task_id}`).then(value => { answer.value = value }).catch(() => {}) }) }
      catch { if (abort.signal.aborted) return; await new Promise(resolve => setTimeout(resolve, 1500)) }
    }
    await refreshHistory()
  } catch (e: any) { if (!abort.signal.aborted) error.value = e.message }
  finally { queryBusy.value = false }
}
onMounted(() => {
  void run(async () => { await loadBases(); await loadDocuments(); await loadMembers(); await refreshHistory() })
  timer = setInterval(() => { if (!document.hidden && !busy.value) void loadDocuments().catch(() => {}) }, 5000)
})
onBeforeUnmount(() => { disposed = true; stream?.abort(); clearInterval(timer) })
</script>

<template>
  <section class="agent-workspace">
    <header class="aw-header"><button class="aw-icon" aria-label="返回音乐" @click="emit('back')"><ArrowLeft :size="21" /></button><div><small>VIDEODREAM / KNOWLEDGE</small><h1>知识库</h1></div><button class="aw-secondary" @click="emit('tasks')"><Workflow :size="17" />任务中心</button></header>
    <div class="aw-intro"><div><h2>从资料中，找到有出处的答案</h2><p>整理文档与视频内容，按权限检索，沿引用回到原文。</p></div><button class="aw-primary" @click="createOpen = !createOpen"><Plus :size="18" />新建知识库</button></div>
    <p v-if="error" class="aw-error" role="alert">{{ error }}</p>
    <form v-if="createOpen" class="aw-panel aw-form" @submit.prevent="create"><label>知识库名称<input v-model="baseName" required maxlength="100" placeholder="例如：我的学习资料" /></label><button class="aw-primary" :disabled="busy">创建知识库</button></form>
    <div v-if="!bases.length" class="aw-empty"><BookOpen :size="38" /><h3>为你的资料留一个位置</h3><p>创建知识库后，上传文档或导入已有的视频摘要。</p></div>
    <div v-else class="aw-knowledge-grid">
      <aside aria-label="知识库列表"><button v-for="base in bases" :key="base.id" class="aw-base" :aria-current="selected?.id === base.id" @click="run(() => select(base))"><strong>{{ base.name }}</strong><small>{{ base.document_count }} 份文档 · {{ {owner:'所有者',editor:'可编辑',viewer:'可阅读'}[base.role as 'owner' | 'editor' | 'viewer'] }}</small></button></aside>
      <div v-if="selected">
        <nav class="aw-tabs" aria-label="知识库功能"><button :aria-current="tab === 'ask'" @click="tab = 'ask'"><BookOpen :size="16" />问答</button><button :aria-current="tab === 'documents'" @click="tab = 'documents'"><FileText :size="16" />文档</button><button :aria-current="tab === 'members'" @click="tab = 'members'"><Users :size="16" />成员与设置</button></nav>
        <template v-if="tab === 'ask'">
          <form class="aw-panel aw-form" @submit.prevent="ask"><label>向“{{ selected.name }}”提问<textarea v-model="question" required maxlength="4000" placeholder="你希望从资料中了解什么？" /></label><div class="aw-query-footer"><label><input v-model="rerank" type="checkbox" />启用重排</label><button class="aw-primary" :disabled="queryBusy || busy"><Send :size="16" />{{ queryBusy ? '正在检索与回答…' : '检索并回答' }}</button></div></form>
          <section v-if="answer" class="aw-panel"><div class="aw-buttons"><span class="aw-badge" :data-status="answer.status">{{ statusLabels[answer.status] || answer.status }}</span><button @click="emit('tasks', answer.id)">查看任务</button></div><p v-if="!answer.result?.answer" class="aw-muted">{{ answer.message || answer.error || '正在检索资料…' }}</p><p v-if="answer.result?.answer" class="aw-answer">{{ answer.result.answer }}</p><p v-if="answer.result?.insufficient" class="aw-notice">现有证据不足，请补充文档或调整问题。</p><article v-for="(citation,index) in answer.result?.citations" :key="citation.chunk_id" class="aw-citation"><strong>来源 {{ index + 1 }} · 版本 {{ citation.version }}<template v-if="citation.locator.page"> · 第 {{ citation.locator.page }} 页</template><template v-else-if="citation.locator.start_ms !== undefined"> · {{ Math.floor(citation.locator.start_ms / 1000) }} 秒</template><template v-else> · {{ citation.locator.section || '原文片段' }}</template></strong><p>{{ citation.content }}</p><button @click="run(() => sourceFile(citation))">查看原文</button></article></section>
          <section v-if="history.length"><h3>最近问答</h3><button v-for="item in history" :key="item.id" class="aw-history-item" @click="showHistory(item.id)">{{ item.result?.answer?.slice(0,100) || statusLabels[item.status] }}<small>{{ new Date(item.created_at).toLocaleString() }}</small></button></section>
        </template>
        <template v-if="tab === 'documents'">
          <label v-if="canEdit" class="aw-upload"><Upload :size="23" /><p>上传文档，建立可引用的知识</p><small>文本 PDF / DOCX / Markdown / TXT · 单文件不超过 20 MiB</small><input type="file" accept=".pdf,.docx,.md,.txt" :disabled="busy" @change="upload" /></label>
          <p v-if="uploadTask" class="aw-notice">文档已进入处理队列。<button @click="emit('tasks', uploadTask)">查看入库进度</button></p>
          <div v-if="!documents.length" class="aw-empty"><FileText :size="32" /><h3>还没有文档</h3><p>上传资料后即可开始检索问答。</p></div>
          <article v-for="doc in documents" :key="doc.id" class="aw-document"><div><h4>{{ doc.title }}</h4><small>{{ {indexing:'入库中',published:'已发布',failed:'入库失败'}[doc.status as 'indexing' | 'published' | 'failed'] || doc.status }} · 当前版本 {{ doc.active_version || '尚未发布' }}</small><p v-if="doc.versions[0]?.error" class="aw-error">{{ doc.versions[0]?.error }}</p><details><summary>版本与来源</summary><p v-for="version in doc.versions" :key="version.version" class="aw-muted">V{{ version.version }} · {{ version.status }} · {{ version.chunk_count }} 段 · {{ version.source_kind }}<br v-if="version.source_locator" />{{ version.source_locator }}</p></details></div><button v-if="canEdit" :disabled="busy || doc.status === 'indexing'" @click="reindex(doc)">重建索引</button><button v-if="canEdit" class="aw-icon aw-danger" :disabled="busy" aria-label="删除文档" @click="removeDocument(doc)"><Trash2 :size="17" /></button></article>
          <form v-if="canEdit" class="aw-panel aw-form" @submit.prevent="importVideo"><h3>导入已有视频内容</h3><label>视频总结任务 ID<input v-model="videoTask" required maxlength="64" placeholder="填写你有权访问的已完成视频总结任务 ID" /></label><label>导入内容<select v-model="videoKind"><option value="video_subtitle">字幕与时间点</option><option value="video_summary">视频摘要</option></select></label><button :disabled="busy">导入知识库</button></form>
        </template>
        <template v-if="tab === 'members'">
          <p class="aw-muted">所有者 ID：{{ selected.owner_id }}</p><div v-for="member in members" :key="member.user_id" class="aw-member"><span>{{ member.username }} · {{ member.role === 'editor' ? '可编辑' : '可阅读' }}<small>（{{ member.user_id }}）</small></span><button v-if="selected.role === 'owner'" :disabled="busy" @click="removeMember(member.user_id)">移除</button></div>
          <template v-if="selected.role === 'owner'"><form class="aw-panel aw-form" @submit.prevent="setMember"><h3>添加或调整成员</h3><label>用户 ID<input v-model="memberId" required inputmode="numeric" pattern="[0-9]+" maxlength="19" placeholder="完整用户 ID" /></label><label>权限<select v-model="memberRole"><option value="viewer">可阅读与问答</option><option value="editor">可上传与管理文档</option></select></label><button :disabled="busy">保存成员权限</button></form><form class="aw-panel aw-form" @submit.prevent="updateBase()"><h3>知识库设置</h3><label>名称<input v-model="selected.name" required maxlength="100" /></label><button :disabled="busy">保存名称</button></form><form class="aw-panel aw-form" @submit.prevent="updateBase(true)"><h3>转移所有权</h3><label>新所有者 ID<input v-model="newOwner" required inputmode="numeric" pattern="[0-9]+" maxlength="19" /></label><button :disabled="busy">转移所有权</button></form><button class="aw-danger" :disabled="busy" @click="removeBase"><Trash2 :size="16" />删除知识库</button></template>
        </template>
      </div>
    </div>
  </section>
</template>
