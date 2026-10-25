import { isAndroidApp, nativeCall } from './native/bridge'

export type DeviceContext = { device_id: string; client_kind: 'web' | 'android' }
const key = 'videodream.device.id'
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
let identity = ''
let pending: Promise<DeviceContext> | undefined

export function currentDeviceId() {
  if (identity || isAndroidApp()) return identity
  try { const stored = localStorage.getItem(key); if (stored && uuid.test(stored)) identity = stored } catch { /* Session-only identity when storage is unavailable. */ }
  if (!identity) {
    identity = crypto.randomUUID()
    try { localStorage.setItem(key, identity) } catch { /* Keep the current page isolated. */ }
  }
  return identity
}

export function deviceContext(): Promise<DeviceContext> {
  if (!isAndroidApp()) return Promise.resolve({ device_id: currentDeviceId(), client_kind: 'web' })
  return pending ||= nativeCall<DeviceContext>('device.info').then(value => {
    if (!uuid.test(value.device_id) || value.client_kind !== 'android') throw new Error('设备身份不可用，请更新应用。')
    identity = value.device_id
    return value
  }).catch(error => { pending = undefined; throw error })
}
