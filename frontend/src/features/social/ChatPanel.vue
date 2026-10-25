<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { account } from '../accounts/auth'
import { request, failure, msgId, socialEvent, upload, assetUrl, socialNavigation } from './social'
import { playTrack } from '../music/player/musicPlayer'
const props = defineProps<{ conversationId?: string; embedded?: boolean }>()
const conversations = ref<any[]>([]), selected = ref(''), detail = ref<any>(null), messages = ref<any[]>([])
const text = ref(''), query = ref(''), error = ref(''), busy = ref(false), reply = ref<any>(null)
const list = ref<HTMLElement | null>(null), attachment = ref<HTMLInputElement | null>(null)
const shareToken = ref('')
const share = ref<any>(null), shareError = ref(''), forward = ref<any>(null), forwardTarget = ref('')
const pending = ref<any>(null), hasMore = ref(false)
const recording = ref(false)
const typing = ref(false)
let typingTimer: ReturnType<typeof setTimeout> | undefined, lastTyping = 0
let recorder: MediaRecorder | undefined, stream: MediaStream | undefined, timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
const canManage = computed(() => ['owner', 'admin'].includes(detail.value?.membership?.role))
async function refresh() { conversations.value = (await request('/conversations')).items }
async function open(id: string) {
  selected.value = id; const version = ++generation; messages.value = []; error.value = ''; reply.value = null; query.value = ''
  try {
    const info = await request(`/conversations/${id}`)
    if (version !== generation) return
    detail.value = info; await loadMessages()
  } catch (e) { error.value = failure(e) }
}
async function loadMessages(older = false) {
  const id = selected.value, version = generation
  if (!id) return
  const suffix = new URLSearchParams()
  if (older && messages.value.length) suffix.set('before', String(messages.value[0].seq))
  if (query.value) suffix.set('q', query.value)
  const data = await request(`/conversations/${id}/messages?${suffix}`)
  if (version !== generation) return
  messages.value = older ? [...data.items, ...messages.value] : data.items
  hasMore.value = data.has_more
  if (!older && !query.value) {
    await request(`/conversations/${id}/read`, 'PUT', { seq: data.last_seq })
    await nextTick(); if (list.value) list.value.scrollTop = list.value.scrollHeight
  }
}
async function send(payload?: any) {
  if (busy.value || !selected.value || !payload && !text.value.trim()) return
  busy.value = true; error.value = ''
  const id = selected.value
  const body = payload || { client_msg_id: msgId(), msg_type: 'text', text: text.value, reply_to_seq: reply.value?.seq }
  pending.value = { id, body }
  try {
    await request(`/conversations/${id}/messages`, 'POST', body)
    pending.value = null; text.value = ''; reply.value = null
    if (selected.value === id) await loadMessages()
    await refresh()
  } catch (e) { error.value = failure(e) } finally { busy.value = false }
}
async function retry() { if (pending.value?.id === selected.value) await send(pending.value.body) }
async function command(path: string, body: any = {}, method = 'POST') {
  error.value = ''
  try { await request(path, method, body); await refresh(); if (selected.value) { detail.value = await request(`/conversations/${selected.value}`); await loadMessages() } } catch (e) { error.value = failure(e) }
}
async function changeMember(action: string, user_id?: string) {
  if (action === 'leave') {
    const id = selected.value; selected.value = ''; detail.value = null
    await command(`/conversations/${id}/members`, { action, user_id }); return
  }
  await command(`/conversations/${selected.value}/members`, { action, user_id })
  if (action === 'leave') { selected.value = ''; detail.value = null }
}
async function configure() {
  const title = window.prompt('群名称', detail.value.title)
  if (!title) return
  const announcement = window.prompt('群公告', detail.value.settings.announcement || '')
  if (announcement !== null) await command(`/conversations/${selected.value}`, { title, announcement }, 'PATCH')
}
async function attach(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const id = selected.value, version = generation
  error.value = ''
  try { const kind = file.type.startsWith('image/') ? 'image' : 'file'; const asset = await upload(file, kind); if (id === selected.value && version === generation) await send({ client_msg_id: msgId(), msg_type: kind, asset_id: asset.id, text: '' }) } catch (e) { error.value = failure(e) }
  if (attachment.value) attachment.value.value = ''
}
async function startRecording() {
  error.value = ''
  try {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') throw new Error('当前浏览器不支持录音，请使用 HTTPS 或本机浏览器。')
    stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    const chunks: BlobPart[] = [], id = selected.value
    const capturedStream = stream, capturedRecorder = new MediaRecorder(stream), version = generation
    recorder = capturedRecorder
    capturedRecorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data) }
    capturedRecorder.onstop = async () => {
      capturedStream.getTracks().forEach(track => track.stop()); recording.value = false
      if (id !== selected.value || version !== generation) return
      try { const asset = await upload(new Blob(chunks, { type: capturedRecorder.mimeType || 'audio/webm' }), 'voice'); if (version === generation && id === selected.value) await send({ client_msg_id: msgId(), msg_type: 'voice', asset_id: asset.id }) } catch (e) { error.value = failure(e) }
    }
    recorder.start(); recording.value = true; timer = setTimeout(stopRecording, 120000)
  } catch (e) { error.value = failure(e) }
}
function stopRecording() { clearTimeout(timer); if (recorder?.state === 'recording') recorder.stop() }
async function showShare(id: string, token = '') { shareToken.value = token; shareError.value = ''; try { share.value = await request(`/shares/${id}${token ? `?token=${encodeURIComponent(token)}` : ''}`) } catch (e) { shareError.value = failure(e) } }
async function copyShare() { try { const result = await request(`/shares/${share.value.id}/copy${shareToken.value ? `?token=${encodeURIComponent(shareToken.value)}` : ''}`, 'POST', {}); shareError.value = `已复制 ${result.copied} 首歌曲；${result.information_only} 首仅分享信息。` } catch (e) { shareError.value = failure(e) } }
async function reportMessage(item: any) { const reason = window.prompt('举报原因'); if (reason?.trim()) { try { await request('/reports', 'POST', { target_kind: 'message', target_key: `${selected.value}:${item.seq}`, reason }); error.value = '举报已提交管理员处理。' } catch (e) { error.value = failure(e) } } }
function navigateShare() { const snapshot = share.value.snapshot; socialNavigation.value = { tab: snapshot.rule_id ? 'rules' : snapshot.performance_id ? 'duets' : snapshot.kind === 'listen' ? 'listen' : 'karaoke', id: snapshot.rule_id || snapshot.performance_id || snapshot.room_id, code: snapshot.code }; share.value = null }
function copyText(value: string) { void navigator.clipboard?.writeText(value).catch(e => { error.value = failure(e) }) }
async function forwardMessage() {
  if (!forward.value || !forwardTarget.value) return
  const item = forward.value
  try {
    await request(`/conversations/${forwardTarget.value}/messages`, 'POST', { client_msg_id: msgId(), msg_type: item.msg_type, text: item.body.text || '', share_id: item.body.share_id })
    forward.value = null
  } catch (e) { error.value = failure(e) }
}
watch(() => props.conversationId, value => { if (value) void open(value) })
watch(text, value => { if (value && selected.value && Date.now() - lastTyping > 4000) { lastTyping = Date.now(); void request(`/conversations/${selected.value}/typing`, 'POST', {}).catch(() => {}) } })
watch(socialEvent, event => {
  if (!event || event.topic === 'read') return
  if (event.topic === 'typing' && event.object_key === selected.value && event.user_id !== account.value?.id) { typing.value = true; clearTimeout(typingTimer); typingTimer = setTimeout(() => { typing.value = false }, 5000) }
  if (['reconnected', 'message', 'recall', 'conversation'].includes(event.topic)) {
    void refresh().catch(() => {})
    if (selected.value && (event.object_key === selected.value || event.topic === 'reconnected')) void loadMessages().catch(e => { error.value = failure(e) })
  }
  if (event.topic === 'share_revoked' && share.value?.id === event.object_key) { share.value = null; shareError.value = '分享已撤销。' }
})
onMounted(async () => { try { await refresh(); if (props.conversationId) await open(props.conversationId); const params = new URLSearchParams(location.search); if (params.get('social_share')) await showShare(params.get('social_share')!, params.get('token') || '') } catch (e) { error.value = failure(e) } })
onBeforeUnmount(() => { generation++; selected.value = ''; clearTimeout(typingTimer); stopRecording(); stream?.getTracks().forEach(track => track.stop()) })
</script>
<template>
  <section class="social-chat" :class="{ 'has-conversation': selected, 'social-chat-embedded': embedded }">
    <aside v-if="!embedded" class="social-conversations"><h2>消息</h2><button v-for="item in conversations" :key="item.id" :class="{ selected: selected === item.id }" @click="item.member_status === 'active' ? open(item.id) : command(`/conversations/${item.id}/members`, { action: 'accept' })"><span class="social-avatar">{{ (item.title || item.peer_name || '聊').slice(0, 1) }}</span><span><strong>{{ item.title || item.peer_name || '好友私聊' }}</strong><small>{{ item.member_status === 'invited' ? '点击接受群邀请' : item.muted_until ? '免打扰' : item.kind === 'direct' ? '私聊' : '群聊' }}</small></span><b v-if="item.unread">{{ item.unread }}</b></button><p v-if="!conversations.length" class="social-empty">添加好友，开始第一段音乐对话。</p></aside>
    <div class="social-chat-main">
      <template v-if="selected && detail">
        <header class="social-chat-header"><button class="social-mobile-back" @click="selected = ''; detail = null">返回</button><div><strong>{{ detail.title || conversations.find(x => x.id === selected)?.peer_name || '私聊' }}</strong><small>{{ detail.settings.announcement || `${detail.members.filter((x: any) => x.status === 'active').length} 位成员` }}</small></div><button @click="command(`/conversations/${selected}`, { pinned: !detail.membership.pinned }, 'PATCH')">{{ detail.membership.pinned ? '取消置顶' : '置顶' }}</button><button @click="command(`/conversations/${selected}`, { muted: !detail.membership.muted_until }, 'PATCH')">{{ detail.membership.muted_until ? '取消免打扰' : '免打扰' }}</button><button v-if="canManage && detail.kind === 'group'" @click="configure">群设置</button></header>
        <details v-if="detail.kind === 'group'" class="social-member-list"><summary>群成员管理</summary><div v-for="person in detail.members" :key="person.user_id" class="social-row"><span>{{ person.username }} · {{ person.role }} · {{ person.status }}</span><template v-if="person.user_id !== account?.id && person.status === 'active' && canManage"><button @click="changeMember('remove', person.user_id)">移除</button><button v-if="detail.membership.role === 'owner'" @click="changeMember(person.role === 'admin' ? 'member' : 'admin', person.user_id)">切换管理员</button><button v-if="detail.membership.role === 'owner'" @click="changeMember('transfer', person.user_id)">转让群主</button></template></div><button @click="changeMember('leave')">退出群聊</button></details>
        <form class="social-row social-search" @submit.prevent="loadMessages()"><input v-model="query" placeholder="搜索聊天记录" aria-label="搜索聊天记录" maxlength="100" /><button>搜索</button><button type="button" @click="query = ''; loadMessages()">重置</button><button type="button" @click="command(`/conversations/${selected}`, { clear: true }, 'PATCH')">清空我的记录</button></form>
        <div ref="list" class="social-messages" aria-live="polite"><button v-if="hasMore" @click="loadMessages(true)">加载更早消息</button><article v-for="item in messages" :key="item.seq" class="social-message" :class="{ mine: item.sender_id === account?.id }"><small>{{ item.sender_name || (item.sender_id === account?.id ? account?.username : '已注销用户') }} · {{ new Date(item.created_at * 1000).toLocaleTimeString() }}</small><p v-if="item.status === 'recalled'" class="social-muted">消息已撤回</p><template v-else><small v-if="item.reply_to_seq">回复 #{{ item.reply_to_seq }}</small><p v-if="item.body.text" class="social-bubble">{{ item.body.text }}</p><img v-if="item.msg_type === 'image'" class="social-attachment-image" :src="assetUrl(item.body.asset_id)" alt="聊天图片" /><audio v-if="item.msg_type === 'voice'" controls :src="assetUrl(item.body.asset_id)" preload="none" /><a v-if="item.msg_type === 'file'" :href="assetUrl(item.body.asset_id)" target="_blank" rel="noopener">{{ item.body.filename }}</a><button v-if="item.msg_type === 'share'" class="social-share-card" @click="showShare(item.body.share_id)">♫ {{ item.body.title }}<small>查看音乐分享</small></button><div class="social-message-actions"><button @click="reportMessage(item)">举报</button><button @click="reply = item">回复</button><button v-if="item.body.text" @click="copyText(item.body.text)">复制</button><button v-if="['text', 'share'].includes(item.msg_type)" @click="forward = item">转发</button><button v-if="item.sender_id === account?.id && Date.now() / 1000 - item.created_at < 120" @click="command(`/conversations/${selected}/messages/${item.seq}/recall`)">撤回</button><button v-if="item.body.share_id && item.sender_id === account?.id" @click="command(`/shares/${item.body.share_id}`, {}, 'DELETE')">撤销分享</button></div></template></article><p v-if="!messages.length" class="social-empty">从一句问候或一首歌开始。</p></div>
        <p v-if="typing" class="social-muted">对方正在输入…</p><form class="social-composer" @submit.prevent="send()"><p v-if="reply" class="social-muted">回复 #{{ reply.seq }} <button type="button" @click="reply = null">取消</button></p><textarea v-model="text" aria-label="消息内容" placeholder="聊聊此刻的音乐…" maxlength="8000" @keydown.enter.exact.prevent="send()" /><div class="social-row"><input ref="attachment" type="file" hidden @change="attach" /><button type="button" :disabled="busy" @click="attachment?.click()">图片 / 文件</button><button type="button" :disabled="busy" @click="recording ? stopRecording() : startRecording()">{{ recording ? '结束录音' : '语音' }}</button><button type="button" @click="text += ' 🎵'">🎵</button><span class="social-spacer" /><button class="primary" :disabled="busy || !text.trim()">{{ busy ? '发送中…' : '发送' }}</button></div></form>
      </template><div v-else class="social-empty">选择会话，和朋友分享正在听的歌。</div>
      <p v-if="error" class="social-error" role="alert">{{ error }} <button v-if="pending" @click="retry">重试发送</button></p>
    </div>
    <div v-if="share || shareError" class="social-dialog-backdrop" @click.self="share = null; shareError = ''"><div class="social-dialog" role="dialog" aria-modal="true" aria-label="音乐分享"><template v-if="share"><h2>{{ share.snapshot.title }}</h2><p>{{ share.snapshot.description }}</p><div v-for="(track, index) in share.snapshot.tracks || []" :key="index" class="social-row"><span>{{ track.title }} · {{ track.artist }}</span><button v-if="['qq', 'netease', 'kugou', 'video'].includes(track.provider) && track.webpage_url" @click="playTrack(track)">播放</button><audio v-if="track.asset_id" controls :src="assetUrl(track.asset_id) + '?share_id=' + share.id + (shareToken ? '&token=' + encodeURIComponent(shareToken) : '')" preload="none" /></div><button v-if="['rule', 'room', 'performance'].includes(share.target_kind)" @click="navigateShare">打开详情</button><audio v-if="share.target_kind === 'performance' && share.snapshot.asset_id" controls :src="assetUrl(share.snapshot.asset_id)" /><button v-if="['track', 'playlist'].includes(share.target_kind)" @click="copyShare">复制到我的歌单</button></template><p v-if="shareError" role="status">{{ shareError }}</p><button @click="share = null; shareError = ''">关闭</button></div></div>
    <div v-if="forward" class="social-dialog-backdrop"><form class="social-dialog" @submit.prevent="forwardMessage"><h2>转发消息</h2><select v-model="forwardTarget" required><option value="" disabled>选择会话</option><option v-for="item in conversations.filter(x => x.member_status === 'active')" :key="item.id" :value="item.id">{{ item.title || item.peer_name }}</option></select><button class="primary">转发</button><button type="button" @click="forward = null">取消</button></form></div>
  </section>
</template>
