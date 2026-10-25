<script setup lang="ts">
import { ref, watch } from 'vue'
import { shareRequest, request, failure, msgId } from './social'
const conversations = ref<any[]>([])
const target = ref(''), scope = ref('conversation'), link = ref('')
const allowFile = ref(false)
const busy = ref(false)
const error = ref('')
watch(shareRequest, async value => {
  error.value = ''; link.value = ''; scope.value = 'conversation'; target.value = ''; allowFile.value = false
  if (value) try { conversations.value = (await request('/conversations')).items.filter((x: any) => x.member_status === 'active') } catch (e) { error.value = failure(e) }
})
async function send() {
  if (!shareRequest.value || scope.value === 'conversation' && !target.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const share = await request('/shares', 'POST', { target_kind: shareRequest.value.kind, source_key: shareRequest.value.key, scope: scope.value, conversation_id: scope.value === 'conversation' ? target.value : undefined, allow_file: allowFile.value })
    if (scope.value !== 'conversation') { const url = new URL(location.origin); url.searchParams.set('social_share', share.id); if (share.token) url.searchParams.set('token', share.token); link.value = url.href; return }
    await request(`/conversations/${target.value}/messages`, 'POST', { client_msg_id: msgId(), msg_type: 'share', share_id: share.id })
    shareRequest.value = null
  } catch (e) { error.value = failure(e) } finally { busy.value = false }
}
</script>
<template>
  <div v-if="shareRequest" class="social-dialog-backdrop" @click.self="!busy && (shareRequest = null)">
    <form class="social-dialog" role="dialog" aria-modal="true" aria-labelledby="share-title" @submit.prevent="send">
      <h2 id="share-title">分享 {{ shareRequest.title }}</h2>
      <label>分享范围<select v-model="scope"><option value="conversation">指定会话</option><option value="friends">好友可访问</option><option value="link">持链接的登录用户</option><option value="public">所有登录用户</option></select></label><label v-if="scope === 'conversation'">发送到<select v-model="target" required><option value="" disabled>选择会话</option><option v-for="item in conversations" :key="item.id" :value="item.id">{{ item.title || item.peer_name || '好友私聊' }}</option></select></label>
      <p v-if="scope === 'conversation' && !conversations.length">先在“音乐社交”中添加好友并创建会话。</p>
      <template v-if="['track', 'playlist'].includes(shareRequest.kind)"><label class="social-check"><input v-model="allowFile" type="checkbox" />允许接收者播放包含的服务器本地音频</label><p class="social-muted">发送分享时的快照；设备本地文件仅分享歌曲信息。</p></template>
      <p v-else-if="shareRequest.kind === 'room'" class="social-muted">接收者可凭卡片中的邀请加入，房间邀请码有效 24 小时。</p>
      <p v-else class="social-muted">分享已发布的版本。</p>
      <p v-if="link" class="social-notice">分享链接（7 天有效）：<a :href="link">{{ link }}</a></p><p v-if="error" role="alert" class="social-error">{{ error }}</p>
      <div class="social-actions"><button type="button" :disabled="busy" @click="shareRequest = null">取消</button><button class="primary" :disabled="busy || (scope === 'conversation' && !target)">{{ busy ? '发送中…' : scope === 'conversation' ? '发送分享' : '生成分享链接' }}</button></div>
    </form>
  </div>
</template>
