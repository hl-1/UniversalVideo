<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { account } from '../accounts/auth'
import { request, socialEvent, failure, upload, assetUrl, openShare } from './social'
const props = defineProps<{ performanceId?: string; code?: string }>()
const works = ref<any[]>([]), songs = ref<any[]>([]), work = ref<any>(null), selectedSong = ref(''), slot = ref(1)
const error = ref(''), busy = ref(false), invitation = ref(''), claimCode = ref(props.code || ''), recordConsent = ref(false), publishVisibility = ref('private')
const device = ref(''), devices = ref<MediaDeviceInfo[]>([]), recording = ref(false), preview = ref(''), take = ref<Blob | null>(null)
const offset = ref(0), gain = ref(0), invitee = ref<string | undefined>(), friends = ref<any[]>([])
const backing = ref<HTMLAudioElement | null>(null), firstVocal = ref<HTMLAudioElement | null>(null)
const songTitle = ref(''), songArtist = ref(''), rights = ref(''), songPublic = ref(false), lyrics = ref(''), backingFile = ref<File | null>(null), guideFile = ref<File | null>(null), showSong = ref(false)
const time = ref(0), recordStarted = ref(0)
const me = computed(() => work.value?.parts.find((x: any) => x.user_id === account.value?.id))
const partner = computed(() => work.value?.parts.find((x: any) => x.user_id && x.user_id !== account.value?.id))
let recorder: MediaRecorder | undefined, stream: MediaStream | undefined, timer: ReturnType<typeof setTimeout> | undefined, partnerTimer: ReturnType<typeof setTimeout> | undefined, disposed = false, generation = 0
async function refresh() { const [a, b] = await Promise.all([request('/performances'), request('/karaoke/songs')]); works.value = a.items; songs.value = b.items }
async function open(id: string) { try { generation++; stop(); take.value = null; URL.revokeObjectURL(preview.value); preview.value = ''; work.value = await request(`/performances/${id}`); offset.value = me.value?.offset_ms || 0; gain.value = Number(me.value?.gain_db || 0); invitation.value = ''; recordConsent.value = false; time.value = 0 } catch (e) { error.value = failure(e) } }
async function create() { await act(async () => { work.value = await request('/performances', 'POST', { karaoke_song_id: selectedSong.value, slot: slot.value }); await refresh() }) }
async function sameSong() { selectedSong.value = work.value.song.id; clearWork(); await create() }
async function act(fn: () => Promise<void>) { if (busy.value) return; busy.value = true; error.value = ''; try { await fn() } catch (e) { error.value = failure(e) } finally { busy.value = false } }
async function action(action: string, extra: any = {}) { await act(async () => { const data = await request(`/performances/${work.value.id}/actions`, 'POST', { action, revision: work.value.revision, ...extra }); if (data.code) invitation.value = data.code; else if (action === 'withdraw' || data.released) work.value = null; else work.value = data; await refresh() }) }
async function claim() { await act(async () => { work.value = await request('/performances/claim', 'POST', { code: claimCode.value }); await refresh() }) }
async function setupMic() { stream?.getTracks().forEach(x => x.stop()); stream = await navigator.mediaDevices.getUserMedia({ audio: { ...(device.value ? { deviceId: { exact: device.value } } : {}), echoCancellation: false, noiseSuppression: false, autoGainControl: false } }); devices.value = (await navigator.mediaDevices.enumerateDevices()).filter(x => x.kind === 'audioinput') }
async function testMic() { await act(async () => { await setupMic(); stream?.getTracks().forEach(x => x.stop()); stream = undefined; error.value = '麦克风已就绪。建议使用耳机，先短录一次并试听校准延迟。' }) }
async function record() {
  if (!recordConsent.value || !work.value) return
  await act(async () => {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') throw new Error('当前环境不支持录音，请使用 HTTPS 或本机浏览器。')
    await setupMic(); const chunks: BlobPart[] = []
    const capturedStream = stream!, capturedRecorder = new MediaRecorder(capturedStream), version = generation, id = work.value.id
    recorder = capturedRecorder
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data) }
    recorder.onstop = () => {
      capturedStream.getTracks().forEach(x => x.stop()); if (stream === capturedStream) stream = undefined; backing.value?.pause(); firstVocal.value?.pause(); recording.value = false
      if (disposed || version !== generation || work.value?.id !== id) return
      take.value = new Blob(chunks, { type: capturedRecorder.mimeType || 'audio/webm' }); URL.revokeObjectURL(preview.value); preview.value = URL.createObjectURL(take.value)
    }
    // Capture just the microphone; backing and the other vocal stay separate.
    backing.value!.currentTime = 0
    try {
      recordStarted.value = performance.now(); capturedRecorder.start(); recording.value = true
      await backing.value!.play()
      if (firstVocal.value) {
        const element = firstVocal.value, delay = Number(partner.value?.offset_ms || 0)
        element.currentTime = Math.max(0, -delay / 1000); element.volume = Math.min(1, 10 ** (Number(partner.value?.gain_db || 0) / 20))
        partnerTimer = setTimeout(() => { if (recording.value && generation === version) void element.play().catch(() => {}) }, Math.max(0, delay))
      }
    } catch (e) { stop(); throw e }
    timer = setTimeout(stop, work.value.song.duration_ms + 5000)
  })
}
function stop() { clearTimeout(timer); clearTimeout(partnerTimer); if (recorder?.state === 'recording') recorder.stop(); backing.value?.pause(); firstVocal.value?.pause(); stream?.getTracks().forEach(x => x.stop()); recording.value = false }
function clearWork() { generation++; stop(); work.value = null; take.value = null; URL.revokeObjectURL(preview.value); preview.value = '' }
async function saveTake() {
  if (!take.value || !recordConsent.value || !work.value) return
  const id = work.value.id, version = generation, revision = work.value.revision, recorded = take.value, delay = offset.value, volume = gain.value
  await act(async () => {
    const asset = await upload(recorded, 'vocal')
    if (version !== generation || work.value?.id !== id) return
    work.value = await request(`/performances/${id}/actions`, 'POST', { action: 'take', revision, asset_id: asset.id, record_consent: true, offset_ms: delay, gain_db: volume })
    take.value = null; URL.revokeObjectURL(preview.value); preview.value = ''; await refresh()
  })
}
async function createSong() {
  if (!backingFile.value) return
  await act(async () => {
    const backingAsset = await upload(backingFile.value!, 'backing'), guideAsset = guideFile.value ? await upload(guideFile.value, 'guide') : undefined
    const parsed = lyrics.value.trim() ? JSON.parse(lyrics.value) : []
    await request('/karaoke/songs', 'POST', { title: songTitle.value, artist: songArtist.value, backing_asset_id: backingAsset.id, guide_asset_id: guideAsset?.id, lyrics: parsed, rights_note: rights.value, visibility: songPublic.value ? 'public' : 'private' })
    showSong.value = false; await refresh()
  })
}
watch(socialEvent, event => { if (work.value && (event?.topic === 'performance' && event.object_key === work.value.id || event?.topic === 'reconnected')) void request(`/performances/${work.value.id}`).then(data => { work.value = data }).catch(e => { error.value = failure(e) }) })
onMounted(async () => { try { await refresh(); friends.value = (await request('/relations')).items.filter((x: any) => x.user_id === account.value?.id && x.kind === 'friend' && x.status === 'accepted'); if (props.performanceId) await open(props.performanceId) } catch (e) { error.value = failure(e) } })
onBeforeUnmount(() => { disposed = true; generation++; stop(); URL.revokeObjectURL(preview.value) })
</script>
<template>
  <section class="social-stack"><header class="social-section-heading"><div><span class="social-eyebrow">两个人，一首完整的歌</span><h2>我的合唱</h2></div><button @click="showSong = !showSong">添加伴奏版本</button></header><p v-if="error" class="social-error" role="alert">{{ error }}</p>
    <form v-if="showSong" class="social-card" @submit.prevent="createSong"><h3>添加固定演唱版本</h3><div class="social-form-grid"><label>歌名<input v-model="songTitle" required maxlength="255" /></label><label>歌手<input v-model="songArtist" maxlength="255" /></label><label>固定伴奏<input type="file" accept="audio/*" required @change="backingFile = ($event.target as HTMLInputElement).files?.[0] || null" /></label><label>可选导唱<input type="file" accept="audio/*" @change="guideFile = ($event.target as HTMLInputElement).files?.[0] || null" /></label></div><label>来源与可用范围<input v-model="rights" required maxlength="300" placeholder="说明伴奏来源及允许的房间播放、录制和分享范围" /></label><label>分段歌词（可选）<textarea v-model="lyrics" placeholder='[{"start_ms":0,"end_ms":5000,"text":"歌词","role":"A"}]' /></label><label class="social-check"><input v-model="songPublic" type="checkbox" />允许其他登录用户使用此伴奏版本</label><button class="primary" :disabled="busy">保存固定版本</button></form>
    <template v-if="!work"><div class="social-grid"><form class="social-card" @submit.prevent="create"><h3>发起录制合唱</h3><label>固定伴奏版本<select v-model="selectedSong" required><option value="" disabled>选择歌曲</option><option v-for="song in songs" :key="song.id" :value="song.id">{{ song.track_snapshot.title }}</option></select></label><label>我的角色<select v-model="slot"><option :value="1">A 唱</option><option :value="2">B 唱</option></select></label><button class="primary" :disabled="busy || !selectedSong">开始创作</button></form><form class="social-card" @submit.prevent="claim"><h3>接受补唱邀请</h3><label>补唱邀请码<input v-model="claimCode" required maxlength="100" /></label><button :disabled="busy || !claimCode.trim()">领取演唱位置</button></form></div><div class="social-grid"><button v-for="item in works" :key="item.id" class="social-card" @click="open(item.id)"><h3>{{ item.track_snapshot.title }}</h3><p>{{ { draft: '录制草稿', mix_pending: '等待混音', mixing: '正在混音', mix_failed: '混音失败，可重试', ready: '等待双方确认', published: '已发布' }[item.status as string] || item.status }}</p></button></div></template>
    <article v-else-if="!me" class="social-card"><header class="social-section-heading"><h3>{{ work.song.track_snapshot.title }} · 合唱作品</h3><button @click="clearWork">返回作品列表</button></header><p>参与者：{{ work.parts.filter((part: any) => part.user_id).map((part: any) => part.user_id).join('、') }}</p><audio v-if="work.mix_asset_id" controls :src="assetUrl(work.mix_asset_id)" /><button v-if="work.song.visibility === 'public'" @click="sameSong">发起同曲合唱</button></article>
    <article v-else class="social-card"><header class="social-section-heading"><h3>{{ work.song.track_snapshot.title }} · {{ me?.slot === 1 ? 'A 唱' : 'B 唱' }}</h3><button :disabled="recording" @click="clearWork">返回作品列表</button></header><p>状态：{{ work.status }} <span v-if="work.last_error">{{ work.last_error }}</span></p><audio ref="backing" :src="assetUrl(work.song.backing_asset_id)" controls preload="metadata" @timeupdate="time = Number(($event.target as HTMLAudioElement).currentTime)" @ended="stop" /><audio v-if="partner?.raw_asset_id" ref="firstVocal" :src="assetUrl(partner.raw_asset_id)" preload="metadata" /><div v-if="work.song.lyrics.length" class="social-lyrics"><p v-for="(line, index) in work.song.lyrics" :key="index" :class="{ active: time * 1000 >= line.start_ms && time * 1000 < line.end_ms }"><small>{{ line.role === 'both' ? '合唱' : line.role + ' 唱' }}</small> {{ line.text }}</p></div><div class="social-form-grid"><label>输入设备<select v-model="device"><option value="">默认麦克风</option><option v-for="item in devices" :key="item.deviceId" :value="item.deviceId">{{ item.label }}</option></select></label><label>人声偏移（毫秒）<input v-model.number="offset" type="number" min="-10000" max="10000" step="10" /></label><label>人声音量（dB）<input v-model.number="gain" type="number" min="-30" max="12" step=".5" /></label></div><label class="social-check"><input v-model="recordConsent" type="checkbox" :disabled="recording" />我同意录制并保存自己的独立人声</label><div class="social-actions"><button :disabled="busy || recording" @click="testMic">检测麦克风</button><button class="primary" :disabled="busy || !recordConsent" @click="recording ? stop() : record()">{{ recording ? '结束录音' : me?.raw_asset_id ? '重录我的部分' : '录制我的部分' }}</button><button :disabled="busy || recording" @click="action('swap')">交换 A/B 角色</button><button v-if="me?.raw_asset_id" :disabled="busy || recording" @click="action('align', { offset_ms: offset, gain_db: gain })">保存对齐与音量</button></div><div v-if="preview" class="social-card"><h4>试听当前录音</h4><audio :src="preview" controls /><button :disabled="busy || !recordConsent" @click="saveTake">{{ busy ? '上传中…' : '保存并替换我的录音' }}</button></div><p class="social-muted">建议使用耳机；负偏移将人声提前，正偏移将人声延后。重录和调整后，双方需要重新确认成品。</p><div v-if="work.owner_id === account?.id" class="social-row"><select v-model="invitee" aria-label="邀请好友"><option :value="undefined">通过补唱链接邀请</option><option v-for="friend in friends" :key="friend.target_user_id" :value="friend.target_user_id">{{ friend.username }}</option></select><button :disabled="busy" @click="action('invite', { invitee_id: invitee })">生成补唱邀请</button></div><p v-if="invitation" class="social-notice">补唱邀请码：{{ invitation }}</p><div class="social-actions"><button :disabled="busy || recording || ['mixing', 'mix_pending'].includes(work.status)" @click="action('mix')">{{ work.status === 'mix_failed' ? '重试混音' : '生成混音成品' }}</button><button :disabled="busy || recording" @click="action('withdraw')">撤下作品</button></div><template v-if="work.mix_asset_id"><h4>试听成品</h4><button v-if="work.status === 'published' && work.visibility === 'public'" @click="openShare('performance', work.id, work.song.track_snapshot.title)">分享合唱</button><audio controls :src="assetUrl(work.mix_asset_id)" /><label>双方确认的发布范围<select v-model="publishVisibility"><option value="private">仅参与者</option><option value="public">所有登录用户</option></select></label><button class="primary" :disabled="busy || !['ready', 'published'].includes(work.status)" @click="action('confirm', { visibility: publishVisibility })">我已试听，同意此版本和发布范围</button><p class="social-muted">双方选择相同范围并确认同一份成品后才会发布。</p></template></article>
  </section>
</template>
