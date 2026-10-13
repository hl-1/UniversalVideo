<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ChevronLeft, ChevronRight, Download, Eye, LoaderCircle, Search, Trash2, X } from 'lucide-vue-next'
import { api, refreshLibrary } from './musicLibrary'
const kind = ref('file')
const query = ref('')
const page = ref(1)
const total = ref(0)
const rows = ref<any[]>([])
const loading = ref(false)
const error = ref('')
const detail = ref<any>(null)
const kinds: Record<string, string> = { file: '我的文件', summary_file: '总结文件', download: '下载任务', summary: 'AI 总结', mind_map: '思维导图', watermark: '去水印任务', batch: '批量下载', music_import: '批量找歌', video_parse: '视频解析', video_search: '视频搜索记录', video_view: '视频预览状态' }
const statuses: Record<string, string> = { pending: '排队中', running: '处理中', finished: '完成', failed: '失败', cancelled: '已取消', cancelling: '正在停止', recognizing: '截图识别中', searching: '搜索中', draft: '待搜索' }
kinds.music_recommendation = '场景歌单任务'
Object.assign(statuses, { partial: '部分完成', waiting_input: '待补充', waiting_auth: '待认证', interrupted: '已中断' })
let timer: number | undefined
let generation = 0
async function load(quiet = false) {
  const revision = ++generation
  if (!quiet) loading.value = true
  try {
    const data = await api(`/api/workspace/resources?kind=${kind.value}&page=${page.value}&q=${encodeURIComponent(query.value)}`)
    if (revision === generation) { rows.value = data.items; total.value = data.total; error.value = '' }
  } catch (failure) { if (revision === generation) error.value = failure instanceof Error ? failure.message : '任务加载失败。' }
  finally { if (revision === generation) loading.value = false }
}
function filter() { page.value = 1; void load() }
async function remove(row: any) {
  if (!confirm(`确认删除“${row.payload?.filename || row.resource_key}”？`)) return
  try { await api(`/api/workspace/resources/${kind.value}/${encodeURIComponent(row.resource_key)}`, { method: 'DELETE' }); await load(); await refreshLibrary() }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '删除失败。' }
}
async function download(row: any) {
  try {
    const response = await fetch(row.download_url)
    if (!response.ok) throw new Error((await response.json()).detail || '下载失败。')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url; link.download = row.payload?.filename || row.resource_key; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 10000)
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '下载失败。' }
}
onMounted(() => { void load(); timer = window.setInterval(() => { if (rows.value.some(row => ['pending', 'running', 'cancelling', 'recognizing', 'searching'].includes(row.payload?.status))) void load(true) }, 5000) })
onBeforeUnmount(() => { generation++; clearInterval(timer) })
</script>
<template>
  <section>
    <p v-if="error" class="account-error" role="alert">{{ error }}</p>
    <form class="admin-filters personal-filters" @submit.prevent="filter"><select v-model="kind" aria-label="我的资源类型" @change="filter"><option v-for="(label, key) in kinds" :key="key" :value="key">{{ label }}</option></select><input v-model="query" placeholder="搜索文件或任务" aria-label="搜索文件或任务" maxlength="100" /><button class="account-icon" title="搜索" aria-label="搜索" type="submit"><Search :size="18" /></button></form>
    <div v-if="loading" class="account-loading"><LoaderCircle class="spin" :size="24" /></div>
    <div v-else class="admin-table-wrap"><table class="admin-table"><thead><tr><th>名称</th><th>状态 / 大小</th><th>创建时间</th><th>操作</th></tr></thead><tbody><tr v-for="row in rows" :key="row.resource_key"><td class="admin-name">{{ row.payload?.result?.title || row.payload?.filename || row.resource_key }}</td><td>{{ statuses[row.payload?.status] || (row.payload?.size ? `${(row.payload.size / 1048576).toFixed(1)} MB` : '已保存') }}<small v-if="row.payload?.message" class="admin-secondary">{{ row.payload.message }}</small></td><td>{{ new Date(row.created_at * 1000).toLocaleString() }}</td><td><div class="admin-actions"><button class="account-icon" title="查看详情" aria-label="查看详情" @click="detail = row.payload"><Eye :size="17" /></button><button v-if="row.download_url" class="account-icon" title="下载文件" aria-label="下载文件" @click="download(row)"><Download :size="17" /></button><button class="account-icon danger" title="删除" aria-label="删除" @click="remove(row)"><Trash2 :size="17" /></button></div></td></tr><tr v-if="!rows.length"><td colspan="4">暂无记录</td></tr></tbody></table></div>
    <footer class="admin-pagination"><span>共 {{ total }} 项</span><button class="account-icon" title="上一页" aria-label="上一页" :disabled="page <= 1" @click="page--; load()"><ChevronLeft :size="19" /></button><span>{{ page }} / {{ Math.max(1, Math.ceil(total / 20)) }}</span><button class="account-icon" title="下一页" aria-label="下一页" :disabled="page * 20 >= total" @click="page++; load()"><ChevronRight :size="19" /></button></footer>
    <div v-if="detail" class="account-dialog-backdrop" @click.self="detail = null" @keydown.esc="detail = null"><section class="account-dialog" role="dialog" aria-modal="true" aria-labelledby="resource-detail-title"><header><h2 id="resource-detail-title">任务详情</h2><button class="account-icon" title="关闭" aria-label="关闭" @click="detail = null"><X :size="20" /></button></header><pre>{{ JSON.stringify(detail, null, 2) }}</pre></section></div>
  </section>
</template>
