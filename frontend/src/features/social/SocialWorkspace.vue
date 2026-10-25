<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { account } from '../accounts/auth'
import { request, failure, startSocial, connectionState, socialEvent, socialNavigation, unread, type SocialTab } from './social'
import ChatPanel from './ChatPanel.vue'
import RoomsPanel from './RoomsPanel.vue'
import RulesPanel from './RulesPanel.vue'
import PerformancesPanel from './PerformancesPanel.vue'
import PlaylistsPanel from './PlaylistsPanel.vue'
import './social.css'
const props = defineProps<{ initialTab?: SocialTab }>()
const tab = ref<SocialTab>(props.initialTab || 'messages'), target = ref(''), targetCode = ref('')
const capability = ref<any>(null), relations = ref<any[]>([]), results = ref<any[]>([]), notifications = ref<any[]>([])
const query = ref(''), message = ref(''), error = ref(''), busy = ref(false), groupName = ref(''), groupMembers = ref<string[]>([]), settings = ref<any>(null)
const labels: Record<SocialTab, string> = { messages: '消息', friends: '好友', listen: '一起听', karaoke: 'K 歌房', rules: '规则广场', duets: '我的合唱', playlists: '共同歌单', settings: '隐私设置' }
const friends = computed(() => relations.value.filter(x => x.user_id === account.value?.id && x.kind === 'friend' && x.status === 'accepted'))
const incoming = computed(() => relations.value.filter(x => x.target_user_id === account.value?.id && x.kind === 'friend' && x.status === 'pending'))
const blocks = computed(() => relations.value.filter(x => x.user_id === account.value?.id && x.kind === 'block'))
const presence = ref<Record<string, any>>({})
const accountCopyStatus = ref(''), copyingAccount = ref(false)
async function copyAccount() {
  const username = account.value?.username
  if (!username || copyingAccount.value) return
  copyingAccount.value = true
  accountCopyStatus.value = ''
  try {
    await navigator.clipboard.writeText(username)
    accountCopyStatus.value = '账号已复制'
  } catch {
    accountCopyStatus.value = '复制失败，请选中或长按账号手动复制。'
  } finally { copyingAccount.value = false }
}
async function refreshPresence() {
  if (tab.value !== 'friends') return
  const entries = await Promise.all(friends.value.map(async friend => [friend.target_user_id, await request(`/presence/${friend.target_user_id}`).catch(() => null)]))
  presence.value = Object.fromEntries(entries)
}
const presenceTimer = setInterval(() => { void refreshPresence() }, 15000)
watch(tab, () => { void refreshPresence() })
watch(friends, () => { void refreshPresence() })
onBeforeUnmount(() => clearInterval(presenceTimer))
function go(next: SocialTab, id = '', code = '') { target.value = id; targetCode.value = code; tab.value = next; error.value = '' }
async function refreshFriends() { const [a, b] = await Promise.all([request('/relations'), request('/notifications')]); relations.value = a.items; notifications.value = b.items }
async function execute(fn: () => Promise<void>) { if (busy.value) return; busy.value = true; error.value = ''; try { await fn() } catch (e) { error.value = failure(e) } finally { busy.value = false } }
async function search() { await execute(async () => { results.value = (await request(`/users?q=${encodeURIComponent(query.value)}`)).items }) }
async function relation(user: string, action: string) { await execute(async () => { const remark = action === 'remark' ? window.prompt('好友备注') : ''; if (remark === null) return; await request(`/relations/${user}`, 'POST', { action, message: message.value, remark }); await refreshFriends() }) }
async function chat(user: string) { await execute(async () => { const data = await request('/conversations', 'POST', { kind: 'direct', members: [user] }); go('messages', data.id) }) }
async function createGroup() { await execute(async () => { const data = await request('/conversations', 'POST', { kind: 'group', title: groupName.value, members: groupMembers.value }); go('messages', data.id) }) }
async function openNotification(item: any) {
  await request(`/notifications/${item.id}/read`, 'PUT', {})
  const data = item.payload
  if (data.room_id) go(data.kind && data.kind !== 'listen' ? 'karaoke' : 'listen', data.room_id, data.code || '')
  else if (data.performance_id) go('duets', item.event_type === 'duet_invite' ? '' : data.performance_id, data.code || '')
  else if (data.rule_id) go('rules', data.rule_id)
  else if (data.conversation_id) go('messages', data.conversation_id)
  await refreshFriends()
}
async function saveSettings() { await execute(async () => { await request('/settings', 'PUT', { discoverable: Boolean(settings.value.discoverable), online_visibility: settings.value.online_visibility, listening_visibility: settings.value.listening_visibility, dm_policy: 'friends', invite_policy: settings.value.invite_policy }); settings.value = await request('/settings') }) }
watch(socialNavigation, next => { if (next) { go(next.tab, next.id, next.code); socialNavigation.value = null } })
watch(socialEvent, next => { if (['relations', 'notification', 'reconnected'].includes(next?.topic)) void refreshFriends().catch(() => {}) })
onMounted(async () => { try { capability.value = await request('/capabilities'); if (capability.value.enabled) { startSocial(); await refreshFriends(); settings.value = await request('/settings') }; if (socialNavigation.value) { const next = socialNavigation.value; go(next.tab, next.id, next.code); socialNavigation.value = null } } catch (e) { error.value = failure(e) } })
</script>
<template>
  <div class="social-workspace"><header class="social-heading"><div><span class="social-eyebrow">把音乐分享给身边的人</span><h1>音乐社交</h1></div><span class="social-connection" :class="connectionState">{{ connectionState === 'online' ? '已连接' : connectionState === 'connecting' ? '正在连接' : '连接恢复中' }}</span></header><p v-if="error" class="social-error" role="alert">{{ error }}</p><p v-if="capability && !capability.enabled" class="social-empty">社交功能已由管理员关闭。</p><template v-else-if="capability"><nav class="social-tabs" aria-label="音乐社交"><button v-for="(label, key) in labels" :key="key" :class="{ selected: tab === key }" :aria-current="tab === key ? 'page' : undefined" @click="go(key)">{{ label }}<b v-if="key === 'messages' && unread">{{ unread }}</b><b v-if="key === 'friends' && incoming.length">{{ incoming.length }}</b></button></nav>
      <ChatPanel v-if="tab === 'messages'" :key="target || 'chat'" :conversation-id="target" />
      <RoomsPanel v-if="tab === 'listen' || tab === 'karaoke'" :key="tab + target + targetCode" :kind="tab === 'listen' ? 'listen' : 'karaoke'" :room-id="target" :code="targetCode" :rtc-available="capability.rtc" :duet-enabled="capability.realtime_duet" />
      <RulesPanel v-if="tab === 'rules'" :key="target || 'rules'" :rule-id="target" />
      <PerformancesPanel v-if="tab === 'duets'" :key="target + targetCode" :performance-id="target" :code="targetCode" />
      <PlaylistsPanel v-if="tab === 'playlists'" />
      <section v-if="tab === 'friends'" class="social-stack">
        <section v-if="account" class="social-card" aria-labelledby="social-my-account">
          <h2 id="social-my-account">我的账号</h2>
          <div class="social-row social-account-row"><label class="social-account-field">登录账号<input :value="account.username" readonly :spellcheck="false" /></label><button type="button" :disabled="copyingAccount" @click="copyAccount">复制账号</button></div>
          <p class="social-muted">把这个登录账号发给朋友，对方即可搜索并发送好友申请；昵称仅用于展示。</p>
          <p v-if="accountCopyStatus" class="social-notice" role="status">{{ accountCopyStatus }}</p>
        </section>
        <div class="social-grid"><form class="social-card" @submit.prevent="search"><h2>发现朋友</h2><label>搜索账号<input v-model="query" required minlength="2" maxlength="40" placeholder="输入对方登录账号或前缀（至少 2 个字符）" /></label><label>申请留言<input v-model="message" maxlength="200" placeholder="一起听歌吗？" /></label><button class="primary" :disabled="busy">搜索</button><div v-for="user in results" :key="user.id" class="social-row"><span>{{ user.username }}</span><button :disabled="busy" @click.prevent="relation(user.id, 'request')">发送好友申请</button></div></form><form class="social-card" @submit.prevent="createGroup"><h2>创建群聊</h2><label>群名称<input v-model="groupName" required maxlength="80" /></label><div class="social-actions"><label v-for="friend in friends" :key="friend.target_user_id" class="social-check"><input v-model="groupMembers" type="checkbox" :value="friend.target_user_id" />{{ friend.remark || friend.username }}</label></div><button :disabled="busy || !groupMembers.length">创建并邀请</button></form></div><section v-if="incoming.length" class="social-card"><h2>好友申请</h2><div v-for="item in incoming" :key="item.user_id" class="social-row"><span>{{ item.username }}<small>{{ item.request_message }}</small></span><button :disabled="busy" @click="relation(item.user_id, 'accept')">同意</button><button :disabled="busy" @click="relation(item.user_id, 'reject')">拒绝</button></div></section><section class="social-card"><h2>我的好友 · {{ friends.length }}</h2><div v-for="friend in friends" :key="friend.target_user_id" class="social-row"><span class="social-avatar">{{ friend.username.slice(0, 1) }}</span><strong>{{ friend.remark || friend.username }}<small class="social-muted"> · {{ presence[friend.target_user_id]?.online ? '在线' : '离线 / 状态未公开' }}<span v-if="presence[friend.target_user_id]?.listening?.playback?.track"> · 正在听 {{ presence[friend.target_user_id].listening.playback.track.title }}</span></small></strong><span class="social-spacer" /><button :disabled="busy" @click="chat(friend.target_user_id)">聊天</button><details><summary>更多</summary><div class="social-actions"><button @click="relation(friend.target_user_id, 'remark')">备注</button><button @click="relation(friend.target_user_id, 'remove')">删除好友</button><button @click="relation(friend.target_user_id, 'block')">拉黑</button></div></details></div><p v-if="!friends.length" class="social-empty">搜索好友的账号，从一首歌开始认识彼此。</p></section><details v-if="blocks.length" class="social-card"><summary>黑名单</summary><div v-for="item in blocks" :key="item.target_user_id" class="social-row"><span>{{ item.username }}</span><button @click="relation(item.target_user_id, 'unblock')">解除拉黑</button></div></details><section class="social-card"><h2>通知与邀请</h2><button v-for="item in notifications" :key="item.id" class="social-notification" :class="{ unread: !item.read_at }" @click="openNotification(item)"><strong>{{ { friend_request: '好友申请', friend_accept: '好友申请已通过', group_invite: '群聊邀请', room_invite: '房间邀请', duet_invite: '补唱邀请', duet_claimed: '好友已领取补唱位置', rule_update: '订阅规则有新版本', shared_playlist: '共同歌单邀请' }[item.event_type as string] || '社交通知' }}</strong><span>{{ item.payload.title || item.payload.message || '查看详情' }}</span></button></section></section>
      <form v-if="tab === 'settings' && settings" class="social-card" @submit.prevent="saveSettings"><h2>我的社交隐私</h2><label class="social-check"><input v-model="settings.discoverable" type="checkbox" />允许通过账号搜索到我</label><label>在线状态<select v-model="settings.online_visibility"><option value="friends">好友可见</option><option value="nobody">关闭</option></select></label><label>正在听什么<select v-model="settings.listening_visibility"><option value="friends">好友可见</option><option value="nobody">关闭</option></select></label><label>好友邀请<select v-model="settings.invite_policy"><option value="friends">接受好友邀请</option><option value="nobody">关闭邀请</option></select></label><p class="social-muted">私信仅对好友开放。在线状态和听歌状态分别控制。</p><button class="primary" :disabled="busy">保存隐私设置</button></form>
    </template><p v-else class="social-empty">正在打开音乐社交…</p></div>
</template>

<style scoped>
.social-account-row { align-items: flex-end; }
.social-account-field { flex: 1 1 220px; min-width: 0; margin: 0; }
</style>
