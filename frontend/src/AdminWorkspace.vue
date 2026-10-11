<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ArrowLeft, ChevronLeft, ChevronRight, Download, KeyRound, LoaderCircle, Search, Settings2, ShieldCheck, Trash2, UserPlus, Users, Files, ScrollText, Activity, X, Save, Eye } from 'lucide-vue-next'
import { account, type Account } from './auth'
import { api, jsonRequest } from './musicLibrary'
const emit = defineEmits<{ back: [] }>()
type View = 'users' | 'data' | 'settings' | 'logs' | 'statistics'
const view = ref<View>('users')
const titles = { users: '用户管理', data: '业务数据', settings: '系统配置', logs: '操作日志', statistics: '运行统计' }
const icons = { users: Users, data: Files, settings: Settings2, logs: ScrollText, statistics: Activity }
const rows = ref<any[]>([])
const users = ref<Account[]>([])
const total = ref(0)
const page = ref(1)
const query = ref('')
const status = ref('')
const owner = ref('')
const kind = ref('file')
const busy = ref(false)
const loading = ref(false)
const error = ref('')
const notice = ref('')
const checked = ref<number[]>([])
const dialog = ref<'create' | 'password' | 'detail' | null>(null)
const target = ref<any>(null)
const username = ref('')
const password = ref('')
const role = ref<'user' | 'super_admin'>('user')
const settings = ref<any>(null)
const serviceKey = ref('')
const replaceKey = ref(false)
const stats = ref<any>(null)
const kinds: Record<string, string> = { file: '文件', image: '图片', thumbnail: '封面', summary_file: '总结文件', download: '下载任务', summary: 'AI 总结', mind_map: '思维导图', watermark: '去水印任务', batch: '批量任务', favorites: '收藏', history: '历史', playlists: '歌单', tracks: '歌曲', downloads: '音乐下载' }
const pages = computed(() => Math.max(1, Math.ceil(total.value / 20)))
let revision = 0
function fail(failure: unknown) { error.value = failure instanceof Error ? failure.message : '操作失败。' }
async function load() {
  const current = ++revision
  loading.value = true
  error.value = ''
  checked.value = []
  try {
    let result
    if (view.value === 'users') result = await api(`/api/admin/users?q=${encodeURIComponent(query.value)}&status=${status.value}&page=${page.value}`)
    else if (view.value === 'data') result = await api(`/api/admin/data?kind=${kind.value}&q=${encodeURIComponent(query.value)}&page=${page.value}${owner.value ? `&user_id=${owner.value}` : ''}`)
    else if (view.value === 'logs') result = await api(`/api/admin/logs?action=${encodeURIComponent(query.value)}&page=${page.value}${owner.value ? `&user_id=${owner.value}` : ''}`)
    else if (view.value === 'settings') result = await api('/api/admin/settings')
    else result = await api('/api/admin/statistics')
    if (current !== revision) return
    if (['users', 'data', 'logs'].includes(view.value)) { rows.value = result.items; total.value = result.total }
    else if (view.value === 'settings') { settings.value = result; replaceKey.value = false; serviceKey.value = '' }
    else stats.value = result
  } catch (failure) { if (current === revision) fail(failure) }
  finally { if (current === revision) loading.value = false }
}
function go(next: View) { view.value = next; page.value = 1; query.value = ''; rows.value = []; notice.value = '' }
async function action(fn: () => Promise<unknown>, message: string) {
  busy.value = true
  error.value = ''
  try { await fn(); notice.value = message; dialog.value = null; password.value = ''; await load(); await loadUsers() }
  catch (failure) { fail(failure) }
  finally { busy.value = false }
}
async function loadUsers() { users.value = (await api('/api/admin/users?page_size=100')).items }
function create() { username.value = ''; password.value = ''; role.value = 'user'; error.value = ''; dialog.value = 'create' }
function reset(row: Account) { target.value = row; password.value = ''; error.value = ''; dialog.value = 'password' }
function saveUser() {
  if (dialog.value === 'create') void action(() => api('/api/admin/users', jsonRequest('POST', { username: username.value, password: password.value, role: role.value })), '账号已创建，首次登录需修改临时密码。')
  else void action(() => api(`/api/admin/users/${target.value.id}/reset-password`, jsonRequest('POST', { password: password.value })), '密码已重置，全部会话已撤销。')
}
function update(row: Account, field: 'role' | 'status', event: Event) {
  const select = event.target as HTMLSelectElement
  const value = select.value
  select.value = row[field]
  if (value === row[field] || !confirm(`确认将 ${row.username} 的${field === 'role' ? '角色' : '状态'}改为 ${value === 'super_admin' ? '超级管理员' : value === 'user' ? '普通用户' : value === 'disabled' ? '停用' : '正常'}？`)) return
  void action(() => api(`/api/admin/users/${row.id}`, jsonRequest('PATCH', { [field]: value })), '账号已更新。')
}
function bulk(state: 'active' | 'disabled') {
  if (!checked.value.length || !confirm(`确认${state === 'disabled' ? '停用' : '恢复'}所选 ${checked.value.length} 个账号？`)) return
  void action(() => api('/api/admin/users/bulk', jsonRequest('PATCH', { ids: checked.value, status: state })), '所选账号已更新。')
}
function remove(row: any) {
  const label = view.value === 'users' ? row.username : row.label
  if (!confirm(`确认删除“${label}”？${view.value === 'users' ? '该账号的业务数据和文件也会删除。' : ''}`)) return
  void action(() => api(view.value === 'users' ? `/api/admin/users/${row.id}` : `/api/admin/data/${kind.value}/${encodeURIComponent(row.resource_key)}`, { method: 'DELETE' }), '已删除。')
}
async function download(row: any) {
  try {
    const response = await fetch(row.download_url)
    if (!response.ok) throw new Error((await response.json()).detail || '下载失败。')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url; link.download = row.filename || row.resource_key; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 10000)
  } catch (failure) { fail(failure) }
}
function saveSettings() {
  const data = { ...settings.value, ai_api_key: replaceKey.value ? serviceKey.value : null }
  void action(() => api('/api/admin/settings', jsonRequest('PUT', data)), '系统配置已保存。')
}
function filter() { page.value = 1; void load() }
watch(view, () => { void load() })
onMounted(() => { void load(); void loadUsers().catch(fail) })
</script>

<template>
  <div class="admin-workspace">
    <header class="admin-header"><button class="account-icon" title="返回音乐中心" aria-label="返回音乐中心" @click="emit('back')"><ArrowLeft :size="21" /></button><ShieldCheck :size="24" /><strong>VideoDream</strong><span>管理后台</span><small>{{ account?.username }}</small></header>
    <div class="admin-layout"><nav class="admin-nav" aria-label="管理后台"><button v-for="name in (Object.keys(titles) as View[])" :key="name" :class="{ active: view === name }" @click="go(name)"><component :is="icons[name]" :size="19" />{{ titles[name] }}</button></nav>
      <main class="admin-content">
        <div class="admin-title"><h1>{{ titles[view] }}</h1><button v-if="view === 'users'" class="account-primary" @click="create"><UserPlus :size="17" />创建用户</button></div>
        <p v-if="error" class="account-error" role="alert">{{ error }}</p><p v-if="notice" class="account-notice" role="status">{{ notice }}</p>
        <form v-if="['users', 'data', 'logs'].includes(view)" class="admin-filters" @submit.prevent="filter">
          <select v-if="view === 'users'" v-model="status" aria-label="账号状态筛选" @change="filter"><option value="">全部状态</option><option value="active">正常</option><option value="disabled">停用</option></select>
          <select v-if="view === 'data'" v-model="kind" aria-label="数据类型" @change="filter"><option v-for="(label, key) in kinds" :key="key" :value="key">{{ label }}</option></select>
          <select v-if="view !== 'users'" v-model="owner" aria-label="用户筛选" @change="filter"><option value="">全部用户</option><option v-for="user in users" :key="user.id" :value="String(user.id)">{{ user.username }}</option></select>
          <input v-model="query" :placeholder="view === 'users' ? '搜索用户名' : view === 'logs' ? '搜索操作类型' : '搜索名称'" aria-label="搜索后台记录" maxlength="80" /><button class="account-icon" title="搜索" aria-label="搜索" type="submit"><Search :size="19" /></button>
        </form>
        <div v-if="view === 'users' && checked.length" class="admin-bulk"><span>已选 {{ checked.length }} 项</span><button :disabled="busy" @click="bulk('disabled')">停用</button><button :disabled="busy" @click="bulk('active')">恢复</button></div>
        <div v-if="loading" class="account-loading"><LoaderCircle class="spin" :size="24" /></div>
        <template v-else-if="view === 'users'">
          <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th><input type="checkbox" aria-label="选择本页用户" :checked="rows.length > 0 && checked.length === rows.length" @change="checked = ($event.target as HTMLInputElement).checked ? rows.map(row => row.id) : []" /></th><th>用户名</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody><tr v-for="row in rows" :key="row.id"><td><input v-model="checked" type="checkbox" :value="row.id" :aria-label="`选择 ${row.username}`" /></td><td><strong>{{ row.username }}</strong><small v-if="row.must_change_password" class="admin-secondary">需修改密码</small></td><td><select :value="row.role" :disabled="busy" :aria-label="`${row.username} 角色`" @change="update(row, 'role', $event)"><option value="user">普通用户</option><option value="super_admin">超级管理员</option></select></td><td><select :value="row.status" :disabled="busy" :aria-label="`${row.username} 状态`" @change="update(row, 'status', $event)"><option value="active">正常</option><option value="disabled">停用</option></select></td><td>{{ new Date(row.created_at * 1000).toLocaleDateString() }}</td><td><div class="admin-actions"><button class="account-icon" :disabled="busy" :title="`重置 ${row.username} 密码`" :aria-label="`重置 ${row.username} 密码`" @click="reset(row)"><KeyRound :size="17" /></button><button class="account-icon danger" :disabled="busy" :title="`删除 ${row.username}`" :aria-label="`删除 ${row.username}`" @click="remove(row)"><Trash2 :size="17" /></button></div></td></tr><tr v-if="!rows.length"><td colspan="6">没有匹配的用户</td></tr></tbody></table></div>
        </template>
        <template v-else-if="view === 'data'">
          <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>名称</th><th>用户</th><th>状态 / 大小</th><th>操作</th></tr></thead><tbody><tr v-for="row in rows" :key="row.resource_key"><td class="admin-name">{{ row.label }}<small class="admin-secondary">{{ row.resource_key }}</small></td><td>{{ row.username }}</td><td>{{ row.payload?.status || (row.payload?.size ? `${(row.payload.size / 1048576).toFixed(1)} MB` : '已保存') }}</td><td><div class="admin-actions"><button class="account-icon" title="查看详情" aria-label="查看详情" @click="target = row; dialog = 'detail'"><Eye :size="17" /></button><button v-if="row.download_url" class="account-icon" title="下载文件" aria-label="下载文件" @click="download(row)"><Download :size="17" /></button><button class="account-icon danger" :disabled="busy" title="删除数据" aria-label="删除数据" @click="remove(row)"><Trash2 :size="17" /></button></div></td></tr><tr v-if="!rows.length"><td colspan="4">暂无数据</td></tr></tbody></table></div>
        </template>
        <template v-else-if="view === 'logs'">
          <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>对象</th><th>结果</th></tr></thead><tbody><tr v-for="row in rows" :key="row.id"><td>{{ new Date(row.created_at * 1000).toLocaleString() }}</td><td>{{ users.find(user => user.id === row.actor_id)?.username || row.actor_id || '访客' }}</td><td>{{ row.action }}</td><td class="admin-name">{{ row.target }}</td><td>{{ row.result }}</td></tr><tr v-if="!rows.length"><td colspan="5">暂无日志</td></tr></tbody></table></div>
        </template>
        <form v-else-if="view === 'settings' && settings" class="admin-settings" @submit.prevent="saveSettings">
          <h2>账号与业务</h2><label v-for="(label, key) in { registration_enabled: '公开注册', music_enabled: '音乐功能', video_enabled: '视频下载', summary_enabled: 'AI 总结与思维导图', watermark_enabled: '视频去水印' }" :key="key" class="admin-toggle"><span>{{ label }}</span><input v-model="settings[key]" type="checkbox" :aria-label="label" /></label>
          <h2>AI 服务</h2><label>服务地址<input v-model="settings.ai_base_url" type="url" required maxlength="2048" /></label><label>文字处理模型<input v-model="settings.ai_model" required maxlength="100" /></label><label>截图识别模型<input v-model="settings.ai_vision_model" maxlength="100" /></label><label>语音转写模型<input v-model="settings.ai_asr_model" maxlength="100" /></label><label class="admin-toggle"><span>替换服务密钥 <small>{{ settings.ai_api_key_masked || '未配置' }}</small></span><input v-model="replaceKey" type="checkbox" /></label><label v-if="replaceKey">新密钥<input v-model="serviceKey" type="password" autocomplete="off" maxlength="4096" /></label><button class="account-primary" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="17" /><Save v-else :size="17" />保存配置</button>
        </form>
        <template v-else-if="view === 'statistics' && stats"><div class="admin-metrics"><div><span>存储占用</span><strong>{{ (stats.storage_bytes / 1048576).toFixed(1) }} MB</strong></div><div><span>文件数</span><strong>{{ stats.file_count }}</strong></div><div><span>MySQL</span><strong>{{ stats.mysql }}</strong></div><div><span>Redis</span><strong>{{ stats.redis }}</strong></div></div><h2>账号状态</h2><table class="admin-table"><tbody><tr v-for="row in stats.users" :key="row.status"><td>{{ row.status === 'active' ? '正常' : '停用' }}</td><td>{{ row.count }}</td></tr></tbody></table><h2>任务状态</h2><table class="admin-table"><tbody><tr v-for="row in stats.tasks" :key="row.status"><td>{{ row.status || '未开始' }}</td><td>{{ row.count }}</td></tr><tr v-if="!stats.tasks.length"><td>暂无任务</td></tr></tbody></table><h2>资源统计</h2><table class="admin-table"><tbody><tr v-for="row in stats.resources" :key="row.kind"><td>{{ kinds[row.kind] || row.kind }}</td><td>{{ row.count }}</td></tr></tbody></table></template>
        <footer v-if="['users', 'data', 'logs'].includes(view)" class="admin-pagination"><span>共 {{ total }} 项</span><button class="account-icon" title="上一页" aria-label="上一页" :disabled="page <= 1 || loading" @click="page--; load()"><ChevronLeft :size="19" /></button><span>{{ page }} / {{ pages }}</span><button class="account-icon" title="下一页" aria-label="下一页" :disabled="page >= pages || loading" @click="page++; load()"><ChevronRight :size="19" /></button></footer>
      </main>
    </div>
    <div v-if="dialog" class="account-dialog-backdrop" @click.self="!busy && (dialog = null)" @keydown.esc="!busy && (dialog = null)"><section class="account-dialog" role="dialog" aria-modal="true" aria-labelledby="admin-dialog-title"><header><h2 id="admin-dialog-title">{{ dialog === 'create' ? '创建用户' : dialog === 'password' ? `重置 ${target.username} 密码` : '数据详情' }}</h2><button class="account-icon" title="关闭" aria-label="关闭" :disabled="busy" @click="dialog = null"><X :size="20" /></button></header><p v-if="error" class="account-error" role="alert">{{ error }}</p><pre v-if="dialog === 'detail'">{{ JSON.stringify(target, null, 2) }}</pre><form v-else @submit.prevent="saveUser"><label v-if="dialog === 'create'">用户名<input v-model="username" required minlength="3" maxlength="40" pattern="[A-Za-z0-9_][A-Za-z0-9_.-]{2,39}" autocomplete="off" /></label><label v-if="dialog === 'create'">角色<select v-model="role"><option value="user">普通用户</option><option value="super_admin">超级管理员</option></select></label><label>临时密码<input v-model="password" type="password" required minlength="8" maxlength="128" autocomplete="new-password" /></label><button class="account-primary" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="17" /><Save v-else :size="17" />保存</button></form></section></div>
  </div>
</template>
