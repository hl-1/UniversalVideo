<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, jsonRequest } from '../../shared/api/client'
type Item = { dimension: string; value: string; scope: string; context: string; preference: number; confidence: number; source: string; evidence_refs: string[]; support?: { recordings: number; days: number } }
type Profile = { agent_revision: number; agent_updated_at: number | null; learning_enabled: boolean; agent_enabled: boolean; agent_state: { summary: string; items: Item[]; hypotheses: Item[] }; latest_evaluation?: { run_id: string; status: string } | null; evidence_details: Record<string, { track?: { title: string; artist: string }; at?: number; weight?: number; context?: string }> }
type Version = { version: number; source: string; summary: string; created_at: number; evidence_summary: { references?: string[] } }
const emit = defineEmits<{ updated: [] }>()
const profile = ref<Profile | null>(null), busy = ref(false), message = ref(''), error = ref(''), showHistory = ref(false), showCorrection = ref(false)
const versions = ref<Version[]>([]), before = ref<number | null>(null)
const draft = ref({ dimension: 'language', value: '', scope: 'long_term', context: 'general', preference: .75 })
const dimensions: Record<string, string> = { language: '语言', artist: '歌手', vocal_role: '人声', moods: '情绪', themes: '主题', tempo: '节奏', energy: '能量', style: '风格', instrument: '乐器' }
const scopes: Record<string, string> = { long_term: '长期偏好', recent: '近期兴趣', context: '场景偏好' }
const contexts: Record<string, string> = { general: '通用', focus: '专注', commute: '通勤', exercise: '运动', sleep: '睡前' }
const sources: Record<string, string> = { agent: '自动评估', user: '用户确认', reset: '清除推断', restore: '历史恢复' }
watch(() => draft.value.scope, scope => { if (scope === 'context' && draft.value.context === 'general') draft.value.context = 'focus' })
function itemValue(item: Item) { return item.dimension === 'vocal_role' ? ({ lead: '主要人声', background: '少量人声', instrumental: '纯音乐' }[item.value] || item.value) : item.value }
const working = computed(() => ['pending', 'running'].includes(profile.value?.latest_evaluation?.status || ''))
function date(value: number) { return new Date(value * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }
function evidenceLabel(reference: string) {
  const evidence = profile.value?.evidence_details[reference]
  if (!evidence) return '已保存的历史证据'
  return `${evidence.track?.title || '歌曲'} · ${evidence.track?.artist || '未知歌手'} · ${reference.startsWith('favorite:') ? '收藏' : reference.startsWith('seed:') ? '种子歌曲' : (evidence.weight || 0) < 0 ? '主动跳过' : '有效收听或主动操作'}${evidence.at ? ` · ${date(evidence.at)}` : ''}`
}
async function load() { profile.value = await api<Profile>('/api/music/discovery/profile') }
async function action(operation: () => Promise<unknown>, success: string) {
  if (busy.value) return
  busy.value = true; error.value = ''; message.value = ''
  try { await operation(); await load(); message.value = success; emit('updated') }
  catch (e) { error.value = e instanceof Error ? e.message : '操作失败。'; await load().catch(() => {}) }
  finally { busy.value = false }
}
function toggleLearning() { void action(() => api('/api/music/discovery/profile', jsonRequest('PATCH', { profile_learning_enabled: !profile.value?.learning_enabled })), '学习设置已更新。') }
function evaluate() { void action(async () => {
  const result = await api<{ status: string }>('/api/music/discovery/profile/evaluations', { ...jsonRequest('POST', {}), headers: { ...(jsonRequest('POST', {}).headers as Record<string, string>), 'Idempotency-Key': crypto.randomUUID() } })
  if (result.status === 'disabled') throw new Error('画像自动评估暂未开放，明确偏好仍会立即生效。')
  if (result.status === 'insufficient_evidence') throw new Error('有效证据还不足，请继续收藏和聆听。')
}, '评估请求已安排，当前推荐可以继续播放。') }
function confirmItem(item: Item) { void correct(item) }
async function correct(item?: Item) {
  const value = item || { ...draft.value, context: draft.value.scope === 'context' ? draft.value.context : 'general', evidence_refs: [] }
  await action(() => api('/api/music/discovery/profile/corrections', jsonRequest('POST', { expected_agent_revision: profile.value!.agent_revision,
    item: { dimension: value.dimension, value: value.value, scope: value.scope, context: value.context, preference: value.preference, evidence_refs: [] } })), '偏好已确认，下批生效。')
}
function edit(item: Item) { draft.value = { dimension: item.dimension, value: item.value, scope: item.scope, context: item.context, preference: item.preference }; showCorrection.value = true }
function reset() { if (confirm('清除自动推断并重新学习？明确设置与屏蔽记录将保留。')) void action(() => api('/api/music/discovery/profile/reset', jsonRequest('POST', { expected_agent_revision: profile.value!.agent_revision })), '推断已清除，开始新的学习区间。') }
async function history(more = false) {
  try {
    const result = await api<{ items: Version[]; next_before: number | null }>(`/api/music/discovery/profile/versions${more && before.value ? `?before=${before.value}` : ''}`)
    versions.value = more ? [...versions.value, ...result.items] : result.items; before.value = result.next_before; showHistory.value = true
  } catch (e) { error.value = String(e) }
}
function restore(version: number) { if (confirm(`恢复版本 ${version} 的自动推断？将结合当前明确偏好生成新版本。`)) void action(() => api('/api/music/discovery/profile/restore', jsonRequest('POST', { expected_agent_revision: profile.value!.agent_revision, version })), '已恢复历史推断，下批生效。') }
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { void load().catch(e => error.value = String(e)); timer = setInterval(() => { if (working.value) void load().catch(() => {}) }, 5000) })
onUnmounted(() => clearInterval(timer))
</script>
<template>
  <section class="music-profile" aria-label="我的音乐画像">
    <header><div><h3>我的音乐画像</h3><p>从收藏与有效收听中了解你的偏好，你可以随时纠正。</p></div><button :disabled="busy" @click="toggleLearning">{{ profile?.learning_enabled ? '暂停学习' : '恢复学习' }}</button></header>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
    <template v-if="profile">
      <p>{{ profile.agent_state.summary || '继续聆听和收藏，逐步建立有依据的音乐画像。' }}</p>
      <small>版本 {{ profile.agent_revision }} · {{ profile.agent_updated_at ? date(profile.agent_updated_at) : '尚无自动推断' }} · {{ profile.learning_enabled ? '学习中' : '学习已暂停' }}<span v-if="!profile.agent_enabled"> · 自动评估暂未开放</span></small>
      <div class="profile-groups"><section v-for="(label, scope) in scopes" :key="scope"><h4>{{ label }}</h4><ul>
        <li v-for="item in profile.agent_state.items.filter(i => i.scope === scope)" :key="`${item.dimension}:${item.value}:${item.context}`">
          <strong>{{ dimensions[item.dimension] }} · {{ itemValue(item) }}</strong><span v-if="item.scope === 'context'">{{ contexts[item.context] }}</span>
          <span>{{ item.preference < 0 ? '减少推荐' : '偏好' }} · {{ item.source === 'user' ? '已确认' : `可信度 ${Math.round(item.confidence * 100)}%` }}</span>
          <small v-if="item.support?.recordings">依据 {{ item.support.recordings }} 首录音、{{ item.support.days }} 天的行为</small>
          <details v-if="item.evidence_refs.length"><summary>查看证据</summary><p v-for="reference in item.evidence_refs" :key="reference">{{ evidenceLabel(reference) }}</p></details>
          <div class="profile-actions"><button v-if="item.source !== 'user'" :disabled="busy" @click="confirmItem(item)">符合我的偏好</button><button :disabled="busy" @click="edit(item)">纠正</button></div>
        </li><li v-if="!profile.agent_state.items.some(i => i.scope === scope)" class="profile-empty">尚无足够依据</li>
      </ul></section></div>
      <details v-if="profile.agent_state.hypotheses.length"><summary>待观察的偏好（{{ profile.agent_state.hypotheses.length }}）</summary><div v-for="item in profile.agent_state.hypotheses" :key="`${item.dimension}:${item.value}:${item.scope}:${item.context}`" class="profile-hypothesis"><span>{{ dimensions[item.dimension] }} · {{ itemValue(item) }} · 证据仍不足</span><button :disabled="busy" @click="confirmItem(item)">确认偏好</button><button :disabled="busy" @click="edit(item)">纠正</button></div></details>
      <div class="profile-actions"><button @click="showCorrection = !showCorrection">添加明确偏好</button><button :disabled="busy || working || !profile.learning_enabled || !profile.agent_enabled" @click="evaluate">{{ working ? '正在评估' : '重新评估' }}</button><button @click="history()">变更历史</button><button :disabled="busy" @click="reset">清除推断</button></div>
      <form v-if="showCorrection" class="profile-correction" @submit.prevent="correct()"><h4>告诉我们你的偏好</h4>
        <label>偏好维度<select v-model="draft.dimension"><option v-for="(label, value) in dimensions" :key="value" :value="value">{{ label }}</option></select></label>
        <label>偏好内容<select v-if="draft.dimension === 'vocal_role'" v-model="draft.value" required><option value="lead">主要人声</option><option value="background">少量人声</option><option value="instrumental">纯音乐</option></select><input v-else v-model="draft.value" maxlength="100" required placeholder="例如：中文、轻快、某位歌手" /></label>
        <label>作用范围<select v-model="draft.scope"><option v-for="(label, value) in scopes" :key="value" :value="value">{{ label }}</option></select></label>
        <label v-if="draft.scope === 'context'">听歌场景<select v-model="draft.context" required><option v-for="(label, value) in contexts" v-show="value !== 'general'" :key="value" :value="value">{{ label }}</option></select></label>
        <label>偏好强度 {{ draft.preference }}<input v-model.number="draft.preference" type="range" min="-1" max="1" step="0.05" /></label><button :disabled="busy">保存偏好</button>
      </form>
    </template>
    <div v-if="showHistory" class="profile-history" role="dialog" aria-modal="true" aria-label="画像变更历史" @keydown.esc="showHistory = false"><header><h3>画像变更历史</h3><button @click="showHistory = false">关闭</button></header><ol><li v-for="version in versions" :key="version.version"><strong>版本 {{ version.version }} · {{ sources[version.source] || version.source }}</strong><p>{{ version.summary }}</p><small>{{ date(version.created_at) }}</small><p v-if="version.evidence_summary.references?.length">{{ version.evidence_summary.references.length }} 条证据摘要</p><button :disabled="busy" @click="restore(version.version)">恢复此版本的推断</button></li></ol><button v-if="before" @click="history(true)">加载更早记录</button><p v-if="!versions.length">尚无变更记录</p></div>
  </section>
</template>
<style scoped>
.music-profile { margin-block: 24px; padding: 20px; border: 1px solid var(--music-line); border-radius: 16px; color: var(--mc-ink); overflow-wrap: anywhere; }
header, .profile-actions, .profile-hypothesis { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
h3, h4 { margin: 0 0 10px; } p, small { color: var(--mc-muted); line-height: 1.7; }
.profile-groups { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-block: 20px; }
ul, ol { list-style: none; padding: 0; } li { border-bottom: 1px solid var(--music-line); padding: 14px 0; } li > span, li > small { display: block; margin-top: 6px; }
button, select, input { min-height: 44px; border: 1px solid var(--music-line); border-radius: 8px; padding: 8px 12px; background: var(--music-surface); color: var(--mc-ink); max-width: 100%; box-sizing: border-box; }
button { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }.profile-actions { justify-content: flex-start; margin-block: 12px; }
.profile-correction { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 12px; } .profile-correction label { display: grid; gap: 8px; }.profile-correction h4 { grid-column: 1/-1; }
.profile-history { position: fixed; inset: 10% 10%; z-index: 120; padding: 24px; overflow: auto; background: var(--music-surface); border: 1px solid var(--music-line); border-radius: 18px; box-shadow: 0 15px 80px #0005; }
@media(max-width:760px) { .music-profile { padding: 16px 12px; }.profile-groups, .profile-correction { grid-template-columns: minmax(0,1fr); }.profile-history { inset: 15% 0 0; border-radius: 18px 18px 0 0; padding: 20px 16px max(20px,env(safe-area-inset-bottom)); } }
</style>
