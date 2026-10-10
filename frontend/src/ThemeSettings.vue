<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Check, ImagePlus, LoaderCircle, Moon, RotateCcw, Save, Sun, Trash2, X } from 'lucide-vue-next'
import { api } from './musicLibrary'
import { applyMusicTheme, defaultTheme, musicTheme, saveMusicTheme, validThemeColors, type MusicTheme } from './musicTheme'

const draft = ref<MusicTheme>({ ...musicTheme.value })
const busy = ref(false)
const error = ref('')
const status = ref('')
const backgroundInput = ref<HTMLInputElement | null>(null)
const valid = computed(() => validThemeColors(draft.value))
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(musicTheme.value))
const presets = [
  { name: '清浅', mode: 'light' as const, accent: '#109b6d', background: '#fafbf9', text: '#252c29' },
  { name: '深夜', mode: 'dark' as const, accent: '#55c7a4', background: '#181b1f', text: '#f4f5f6' },
  { name: '蔷薇', mode: 'light' as const, accent: '#b84363', background: '#fcfafb', text: '#33282e' },
  { name: '海风', mode: 'light' as const, accent: '#187fa0', background: '#f6fafc', text: '#263238' },
]
const colorFields = [{ key: 'accent' as const, label: '主题色' }, { key: 'background' as const, label: '背景色' }, { key: 'text' as const, label: '文字颜色' }]
function preset(value: typeof presets[number]) {
  draft.value = { ...draft.value, mode: value.mode, accent: value.accent, background: value.background, text: value.text }
}
function mode(value: 'light' | 'dark') { preset(presets[value === 'dark' ? 1 : 0]!) }
function cancel() { draft.value = { ...musicTheme.value }; error.value = ''; status.value = '' }
async function upload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.size > 30 * 1024 * 1024) { error.value = '请选择 30 MB 以内的背景图片。'; return }
  busy.value = true
  error.value = ''
  try {
    const form = new FormData()
    form.append('file', file)
    const response = await api<{ background_image: string }>('/api/library/theme/background', { method: 'POST', body: form })
    draft.value.background_image = response.background_image
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '背景上传失败。' }
  finally { busy.value = false }
}
async function save() {
  if (!valid.value || busy.value) return
  busy.value = true
  error.value = ''
  try { await saveMusicTheme(draft.value); status.value = '主题已保存' }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '主题保存失败。' }
  finally { busy.value = false }
}
watch(draft, value => { applyMusicTheme(value); status.value = '' }, { deep: true })
onBeforeUnmount(() => applyMusicTheme(musicTheme.value))
</script>

<template>
  <form class="appearance-settings" @submit.prevent="save">
    <div class="appearance-section"><h2>配色</h2>
      <div class="appearance-modes" role="group" aria-label="主题模式"><button type="button" :aria-pressed="draft.mode === 'light'" @click="mode('light')"><Sun :size="17" />浅色</button><button type="button" :aria-pressed="draft.mode === 'dark'" @click="mode('dark')"><Moon :size="17" />深色</button></div>
      <div class="appearance-presets"><label v-for="item in presets" :key="item.name"><button type="button" class="theme-swatch" :title="item.name" :aria-label="`${item.name}配色`" :style="{ background: item.background, borderColor: item.accent, color: item.accent }" @click="preset(item)"><Check v-if="draft.accent === item.accent && draft.background === item.background && draft.text === item.text" :size="18" /><span v-else :style="{ background: item.accent }" /></button><span>{{ item.name }}</span></label></div>
      <div class="appearance-colors"><label v-for="field in colorFields" :key="field.key"><span>{{ field.label }}</span><div><input v-model="draft[field.key]" type="color" :aria-label="field.label" /><input v-model="draft[field.key]" type="text" maxlength="7" :aria-label="`${field.label} HEX`" spellcheck="false" /></div></label></div>
      <p v-if="!valid" class="appearance-error" role="alert">请使用有效的 HEX 颜色，并提高文字与背景的对比度。</p>
    </div>
    <div class="appearance-section"><div class="appearance-section-title"><h2>背景图片</h2><button type="button" class="mc-icon" :disabled="busy" title="上传背景图片" aria-label="上传背景图片" @click="backgroundInput?.click()"><LoaderCircle v-if="busy" :size="19" class="spin" /><ImagePlus v-else :size="19" /></button><button type="button" class="mc-icon" :disabled="!draft.background_image || busy" title="移除背景图片" aria-label="移除背景图片" @click="draft.background_image = null"><Trash2 :size="18" /></button></div>
      <input ref="backgroundInput" type="file" accept="image/jpeg,image/png,image/webp" aria-label="选择主题背景图片" hidden @change="upload" />
      <button v-if="!draft.background_image" class="appearance-upload" type="button" :disabled="busy" @click="backgroundInput?.click()"><ImagePlus :size="24" /><span>选择背景图片</span></button>
      <img v-else class="appearance-image" :src="draft.background_image" alt="当前背景图片" />
      <div v-if="draft.background_image" class="appearance-background-controls"><label>填充方式<select v-model="draft.fit" aria-label="背景填充方式"><option value="cover">铺满</option><option value="contain">完整显示</option><option value="tile">平铺</option></select></label><label>图片位置<select v-model="draft.position" aria-label="背景图片位置"><option value="center">居中</option><option value="top">顶部</option><option value="bottom">底部</option></select></label><label>遮罩 <output>{{ draft.overlay }}%</output><input v-model.number="draft.overlay" type="range" min="0" max="100" aria-label="背景遮罩" /></label><label>模糊 <output>{{ draft.blur }} px</output><input v-model.number="draft.blur" type="range" min="0" max="24" aria-label="背景模糊" /></label></div>
    </div>
    <p v-if="error" class="appearance-error" role="alert">{{ error }}</p><p v-if="status" role="status">{{ status }}</p>
    <footer class="appearance-actions"><button type="button" class="mc-command" :disabled="busy" @click="draft = { ...defaultTheme }"><RotateCcw :size="17" />恢复默认</button><button type="button" class="mc-command" :disabled="busy || !dirty" @click="cancel"><X :size="17" />取消</button><button type="submit" class="mc-command primary" :disabled="busy || !valid || !dirty"><LoaderCircle v-if="busy" :size="17" class="spin" /><Save v-else :size="17" />保存主题</button></footer>
  </form>
</template>
