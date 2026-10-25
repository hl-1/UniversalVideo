<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Room, RoomEvent, Track } from 'livekit-client'
import { account } from '../accounts/auth'
import { currentTrack } from '../music/player/musicPlayer'
import { trackPayload } from '../music/musicLibrary'
import { request, socialEvent, failure, openShare } from './social'
import { roomAudio } from './roomAudio'
import ChatPanel from './ChatPanel.vue'
const props = defineProps<{ kind: 'listen' | 'karaoke'; roomId?: string; code?: string; rtcAvailable: boolean; duetEnabled: boolean }>()
const rooms = ref<any[]>([]), room = ref<any>(null), title = ref(''), code = ref(props.code || ''), visibility = ref('friends')
const error = ref(''), busy = ref(false), invitation = ref(''), songs = ref<any[]>([]), song = ref(''), partner = ref<string | undefined>()
const newKind = ref(props.kind === 'listen' ? 'listen' : 'karaoke')
const voiceConnected = ref(false), micOn = ref(false), devices = ref<MediaDeviceInfo[]>([]), device = ref('')
const audio = roomAudio(), audioError = audio.error, audioReady = audio.ready, audioVolume = audio.volume, audioCurrent = audio.current
let rtc: Room | undefined
const connectionId = crypto.randomUUID().replaceAll('-', ''), currentSong = ref<any>(null), reactions = ref<string[]>([])
const guide = ref<HTMLAudioElement | null>(null), guideVolume = ref(0)
let loadedSong = ''
const heartbeat = setInterval(() => { if (room.value) void request(`/rooms/${room.value.id}/heartbeat`, 'POST', { connection_id: connectionId }).catch(() => {}) }, 15000)
const guideSync = setInterval(() => { if (!guide.value || !currentSong.value?.guide_asset_id) return; const element = guide.value; element.volume = guideVolume.value; if (Math.abs(element.currentTime - audio.audio.currentTime) > .2) element.currentTime = audio.audio.currentTime; if (audio.audio.paused) element.pause(); else if (element.paused) void element.play().catch(() => {}) }, 250)
const remoteAudio = ref<HTMLDivElement | null>(null)
const vocalVolume = ref(.8)
watch(vocalVolume, value => { remoteAudio.value?.querySelectorAll('audio').forEach(element => { element.volume = value }) })
const me = computed(() => room.value?.members.find((x: any) => x.user_id === account.value?.id))
const manager = computed(() => ['owner', 'admin'].includes(me.value?.role))
const controller = computed(() => manager.value || me.value?.permissions.control)
async function refresh() { rooms.value = (await request('/rooms')).items.filter((x: any) => props.kind === 'listen' ? x.kind === 'listen' : x.kind !== 'listen') }
async function open(id: string) {
  error.value = ''
  try { await disconnect(); room.value = await request(`/rooms/${id}`); await prepare(); await refresh() } catch (e) { error.value = failure(e) }
}
async function prepare() {
  if (!room.value) return
  const id = room.value.id
  if (room.value.playback.karaoke_song_id !== loadedSong) { loadedSong = room.value.playback.karaoke_song_id || ''; currentSong.value = loadedSong ? await request(`/rooms/${id}/song`) : null }
  await audio.update(room.value, async () => {
    if (room.value?.id !== id) return
    try { room.value = await request(`/rooms/${id}/control`, 'POST', { action: 'ready', revision: room.value.revision }) } catch { if (room.value?.id === id) room.value = await request(`/rooms/${id}`) }
  })
  if (rtc && !me.value?.permissions.can_publish && micOn.value) { await rtc.localParticipant.setMicrophoneEnabled(false); micOn.value = false }
}
async function create() {
  busy.value = true; error.value = ''
  try { room.value = await request('/rooms', 'POST', { title: title.value, kind: newKind.value, visibility: visibility.value, capacity: props.kind === 'listen' ? 10 : 20 }); await refresh() } catch (e) { error.value = failure(e) } finally { busy.value = false }
}
async function join(id?: string) {
  error.value = ''; busy.value = true
  try { room.value = await request(id ? `/rooms/${id}/join` : '/rooms/join', 'POST', { code: code.value.trim() }); audio.start(); await prepare(); await refresh() } catch (e) { error.value = failure(e) } finally { busy.value = false }
}
async function control(action: string, extra: Record<string, any> = {}) {
  if (!room.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const data = await request(`/rooms/${room.value.id}/control`, 'POST', { action, revision: room.value.revision, ...extra })
    if (data.left) { await disconnect(); audio.audio.pause(); room.value = null; await refresh(); return }
    room.value = data; await prepare()
  } catch (e) { error.value = failure(e); if (room.value) room.value = await request(`/rooms/${room.value.id}`).catch(() => null) } finally { busy.value = false }
}
async function invite() { try { invitation.value = (await request(`/rooms/${room.value.id}/invites`, 'POST', {})).code } catch (e) { error.value = failure(e) } }
async function connectVoice() {
  error.value = ''
  try {
    await disconnect()
    const data = await request(`/rooms/${room.value.id}/rtc`, 'POST', {})
    const voice = new Room({ audioCaptureDefaults: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }, publishDefaults: { audioPreset: { maxBitrate: 128000 }, dtx: false } })
    rtc = voice
    voice.on(RoomEvent.TrackSubscribed, track => { if (track.kind === Track.Kind.Audio) { const element = track.attach(); element.volume = vocalVolume.value; remoteAudio.value?.appendChild(element) } })
    voice.on(RoomEvent.TrackUnsubscribed, track => track.detach().forEach(node => node.remove()))
    voice.on(RoomEvent.Disconnected, () => { voiceConnected.value = false; micOn.value = false })
    await voice.connect(data.url, data.token); voiceConnected.value = true; audio.start(); await prepare()
  } catch (e) { error.value = failure(e) }
}
async function microphone() {
  if (!rtc) return
  try { await rtc.localParticipant.setMicrophoneEnabled(!micOn.value, device.value ? { deviceId: device.value } : undefined); micOn.value = !micOn.value } catch (e) { error.value = failure(e) }
}
async function testMic() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: device.value ? { deviceId: { exact: device.value } } : true })
    devices.value = (await navigator.mediaDevices.enumerateDevices()).filter(x => x.kind === 'audioinput')
    stream.getTracks().forEach(track => track.stop()); await control('ready', { microphone_ready: true })
  } catch (e) { error.value = failure(e) }
}
async function disconnect() { await rtc?.disconnect(); rtc = undefined; voiceConnected.value = false; micOn.value = false; remoteAudio.value?.replaceChildren() }
watch(socialEvent, event => {
  if (!event) return
  if (event.topic === 'reaction' && event.object_key === room.value?.id) { reactions.value = [...reactions.value.slice(-7), event.reaction]; setTimeout(() => reactions.value.shift(), 4000) }
  if (event.topic === 'room' || event.topic === 'reconnected') {
    void refresh().catch(() => {})
    if (room.value && (event.object_key === room.value.id || event.topic === 'reconnected')) void request(`/rooms/${room.value.id}`).then(async data => { room.value = data; await prepare() }).catch(async e => { error.value = failure(e); room.value = null; audio.audio.pause(); await disconnect() })
  }
})
onMounted(async () => { try { await refresh(); if (props.kind !== 'listen') songs.value = (await request('/karaoke/songs')).items; if (props.roomId) { try { await request(`/rooms/${props.roomId}`); await open(props.roomId) } catch { await join(props.roomId) } } } catch (e) { error.value = failure(e) } })
onBeforeUnmount(() => { clearInterval(heartbeat); clearInterval(guideSync); guide.value?.pause(); audio.stop(); void disconnect() })
</script>
<template>
  <section class="social-stack">
    <p v-if="error" class="social-error" role="alert">{{ error }}</p>
    <template v-if="!room">
      <div class="social-grid"><form class="social-card" @submit.prevent="create"><h2>{{ kind === 'listen' ? '创建一起听房间' : '创建 K 歌房' }}</h2><label>房间名称<input v-model="title" required maxlength="80" placeholder="今晚一起听" /></label><label>加入方式<select v-model="visibility"><option value="friends">好友可见</option><option value="private">私密邀请</option><option value="invite">邀请码加入</option></select></label><label v-if="kind !== 'listen' && duetEnabled">演唱方式<select v-model="newKind"><option value="karaoke">单人轮唱</option><option value="duet">双人实时对唱</option></select></label><button class="primary" :disabled="busy || !title.trim()">创建房间</button></form><form class="social-card" @submit.prevent="join()"><h2>朋友在等你</h2><p>输入朋友发来的邀请码。</p><label>邀请码<input v-model="code" required maxlength="100" autocomplete="off" /></label><button :disabled="busy || !code.trim()">加入并开始听</button></form></div>
      <div class="social-section-heading"><h2>好友的房间</h2><button @click="refresh">刷新</button></div><div class="social-grid"><article v-for="item in rooms" :key="item.id" class="social-card"><span class="social-eyebrow">{{ item.kind === 'listen' ? '一起听' : item.kind === 'duet' ? '实时对唱' : 'K 歌房' }}</span><h3>{{ item.title }}</h3><p>{{ item.playback.track?.title || '等待第一首歌' }}</p><button @click="item.owner_id === account?.id ? open(item.id) : join(item.id)">进入房间</button></article></div><p v-if="!rooms.length" class="social-empty">创建房间，邀请朋友来听同一首歌。</p>
    </template>
    <template v-else>
      <header class="social-section-heading"><div><span class="social-eyebrow">{{ room.kind === 'listen' ? '共同聆听' : '轮麦演唱' }}</span><h2>{{ room.title }}</h2><p>{{ room.members.length }} / {{ room.config.capacity }} 人 · 已一起 {{ Math.floor((Date.now() / 1000 - room.created_at) / 60) }} 分钟</p></div><div class="social-actions"><button v-if="manager" @click="invite">生成邀请码</button><button v-if="manager" @click="openShare('room', room.id, room.title)">分享房间</button><button @click="audio.start()">开始听</button><button :disabled="busy" @click="control(me?.role === 'owner' ? 'close' : 'leave')">{{ me?.role === 'owner' ? '结束房间' : '退出房间' }}</button></div></header>
      <p v-if="invitation" class="social-notice">邀请码：<strong>{{ invitation }}</strong></p>
      <div class="social-now-playing"><span class="social-record">♫</span><div><h3>{{ room.playback.track?.title || '选择第一首歌' }}</h3><p>{{ room.playback.track?.artist || '让音乐连接你和朋友' }}</p><p>{{ audioReady ? '音源已准备' : '等待音源' }} · {{ room.playback.state === 'playing' ? '播放中' : '已暂停' }}</p><p v-if="audioError" class="social-error">{{ audioError }}</p><div class="social-actions" v-if="controller"><button :disabled="busy || !room.playback.track" @click="control(room.playback.state === 'playing' ? 'pause' : 'play')">{{ room.playback.state === 'playing' ? '暂停' : '同步播放' }}</button><button :disabled="busy" @click="control('next')">下一首 / 下一位</button></div></div></div>
      <label v-if="room.playback.track && controller">播放进度<input type="range" min="0" :max="room.playback.track.duration || 600" :value="audioCurrent" @change="control('seek', { position_ms: Math.round(Number(($event.target as HTMLInputElement).value) * 1000) })" /></label><label>我的音量<input type="range" min="0" max="1" step=".01" :value="audioVolume" @input="audio.setVolume(Number(($event.target as HTMLInputElement).value))" /></label>
      <div v-if="currentSong?.lyrics?.length" class="social-lyrics"><p v-for="(line, index) in currentSong.lyrics" :key="index" :class="{ active: audioCurrent * 1000 >= line.start_ms && audioCurrent * 1000 < line.end_ms }"><small>{{ line.role }}</small> {{ line.text }}</p></div><label v-if="currentSong?.guide_asset_id">导唱音量<input v-model.number="guideVolume" type="range" min="0" max="1" step=".01" /><audio ref="guide" :src="`/api/social/assets/${currentSong.guide_asset_id}/content`" preload="auto" /></label><p v-if="reactions.length" class="social-reactions" aria-live="polite">{{ reactions.join(" ") }}</p><div v-if="room.kind !== 'listen'" class="social-card"><h3>观众席与麦克风</h3><p v-if="!rtcAvailable">语音服务尚未配置，可先在“我的合唱”中录制作品。</p><div class="social-actions"><button :disabled="!rtcAvailable" @click="voiceConnected ? disconnect() : connectVoice()">{{ voiceConnected ? '离开语音' : '进入观众席' }}</button><button @click="testMic">检测麦克风并准备</button><select v-if="devices.length" v-model="device" aria-label="输入设备"><option value="">默认麦克风</option><option v-for="item in devices" :key="item.deviceId" :value="item.deviceId">{{ item.label }}</option></select><button :disabled="!voiceConnected || !me?.permissions.can_publish" @click="microphone">{{ micOn ? '关闭麦克风' : '开始演唱' }}</button></div><p class="social-muted">轮到自己并获准上麦后才能演唱。房间默认不录制。</p><label>远端人声 / 对方监听音量<input v-model.number="vocalVolume" type="range" min="0" max="1" step=".01" /></label><p class="social-muted">调至 0 可关闭对方监听，减少远端延迟对演唱的影响。</p><button v-if="room.playback.singers?.includes(account?.id)" :disabled="busy" @click="control('finish')">演唱结束，主动让麦</button><div ref="remoteAudio" /></div>
      <div class="social-grid"><section class="social-card"><h3>共同点歌</h3><template v-if="room.kind === 'listen'"><p>{{ currentTrack ? `当前选择：${currentTrack.title}` : '先在音乐中心播放一首在线歌曲，再加入队列。' }}</p><button :disabled="!currentTrack || busy" @click="control('enqueue', { track: trackPayload(currentTrack!) })">将正在听的歌加入队列</button></template><template v-else><label>演唱曲目<select v-model="song"><option value="" disabled>选择固定伴奏版本</option><option v-for="item in songs" :key="item.id" :value="item.id">{{ item.track_snapshot.title }}</option></select></label><label v-if="room.kind === 'duet'">合唱伙伴<select v-model="partner"><option v-for="person in room.members.filter((x: any) => x.user_id !== account?.id)" :key="person.user_id" :value="person.user_id">{{ person.username }}</option></select></label><button :disabled="!song || busy" @click="control('enqueue', { karaoke_song_id: song, target_user_id: partner })">点歌并排麦</button></template><ol class="social-queue"><li v-for="item in room.queue" :key="item.id"><span>{{ item.track_snapshot.title }} <small>{{ item.status === 'performing' ? '当前' : '等待' }}</small></span><button v-if="item.status === 'queued' && (manager || item.requester_id === account?.id)" @click="control('cancel_queue', { queue_id: item.id })">取消</button></li></ol></section><section class="social-card"><h3>房间成员</h3><div v-for="person in room.members" :key="person.user_id" class="social-row"><span>{{ person.username }} <small>{{ person.role === 'owner' ? '房主' : person.permissions.can_publish ? '演唱者' : '成员' }}</small></span><template v-if="manager && person.user_id !== account?.id"><button @click="control('controller', { target_user_id: person.user_id, allow: !person.permissions.control })">{{ person.permissions.control ? '取消控制' : '授权控制' }}</button><button v-if="me?.role === 'owner'" @click="control('admin', { target_user_id: person.user_id, allow: person.role !== 'admin' })">{{ person.role === 'admin' ? '取消管理员' : '设为管理员' }}</button><button v-if="me?.role === 'owner'" @click="control('transfer', { target_user_id: person.user_id })">移交</button><button v-if="room.kind !== 'listen'" @click="control('mute', { target_user_id: person.user_id, allow: !person.permissions.muted })">{{ person.permissions.muted ? '解除禁言' : '禁言' }}</button><button @click="control('remove', { target_user_id: person.user_id })">移除</button></template></div><div class="social-actions"><button v-for="reaction in ['👏', '❤️', '🎵', '🎉']" :key="reaction" @click="control('react', { reaction })">{{ reaction }}</button></div></section></div>
      <details class="social-card"><summary>房间聊天</summary><ChatPanel :key="room.conversation_id" :conversation-id="room.conversation_id" embedded /></details>
    </template>
  </section>
</template>
