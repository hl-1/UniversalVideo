<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { KeyRound, LoaderCircle } from 'lucide-vue-next'
import { account, changePassword } from './auth'
import { api } from './musicLibrary'
const old = ref('')
const password = ref('')
const confirmation = ref('')
const busy = ref(false)
const error = ref('')
const records = ref<Array<{ result: string; ip: string; created_at: number }>>([])
const page = ref(1)
const total = ref(0)
async function load() {
  try {
    const data = await api(`/api/auth/login-records?page=${page.value}`, { headers: {} })
    records.value = data.items
    total.value = data.total
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '登录记录加载失败。' }
}
async function save() {
  if (password.value !== confirmation.value) { error.value = '两次新密码不一致。'; return }
  busy.value = true
  error.value = ''
  try { await changePassword(old.value, password.value) }
  catch (failure) { error.value = failure instanceof Error ? failure.message : '密码修改失败。' }
  finally { busy.value = false }
}
onMounted(() => { if (!account.value?.must_change_password) void load() })
</script>

<template>
  <section class="account-security">
    <h2>{{ account?.must_change_password ? '修改临时密码' : '账号安全' }}</h2>
    <p v-if="account?.must_change_password" class="account-note">请设置新密码后重新登录。</p>
    <p v-if="error" class="account-error" role="alert">{{ error }}</p>
    <form class="account-password" @submit.prevent="save">
      <label>{{ account?.must_change_password ? '临时密码' : '原密码' }}<input v-model="old" type="password" autocomplete="current-password" required maxlength="128" /></label>
      <label>新密码<input v-model="password" type="password" autocomplete="new-password" required minlength="8" maxlength="128" /></label>
      <label>确认新密码<input v-model="confirmation" type="password" autocomplete="new-password" required minlength="8" maxlength="128" /></label>
      <button class="account-primary" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="17" /><KeyRound v-else :size="17" />修改密码</button>
    </form>
    <template v-if="!account?.must_change_password">
      <h2>登录记录</h2>
      <div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>时间</th><th>来源 IP</th><th>结果</th></tr></thead><tbody><tr v-for="record in records" :key="record.created_at"><td>{{ new Date(record.created_at * 1000).toLocaleString() }}</td><td>{{ record.ip }}</td><td>{{ record.result === 'success' ? '成功' : '失败' }}</td></tr><tr v-if="!records.length"><td colspan="3">暂无登录记录</td></tr></tbody></table></div>
      <div class="admin-pagination"><button :disabled="page <= 1" @click="page--; load()">上一页</button><span>{{ page }} / {{ Math.max(1, Math.ceil(total / 20)) }}</span><button :disabled="page * 20 >= total" @click="page++; load()">下一页</button></div>
    </template>
  </section>
</template>
