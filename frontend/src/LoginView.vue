<script setup lang="ts">
import { ref } from 'vue'
import { Headphones, LoaderCircle, LogIn, UserPlus } from 'lucide-vue-next'
import { authError, authReady, initialized, registrationEnabled, login, register } from './auth'
const username = ref('')
const password = ref('')
const confirmation = ref('')
const registering = ref(false)
const busy = ref(false)
const error = ref('')
async function submit() {
  if (registering.value && password.value !== confirmation.value) { error.value = '两次密码不一致。'; return }
  busy.value = true
  error.value = ''
  try {
    if (registering.value) await register(username.value, password.value)
    else await login(username.value, password.value)
    password.value = confirmation.value = ''
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '登录失败。' }
  finally { busy.value = false }
}
</script>

<template>
  <main class="account-entry">
    <div class="account-brand"><Headphones :size="28" /><span>VideoDream</span></div>
    <form class="account-form" @submit.prevent="submit">
      <h1>{{ registering ? '创建账号' : '账号登录' }}</h1>
      <div v-if="!authReady" class="account-loading"><LoaderCircle class="spin" :size="22" /></div>
      <template v-else>
        <p v-if="!initialized" class="account-note">管理员密码尚未设置，请在本机运行账号初始化脚本。</p>
        <p v-if="error || authError" class="account-error" role="alert">{{ error || authError }}</p>
        <label>用户名<input v-model="username" autocomplete="username" required minlength="3" maxlength="40" pattern="[A-Za-z0-9_][A-Za-z0-9_.-]{2,39}" autofocus /></label>
        <label>密码<input v-model="password" type="password" :autocomplete="registering ? 'new-password' : 'current-password'" required minlength="8" maxlength="128" /></label>
        <label v-if="registering">确认密码<input v-model="confirmation" type="password" autocomplete="new-password" required minlength="8" maxlength="128" /></label>
        <button class="account-primary" type="submit" :disabled="busy || !initialized"><LoaderCircle v-if="busy" class="spin" :size="18" /><UserPlus v-else-if="registering" :size="18" /><LogIn v-else :size="18" />{{ registering ? '注册并登录' : '登录' }}</button>
        <button v-if="registrationEnabled" type="button" class="account-text" @click="registering = !registering; error = ''">{{ registering ? '返回登录' : '创建账号' }}</button>
      </template>
    </form>
  </main>
</template>
