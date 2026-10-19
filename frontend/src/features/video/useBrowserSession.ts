import { computed, onBeforeUnmount, ref, type Ref } from 'vue'
import { readApiError } from '../../shared/api/client'

export function useBrowserSession(loadingParse: Ref<boolean>, errorMessage: Ref<string>) {
  type BrowserSession = { status: 'closed' | 'waiting' | 'open' | 'expired' | 'error'; visible: boolean; expires_in: number; url?: string }
  const browserSession = ref<BrowserSession | null>(null)
  const verificationTarget = ref('')
  const browserBusy = ref(false)
  let browserTimer: number | undefined
  let browserRevision = 0
  let browserDisposed = false
  const verificationMessage = computed(() => {
    if (browserBusy.value) return '正在更新验证窗口…'
    if (browserSession.value?.visible) return browserSession.value.status === 'waiting' ? '抖音正在等待安全验证' : '验证窗口已打开'
    if (browserSession.value?.status === 'expired') return '验证窗口已超时关闭，会话已保留'
    if (browserSession.value?.status === 'error') return '验证窗口未能打开'
    if (browserSession.value?.status === 'closed') return '验证窗口已关闭，会话已保留'
    return '抖音浏览器会话已保留'
  })
  function stopBrowserPolling() {
    browserRevision++
    if (browserTimer !== undefined) window.clearTimeout(browserTimer)
    browserTimer = undefined
  }

  async function refreshBrowserSession() {
    stopBrowserPolling()
    const revision = browserRevision
    try {
      const response = await fetch('/api/browser-session')
      if (!response.ok) throw new Error(await readApiError(response))
      const data: BrowserSession = await response.json()
      if (browserDisposed || browserRevision !== revision) return
      browserSession.value = data
      if (browserSession.value?.visible) {
        verificationTarget.value ||= browserSession.value.url || ''
        browserTimer = window.setTimeout(() => void refreshBrowserSession(), 3000)
      }
    } catch (error) {
      if (!browserDisposed && browserRevision === revision && verificationTarget.value) errorMessage.value = error instanceof Error ? error.message : '读取验证窗口状态失败。'
    }
  }

  async function openVerificationBrowser() {
    if (!verificationTarget.value || browserBusy.value || loadingParse.value) return
    browserBusy.value = true
    errorMessage.value = ''
    stopBrowserPolling()
    try {
      const response = await fetch('/api/browser-session/open', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: verificationTarget.value }),
      })
      if (!response.ok) throw new Error(await readApiError(response))
      browserSession.value = await response.json()
      if (!browserDisposed) browserTimer = window.setTimeout(() => void refreshBrowserSession(), 3000)
    } catch (error) {
      errorMessage.value = error instanceof Error ? error.message : '打开验证窗口失败。'
    } finally {
      browserBusy.value = false
    }
  }

  async function closeVerificationBrowser(reset = false) {
    if (browserBusy.value || loadingParse.value) return
    browserBusy.value = true
    stopBrowserPolling()
    try {
      const response = await fetch(`/api/browser-session?reset=${reset}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await readApiError(response))
      browserSession.value = await response.json()
      if (reset) verificationTarget.value = ''
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value = error instanceof Error ? error.message : '关闭验证窗口失败。'
    } finally {
      browserBusy.value = false
    }
  }


  onBeforeUnmount(() => { browserDisposed = true; stopBrowserPolling() })
  return { browserSession, verificationTarget, browserBusy, verificationMessage, refreshBrowserSession, openVerificationBrowser, closeVerificationBrowser, stopBrowserPolling }
}
