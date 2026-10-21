import { onBeforeUnmount, ref } from 'vue'
import { readApiError } from '../../shared/api/client.ts'

export type BilibiliChoice = 'confirmed' | 'speech' | 'cancel'
type Binding = { bound: boolean; nickname: string; expires_at?: number | null }
type Qr = { login_id: string; status: 'waiting_scan' | 'waiting_confirm' | 'expired' | 'confirmed'; qr_image?: string; expires_at?: number }

export function useBilibiliSession() {
  const bilibiliStatus = ref<Binding>({ bound: false, nickname: '' })
  const bilibiliDialog = ref(false)
  const bilibiliForSummary = ref(false)
  const bilibiliSpeechOnly = ref(false)
  const bilibiliBusy = ref(false)
  const bilibiliError = ref('')
  const bilibiliQr = ref<Qr | null>(null)
  let revision = 0
  let statusRevision = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let resolveChoice: ((choice: BilibiliChoice) => void) | undefined

  async function request<T>(path: string, method = 'GET'): Promise<T> {
    const response = await fetch(`/api/videos/bilibili/${path}`, { method })
    if (!response.ok) throw new Error(await readApiError(response))
    return response.json()
  }

  function stopTimer() { if (timer) clearTimeout(timer); timer = undefined }
  function discardQr(id?: string) {
    if (id) void request(`qr/${encodeURIComponent(id)}`, 'DELETE').catch(() => {})
  }

  function closeBilibiliLogin(choice: BilibiliChoice = 'cancel') {
    revision++
    stopTimer()
    if (choice !== 'confirmed') discardQr(bilibiliQr.value?.login_id)
    bilibiliDialog.value = false
    bilibiliBusy.value = false
    bilibiliQr.value = null
    const settle = resolveChoice
    resolveChoice = undefined
    settle?.(choice)
  }

  async function refreshBilibiliStatus() {
    const selection = ++statusRevision
    try {
      const result = await request<Binding>('session')
      if (selection === statusRevision) bilibiliStatus.value = result
    } catch (failure) {
      if (selection === statusRevision) bilibiliError.value = failure instanceof Error ? failure.message : 'B站登录状态查询失败。'
    }
  }

  function schedulePoll(selection: number) {
    stopTimer()
    timer = setTimeout(() => void pollQr(selection), 3000)
  }

  async function pollQr(selection: number) {
    const qr = bilibiliQr.value
    if (selection !== revision || !bilibiliDialog.value || !qr) return
    if (qr.expires_at && Date.now() >= qr.expires_at * 1000) {
      bilibiliQr.value = { ...qr, status: 'expired' }
      return
    }
    try {
      const result = await request<Qr & Binding>(`qr/${encodeURIComponent(qr.login_id)}`)
      if (selection !== revision) return
      bilibiliError.value = ''
      bilibiliQr.value = { ...qr, ...result }
      if (result.status === 'confirmed') {
        statusRevision++
        bilibiliStatus.value = { bound: true, nickname: result.nickname }
        closeBilibiliLogin('confirmed')
        return
      }
      if (result.status === 'expired') return
    } catch (failure) {
      if (selection !== revision) return
      bilibiliError.value = failure instanceof Error ? failure.message : '扫码状态查询失败，请稍后重试。'
    }
    if (selection === revision) schedulePoll(selection)
  }

  async function refreshBilibiliQr() {
    const selection = ++revision
    stopTimer()
    discardQr(bilibiliQr.value?.login_id)
    bilibiliQr.value = null
    bilibiliBusy.value = true
    bilibiliError.value = ''
    try {
      const result = await request<Qr>('qr', 'POST')
      if (selection !== revision || !bilibiliDialog.value) { discardQr(result.login_id); return }
      bilibiliQr.value = result
      schedulePoll(selection)
    } catch (failure) {
      if (selection === revision) bilibiliError.value = failure instanceof Error ? failure.message : '二维码获取失败，请重试。'
    } finally {
      if (selection === revision) bilibiliBusy.value = false
    }
  }

  function requestBilibiliLogin(): Promise<BilibiliChoice> {
    closeBilibiliLogin()
    bilibiliStatus.value = { bound: false, nickname: '' }
    statusRevision++
    bilibiliForSummary.value = true
    bilibiliSpeechOnly.value = false
    bilibiliError.value = ''
    bilibiliDialog.value = true
    return new Promise(resolve => { resolveChoice = resolve })
  }

  function requestBilibiliSpeechConfirmation(): Promise<BilibiliChoice> {
    closeBilibiliLogin()
    bilibiliForSummary.value = true
    bilibiliSpeechOnly.value = true
    bilibiliError.value = ''
    bilibiliDialog.value = true
    return new Promise(resolve => { resolveChoice = resolve })
  }

  function openBilibiliLogin() {
    closeBilibiliLogin()
    bilibiliForSummary.value = false
    bilibiliSpeechOnly.value = false
    bilibiliDialog.value = true
    void refreshBilibiliQr()
  }

  async function disconnectBilibili() {
    closeBilibiliLogin()
    const selection = ++statusRevision
    bilibiliBusy.value = true
    bilibiliError.value = ''
    try {
      const result = await request<Binding>('session', 'DELETE')
      if (selection === statusRevision) bilibiliStatus.value = result
    } catch (failure) {
      if (selection === statusRevision) bilibiliError.value = failure instanceof Error ? failure.message : 'B站解绑失败，请重试。'
    } finally {
      if (selection === statusRevision) bilibiliBusy.value = false
    }
  }

  function resetBilibiliSession() {
    closeBilibiliLogin()
    statusRevision++
    bilibiliStatus.value = { bound: false, nickname: '' }
    bilibiliError.value = ''
  }
  onBeforeUnmount(resetBilibiliSession)

  return { bilibiliStatus, bilibiliDialog, bilibiliForSummary, bilibiliSpeechOnly, bilibiliBusy, bilibiliError, bilibiliQr,
    refreshBilibiliStatus, requestBilibiliLogin, requestBilibiliSpeechConfirmation, openBilibiliLogin, refreshBilibiliQr, closeBilibiliLogin,
    disconnectBilibili, resetBilibiliSession }
}
