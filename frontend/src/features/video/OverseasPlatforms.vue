<script setup lang="ts">
import { ref } from 'vue'
import { account } from '../accounts/auth'
import { api } from '../../shared/api/client'

type Check = { platform: string; connected: boolean; available: boolean; status?: number; elapsed_ms?: number; message?: string }
const checking = ref(false)
const message = ref('')
const results = ref<Check[]>([])
async function checkConnection() {
  checking.value = true
  results.value = []
  try {
    const data = await api<{ results: Check[]; message: string }>('/api/admin/video-network/check', { method: 'POST' })
    results.value = data.results
    message.value = data.message
  } catch (error) {
    message.value = error instanceof Error ? error.message : '代理检测失败，请稍后重试。'
  } finally {
    checking.value = false
  }
}
</script>

<template>
  <section v-if="account?.role === 'super_admin'" class="overseas-platforms" aria-label="海外平台">
    <div class="overseas-heading"><strong>海外平台 · 超级管理员</strong><button type="button" class="secondary-action compact" :disabled="checking" @click="checkConnection">{{ checking ? '检测中…' : '检查代理连接' }}</button></div>
    <div class="tag-grid"><a href="https://www.youtube.com/" target="_blank" rel="noreferrer">YouTube</a><a href="https://www.pornhub.com/" target="_blank" rel="noreferrer">Pornhub</a></div>
    <p>使用服务器配置的代理下载；两站的音频、预览和衍生文件也仅供超级管理员使用。</p>
    <div role="status" aria-live="polite"><p v-for="result in results" :key="result.platform">{{ result.platform }}：{{ result.available ? `连接正常 · ${result.elapsed_ms} ms` : result.connected ? `站点返回 HTTP ${result.status}` : result.message }}</p><p v-if="message">{{ message }}</p></div>
  </section>
</template>

<style scoped>
.overseas-platforms{margin-top:24px;padding-top:20px;border-top:1px solid var(--line,#e3e8ed)}.overseas-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px}.overseas-platforms p{font-size:13px;line-height:1.7;color:var(--muted,#667085);margin:10px 0 0}
</style>
