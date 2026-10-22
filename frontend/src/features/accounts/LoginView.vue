<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { Headphones, LoaderCircle, LogIn, UserPlus } from 'lucide-vue-next'
import { authError, authReady, initialized, registrationEnabled, login, register, loadCaptcha, type CaptchaChallenge } from './auth'
const username = ref('')
const password = ref('')
const confirmation = ref('')
const registering = ref(false)
const busy = ref(false)
const error = ref('')
const captcha = ref<CaptchaChallenge | null>(null)
const captchaCode = ref('')
const captchaLoading = ref(false)
const captchaError = ref('')
let captchaRevision = 0
async function refreshCaptcha() {
  const revision = ++captchaRevision
  captcha.value = null
  captchaCode.value = ''
  captchaError.value = ''
  captchaLoading.value = true
  try {
    const challenge = await loadCaptcha(registering.value ? 'register' : 'login')
    if (revision === captchaRevision) captcha.value = challenge
  } catch (failure) {
    if (revision === captchaRevision) captchaError.value = failure instanceof Error ? failure.message : '验证码加载失败，请重试。'
  } finally { if (revision === captchaRevision) captchaLoading.value = false }
}
watch([authReady, initialized, registering], () => {
  if (authReady.value && initialized.value) void refreshCaptcha()
}, { immediate: true })
onBeforeUnmount(() => { captchaRevision++ })
async function submit() {
  if (busy.value || captchaLoading.value || !captcha.value) return
  if (registering.value && password.value !== confirmation.value) { error.value = '两次密码不一致。'; return }
  busy.value = true
  error.value = ''
  try {
    const answer = { captcha_id: captcha.value.captcha_id, captcha_code: captchaCode.value }
    if (registering.value) await register(username.value, password.value, answer)
    else await login(username.value, password.value, answer)
    password.value = confirmation.value = ''
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : '登录失败。'
    await refreshCaptcha()
  }
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
        <div v-if="initialized" class="account-captcha">
          <label for="account-captcha-code">验证码</label>
          <div class="account-captcha-row">
            <input id="account-captcha-code" v-model="captchaCode" autocomplete="off" autocapitalize="characters" :spellcheck="false" placeholder="输入图片字符" required minlength="4" maxlength="4" pattern="[A-Za-z0-9]{4}" :disabled="busy || captchaLoading || !captcha" aria-describedby="account-captcha-note" />
            <button type="button" class="account-captcha-image" :disabled="busy || captchaLoading" title="换一张验证码" aria-label="换一张验证码" @click="refreshCaptcha">
              <LoaderCircle v-if="captchaLoading" class="spin" :size="20" /><img v-else-if="captcha" :src="captcha.image" alt="图片验证码" /><span v-else>点击重试</span>
            </button>
          </div>
          <p id="account-captcha-note" :class="{ 'account-captcha-error': captchaError }" :role="captchaError ? 'alert' : undefined">{{ captchaError || '不区分大小写，2 分钟内有效；点击图片换一张。' }}</p>
        </div>
        <button class="account-primary" type="submit" :disabled="busy || !initialized || captchaLoading || !captcha"><LoaderCircle v-if="busy" class="spin" :size="18" /><UserPlus v-else-if="registering" :size="18" /><LogIn v-else :size="18" />{{ registering ? '注册并登录' : '登录' }}</button>
        <button v-if="registrationEnabled" type="button" class="account-text" :disabled="busy" @click="registering = !registering; error = ''">{{ registering ? '返回登录' : '创建账号' }}</button>
      </template>
    </form>
  </main>
</template>
