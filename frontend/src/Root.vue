<script setup lang="ts">
import App from './App.vue'
import LoginView from './LoginView.vue'
import AccountSettings from './AccountSettings.vue'
import { account, logout } from './auth'
import { resetLibrary } from './musicLibrary'
import { queue, stopPlayback } from './musicPlayer'
import { applyMusicTheme, defaultTheme, musicTheme } from './musicTheme'
import { LogOut } from 'lucide-vue-next'
window.addEventListener('videodream-session-cleared', () => {
  stopPlayback()
  queue.value = []
  resetLibrary()
  musicTheme.value = { ...defaultTheme }
  applyMusicTheme(defaultTheme)
  try {
    for (let index = localStorage.length - 1; index >= 0; index--) {
      const key = localStorage.key(index)
      if (key?.startsWith('videodream.')) localStorage.removeItem(key)
    }
  } catch { /* Private browsing can restrict local storage. */ }
})
</script>
<template>
  <LoginView v-if="!account" />
  <main v-else-if="account.must_change_password" class="account-required"><header><strong>VideoDream</strong><button class="account-icon" title="退出登录" aria-label="退出登录" @click="logout"><LogOut :size="20" /></button></header><AccountSettings /></main>
  <App v-else :key="account.id" />
</template>
