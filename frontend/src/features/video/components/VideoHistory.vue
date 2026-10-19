<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, ChevronLeft, ChevronRight, Copy, Eye, LoaderCircle, RotateCcw, Search, Trash2, Video, X } from 'lucide-vue-next'
import { api } from '../../../shared/api/client'
import type { VideoHistoryRow } from '../videoHistory'

const emit = defineEmits<{ open: [id: string]; reparse: [url: string]; removed: [id: string] }>()
const rows = ref<VideoHistoryRow[]>([])
const page = ref(1)
const total = ref(0)
const query = ref('')
const platform = ref('')
const status = ref('')
const loading = ref(false)
const error = ref('')
const deleting = ref<VideoHistoryRow | null>(null)
const cascade = ref(false)
const busy = ref(false)
const copied = ref('')
let revision = 0
let timer: number | undefined
async function load(quiet = false) {
  const current = ++revision
  if (!quiet) loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), q: query.value, platform: platform.value, status: status.value })
    const data = await api(`/api/videos/records?${params}`)
    if (current === revision) { rows.value = data.items; total.value = data.total; error.value = '' }
  } catch (failure) { if (current === revision) error.value = failure instanceof Error ? failure.message : '历史加载失败。' }
  finally { if (current === revision) loading.value = false }
}
function filter() { page.value = 1; void load() }
async function copy(row: VideoHistoryRow) {
  try { await navigator.clipboard.writeText(row.url); copied.value = row.record_id }
  catch { error.value = '复制失败。' }
}
async function remove() {
  if (!deleting.value || busy.value) return
  busy.value = true
  try {
    const id = deleting.value.record_id
    await api(`/api/videos/records/${id}?cascade=${cascade.value}`, { method: 'DELETE' })
    deleting.value = null; emit('removed', id); await load()
    if (!rows.value.length && page.value > 1) { page.value--; await load() }
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '删除失败。' }
  finally { busy.value = false }
}
function duration(seconds?: number | null) { return seconds ? `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}` : '' }
const labels: Record<string, string> = { running: '解析中', finished: '成功', failed: '失败', cancelled: '已取消' }
onMounted(() => { void load(); timer = window.setInterval(() => { if (rows.value.some(row => row.status === 'running')) void load(true) }, 5000) })
onBeforeUnmount(() => { revision++; clearInterval(timer) })
defineExpose({ refresh: load })
</script>

<template>
  <section class="video-history" aria-label="解析历史">
    <form class="video-history-filters" @submit.prevent="filter">
      <input v-model="query" placeholder="搜索标题或链接" aria-label="搜索解析历史" maxlength="100" />
      <select v-model="platform" aria-label="筛选平台" @change="filter"><option value="">全部平台</option><option v-for="name in ['抖音', 'B站', 'YouTube', '小红书', '微博']" :key="name">{{ name }}</option></select>
      <select v-model="status" aria-label="筛选状态" @change="filter"><option value="">全部状态</option><option value="finished">成功</option><option value="failed">失败</option><option value="running">解析中</option></select>
      <button type="submit" class="video-icon" title="搜索" aria-label="搜索"><Search :size="18" /></button>
    </form>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <div v-if="loading" class="video-history-empty"><LoaderCircle :size="24" class="spin" /></div>
    <div v-else-if="!rows.length" class="video-history-empty">暂无解析记录</div>
    <div v-else class="video-history-list">
      <article v-for="row in rows" :key="row.record_id" class="video-history-row">
        <button class="video-history-cover" :aria-label="`打开 ${row.title}`" @click="emit('open', row.record_id)"><img v-if="row.thumbnail" :src="row.thumbnail" alt="" /><Video v-else :size="24" /></button>
        <div class="video-history-info"><button class="video-history-title" @click="emit('open', row.record_id)">{{ row.title }}</button><div class="video-history-meta"><span>{{ row.platform }}</span><span>{{ duration(row.duration) }}</span><time>{{ new Date(row.created_at * 1000).toLocaleString() }}</time><span :class="`video-status-${row.status}`">{{ labels[row.status] }}</span><span v-if="row.has_summary">总结</span><span v-if="row.has_mind_map">导图</span></div><p v-if="row.error" class="video-history-error">{{ row.error }}</p></div>
        <div class="video-history-actions"><button class="video-icon" title="打开记录" aria-label="打开记录" @click="emit('open', row.record_id)"><Eye :size="18" /></button><button class="video-icon" title="重新解析" aria-label="重新解析" @click="emit('reparse', row.url)"><RotateCcw :size="18" /></button><button class="video-icon" title="复制链接" aria-label="复制链接" @click="copy(row)"><Check v-if="copied === row.record_id" :size="18" /><Copy v-else :size="18" /></button><button class="video-icon" title="删除记录" aria-label="删除记录" @click="deleting = row; cascade = false"><Trash2 :size="18" /></button></div>
      </article>
    </div>
    <footer class="video-history-pagination"><span>共 {{ total }} 条</span><button class="video-icon" title="上一页" aria-label="上一页" :disabled="page <= 1" @click="page--; load()"><ChevronLeft :size="18" /></button><span>{{ page }} / {{ Math.max(1, Math.ceil(total / 20)) }}</span><button class="video-icon" title="下一页" aria-label="下一页" :disabled="page * 20 >= total" @click="page++; load()"><ChevronRight :size="18" /></button></footer>
    <Teleport to="body"><div v-if="deleting" class="account-dialog-backdrop" @click.self="!busy && (deleting = null)"><section class="account-dialog" role="dialog" aria-modal="true" aria-labelledby="video-delete-title"><header><h2 id="video-delete-title">删除解析记录</h2><button class="video-icon" title="关闭" aria-label="关闭" :disabled="busy" @click="deleting = null"><X :size="18" /></button></header><p class="video-delete-name">{{ deleting.title }}</p><label class="video-delete-option"><input v-model="cascade" type="checkbox" />同时删除关联的总结、导图和下载文件</label><p v-if="error" class="error-text" role="alert">{{ error }}</p><footer class="video-delete-actions"><button class="secondary-action" :disabled="busy" @click="deleting = null">取消</button><button class="primary-action" :disabled="busy" @click="remove"><Trash2 :size="16" />{{ busy ? '删除中' : '删除' }}</button></footer></section></div></Teleport>
  </section>
</template>
