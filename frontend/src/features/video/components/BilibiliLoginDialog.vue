<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { AudioLines, LoaderCircle, QrCode, RefreshCw, X } from 'lucide-vue-next'

const props = defineProps<{
  open: boolean
  forSummary: boolean
  speechOnly?: boolean
  busy: boolean
  error: string
  qr: { status: string; qr_image?: string } | null
}>()
const emit = defineEmits<{ close: []; speech: []; refresh: [] }>()
const panel = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null
watch(() => props.open, async open => {
  if (open) { previousFocus = document.activeElement as HTMLElement; await nextTick(); panel.value?.focus() }
  else previousFocus?.focus()
})
function keyboard(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.stopPropagation(); emit('close') }
  if (event.key !== 'Tab') return
  const controls = panel.value?.querySelectorAll<HTMLElement>('button:not(:disabled), [href], [tabindex="0"]')
  const first = controls?.[0], last = controls?.[controls.length - 1]
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.value)) { event.preventDefault(); first?.focus() }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="bili-overlay" @click.self="emit('close')" @keydown="keyboard">
      <section ref="panel" class="bili-dialog" role="dialog" aria-modal="true" aria-labelledby="bili-title" aria-describedby="bili-description" tabindex="-1">
        <button class="bili-close" :aria-label="speechOnly ? '关闭转写提示' : '关闭 B站登录'" @click="emit('close')"><X :size="20" /></button>
        <div class="bili-icon"><AudioLines v-if="speechOnly" :size="25" /><QrCode v-else :size="25" /></div>
        <h2 id="bili-title">{{ speechOnly ? '本视频暂无可用字幕' : forSummary ? '登录 B站获取字幕' : '连接 B站账号' }}</h2>
        <p id="bili-description">{{ speechOnly ? '需要提取音轨并进行语音转写，耗时通常更长。是否继续生成？' : forSummary ? '此视频的字幕需要 B站登录。扫码后会重新检查字幕并继续；也可以直接使用语音转写。' : '使用哔哩哔哩 App 扫码并确认，后续生成时将优先获取平台字幕。' }}</p>
        <p v-if="!speechOnly" class="bili-privacy">登录状态仅用于你当前账号获取 B站字幕，可随时解除绑定。</p>
        <div v-if="!speechOnly && busy" class="bili-loading" role="status"><LoaderCircle :size="24" class="bili-spin" />正在获取二维码</div>
        <div v-else-if="!speechOnly && qr" class="bili-qr">
          <img v-if="qr.qr_image && qr.status !== 'expired'" :src="qr.qr_image" alt="使用哔哩哔哩 App 扫描此二维码登录" width="220" height="220" />
          <div v-else class="bili-expired"><QrCode :size="50" />二维码已过期</div>
          <p aria-live="polite">{{ qr.status === 'expired' ? '请刷新二维码后重新扫码' : qr.status === 'waiting_confirm' ? '扫码成功，请在 B站 App 中确认登录' : '请使用哔哩哔哩 App 扫码' }}</p>
          <button class="bili-refresh" @click="emit('refresh')"><RefreshCw :size="15" />刷新二维码</button>
        </div>
        <p v-if="error" class="bili-error" role="alert">{{ error }}</p>
        <button v-if="!speechOnly && !busy && !qr" class="bili-primary" @click="emit('refresh')">{{ error ? '重新获取二维码' : '扫码登录获取字幕' }}</button>
        <button v-if="forSummary" :class="speechOnly ? 'bili-primary' : 'bili-secondary'" @click="emit('speech')">继续语音转写</button>
        <button class="bili-cancel" @click="emit('close')">取消</button>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.bili-overlay { position: fixed; inset: 0; z-index: 2000; display: grid; place-items: center; padding: 20px; background: #101c3599; backdrop-filter: blur(5px); }
.bili-dialog { position: relative; width: min(420px, 100%); max-height: calc(100dvh - 40px); overflow-y: auto; padding: 30px; border-radius: 22px; background: #fff; color: #1d293d; box-shadow: 0 24px 90px #0b183a3b; text-align: center; }
.bili-dialog h2 { margin: 12px 0; font-size: 22px; }
.bili-dialog p { line-height: 1.7; font-size: 14px; color: #64748b; }
.bili-dialog button { cursor: pointer; font: inherit; }
.bili-close { position: absolute; top: 16px; right: 16px; display: flex; padding: 6px; border: 0; background: transparent; color: #64748b; }
.bili-icon { display: inline-flex; padding: 12px; border-radius: 16px; background: #edf3ff; color: #4268d9; }
.bili-primary, .bili-secondary { width: 100%; padding: 12px; border-radius: 11px; border: 1px solid #d7e0f0; margin-top: 12px; }
.bili-primary { background: #4167d9; color: #fff; border-color: #4167d9; }
.bili-secondary { background: #f7f9ff; color: #344c88; }
.bili-cancel, .bili-refresh { border: 0; background: transparent; color: #64748b; padding: 12px; }
.bili-refresh { display: inline-flex; align-items: center; gap: 7px; color: #4268d9; }
.bili-qr img { margin: 10px auto 0; display: block; max-width: 100%; height: auto; image-rendering: pixelated; }
.bili-expired { height: 220px; display: flex; flex-direction: column; gap: 18px; align-items: center; justify-content: center; background: #f3f5fa; border-radius: 12px; color: #8591a4; }
.bili-loading { display: flex; align-items: center; justify-content: center; gap: 10px; padding: 45px 0; color: #4268d9; }
.bili-dialog .bili-error { color: #bf3445; background: #fff2f3; padding: 10px; border-radius: 9px; }
.bili-spin { animation: bili-spin 1s linear infinite; }
@keyframes bili-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .bili-spin { animation: none; } }
</style>
