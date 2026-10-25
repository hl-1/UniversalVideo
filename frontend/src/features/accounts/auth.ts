import { ref } from 'vue'
import { isAndroidApp, nativeCall } from '../../shared/native/bridge'
import { deviceContext } from '../../shared/device'

export type Account = { id: string; legacy_id?: string | null; username: string; role: 'user' | 'super_admin'; status: 'active' | 'disabled'; must_change_password: boolean; created_at: number }
export const account = ref<Account | null>(null)
export const authReady = ref(false)
export const authError = ref('')
export const registrationEnabled = ref(false)
export const initialized = ref(true)
export type CaptchaChallenge = { captcha_id: string; image: string; expires_in: number; legacy?: boolean }
export type CaptchaAnswer = { captcha_id: string; captcha_code: string }
const nativeFetch = window.fetch.bind(window)
let accessToken = ''
let expiresAt = 0
let refreshPromise: Promise<boolean> | undefined
let epoch = 0
let discoveryPreparationKey = ''
const events = typeof BroadcastChannel === 'undefined' ? undefined : new BroadcastChannel('videodream-account-events')

function clearSession(broadcast = false) {
  epoch++
  discoveryPreparationKey = ''
  accessToken = ''
  expiresAt = 0
  account.value = null
  window.dispatchEvent(new Event('videodream-session-cleared'))
  if (broadcast) events?.postMessage('logout')
}
if (events) events.onmessage = event => { if (event.data === 'logout') clearSession() }

async function authRequest(path: string, body?: unknown) {
  if (isAndroidApp()) { await deviceContext(); return nativeCall('auth.request', { path, body }) }
  const device = await deviceContext()
  const response = await nativeFetch(`/api/auth/${path}`, { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-VideoDream-CSRF': '1', 'X-VideoDream-Device': device.device_id, 'X-VideoDream-Client': device.client_kind }, body: body ? JSON.stringify(body) : undefined })
  const data = await response.json()
  if (!response.ok) throw Object.assign(new Error(data.detail || '认证服务暂不可用。'), { status: response.status })
  return data
}

function accept(data: { access_token: string; expires_in: number; user: Account }) {
  accessToken = data.access_token
  expiresAt = Date.now() + data.expires_in * 1000
  account.value = data.user
  authError.value = ''
  const day = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date())
  const key = `${epoch}:${data.user.id}:${day}`
  if (!data.user.must_change_password && discoveryPreparationKey !== key) {
    discoveryPreparationKey = key
    // Defer until refreshPromise has resolved; preparing recommendations never delays authentication.
    setTimeout(() => {
      if (key !== discoveryPreparationKey) return
      void authenticatedFetch('/api/music/discovery/prepare', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .then(() => { if (key === discoveryPreparationKey) window.dispatchEvent(new Event('videodream-discovery-ready')) })
        .catch(() => undefined)
    }, 0)
  }
}

export async function refreshSession(): Promise<boolean> {
  if (refreshPromise) return refreshPromise
  const revision = epoch
  const refresh = async () => {
    try {
      const data = await authRequest('refresh')
      if (revision !== epoch) return false
      accept(data)
      return true
    } catch (failure) {
      if (revision !== epoch) return false
      if ((failure as { status?: number }).status === 401) clearSession()
      else authError.value = failure instanceof Error ? failure.message : '认证服务暂不可用。'
      return false
    }
  }
  const pending = (async () => navigator.locks ? await navigator.locks.request('videodream-refresh', refresh) : await refresh())()
    .finally(() => { refreshPromise = undefined })
  refreshPromise = pending
  return pending
}

async function token(force = false) {
  if (force || !accessToken || Date.now() > expiresAt - 30000) await refreshSession()
  return accessToken
}

export async function authenticatedFetch(input: RequestInfo | URL, options?: RequestInit): Promise<Response> {
  const url = new URL(input instanceof Request ? input.url : String(input), window.location.href)
  if (url.origin !== location.origin || !url.pathname.startsWith('/api/') || ['/api/auth/status', '/api/auth/captcha', '/api/auth/login', '/api/auth/register', '/api/auth/refresh', '/api/auth/logout'].includes(url.pathname)) return nativeFetch(input, options)
  const revision = epoch
  const request = new Request(input, options)
  const saved = request.clone()
  const headers = new Headers(request.headers)
  const device = await deviceContext()
  headers.set('X-VideoDream-Device', device.device_id)
  headers.set('X-VideoDream-Client', device.client_kind)
  const credential = await token()
  if (credential) headers.set('Authorization', `Bearer ${credential}`)
  let response = await nativeFetch(new Request(request, { headers, credentials: 'same-origin' }))
  if (revision !== epoch) throw new DOMException('Session changed', 'AbortError')
  if (response.status === 401 && await refreshSession()) {
    headers.set('Authorization', `Bearer ${accessToken}`)
    response = await nativeFetch(new Request(saved, { headers, credentials: 'same-origin' }))
  }
  if (revision !== epoch) throw new DOMException('Session changed', 'AbortError')
  if (response.status === 401) clearSession()
  if (response.status === 403 && account.value && (await response.clone().text()).includes('临时密码')) {
    account.value = { ...account.value, must_change_password: true }
  }
  return response
}

export async function loadCaptcha(purpose: 'login' | 'register'): Promise<CaptchaChallenge> {
  const response = await nativeFetch(`/api/auth/captcha?purpose=${purpose}`, { cache: 'no-store', credentials: 'same-origin' })
  // Earlier deployed backends protect unknown /api routes with 401. Server-side
  // login validation remains authoritative; web clients keep requiring captcha.
  if (isAndroidApp() && [401, 404].includes(response.status)) return { captcha_id: '', image: '', expires_in: 0, legacy: true }
  const data = await response.json()
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '验证码加载失败，请重试。')
  return data
}

export async function login(username: string, password: string, captcha: CaptchaAnswer) {
  const data = await authRequest('login', { username, password, ...captcha })
  clearSession(true)
  accept(data)
}

export async function register(username: string, password: string, captcha: CaptchaAnswer) {
  const data = await authRequest('register', { username, password, ...captcha })
  if (isAndroidApp() && !data.access_token) {
    await login(username, password, captcha)
    return
  }
  clearSession(true)
  accept(data)
}

export async function logout() {
  await authRequest('logout')
  clearSession(true)
}

export async function changePassword(old_password: string, new_password: string) {
  const response = await authenticatedFetch('/api/auth/password', { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${await token()}` }, body: JSON.stringify({ old_password, new_password }) })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '密码修改失败。')
  clearSession(true)
  if (isAndroidApp()) void nativeCall('auth.clear').catch(() => {})
}

export async function initializeAuth() {
  window.fetch = authenticatedFetch
  if (!isAndroidApp() && 'serviceWorker' in navigator && navigator.serviceWorker) {
    // Keep answering old workers until the pass-through worker takes control.
    navigator.serviceWorker.addEventListener('message', event => {
      if (event.data?.type === 'videodream-token') {
        void token(Boolean(event.data.force)).then(value => event.ports[0]?.postMessage({ token: value }))
      }
    })
    void navigator.serviceWorker.register('/auth-worker.js', { scope: '/', updateViaCache: 'none' })
      .then(registration => registration.update()).catch(() => undefined)
  }
  try {
    const response = await nativeFetch('/api/auth/status')
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '认证服务暂不可用。')
    initialized.value = data.initialized
    registrationEnabled.value = data.registration_enabled
    if (initialized.value) await refreshSession()
  } catch (failure) {
    authError.value = failure instanceof Error ? failure.message : '认证初始化失败。'
  } finally { authReady.value = true }
}

if (typeof window !== 'undefined') {
  window.addEventListener('xianyue:session.expired', () => clearSession())
  window.addEventListener('xianyue-resume', () => { if (isAndroidApp() && authReady.value) void refreshSession() })
}

export function mediaFetch(url: string, options?: RequestInit) {
  const target = new URL(url, location.href)
  if (target.origin !== location.origin || !target.pathname.startsWith('/api/')) throw new Error('媒体地址无效。')
  return nativeFetch(target, { ...options, credentials: 'same-origin', cache: 'no-store' })
}
