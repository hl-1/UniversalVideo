<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ImagePlus, LoaderCircle, RotateCcw, X } from 'lucide-vue-next'
import TrackCover from './TrackCover.vue'
import { api } from './musicLibrary'

const props = defineProps<{ title: string; endpoint: string; image?: string | null }>()
const emit = defineEmits<{ close: []; saved: [result: { thumbnail?: string | null; avatar?: string | null }] }>()
const busy = ref(false)
const error = ref('')
const input = ref<HTMLInputElement | null>(null)
const dialog = ref<HTMLElement | null>(null)
const previousFocus = document.activeElement as HTMLElement | null
async function upload(event: Event) {
  const control = event.target as HTMLInputElement
  const file = control.files?.[0]
  control.value = ''
  if (!file) return
  const form = new FormData()
  form.append('file', file)
  await save({ method: 'POST', body: form })
}
async function save(options: RequestInit) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try { emit('saved', await api(props.endpoint, options)); emit('close') }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '图片保存失败。' }
  finally { busy.value = false }
}
function close() { if (!busy.value) emit('close') }
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); close() }
  if (event.key !== 'Tab') return
  const controls = Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled)') || [])
  const first = controls[0], last = controls[controls.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(() => { void nextTick(() => dialog.value?.querySelector('button')?.focus()) })
onBeforeUnmount(() => previousFocus?.isConnected && previousFocus.focus())
</script>

<template>
  <Teleport to="body"><div class="mc-modal-backdrop" @click.self="close"><section ref="dialog" class="mc-modal image-settings" role="dialog" aria-modal="true" aria-labelledby="image-settings-title" @keydown="key"><header><h2 id="image-settings-title">{{ title }}</h2><button type="button" class="mc-icon" :disabled="busy" title="关闭" aria-label="关闭" @click="close"><X :size="20" /></button></header><TrackCover :src="image" :title="title" /><p v-if="error" class="mc-modal-error" role="alert">{{ error }}</p><input ref="input" type="file" accept="image/jpeg,image/png,image/webp" aria-label="选择自定义图片" hidden @change="upload" /><footer><button type="button" class="mc-command" :disabled="busy" @click="save({ method: 'DELETE' })"><RotateCcw :size="17" />恢复默认</button><button type="button" class="mc-command primary" :disabled="busy" @click="input?.click()"><LoaderCircle v-if="busy" :size="17" class="spin" /><ImagePlus v-else :size="17" />上传图片</button></footer></section></div></Teleport>
</template>
