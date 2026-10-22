export type DeviceContext = { device_id: string; client_kind: 'web' }
const key = 'videodream.device.id'
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
let identity = ''

export function currentDeviceId() {
  if (identity) return identity
  try { const stored = localStorage.getItem(key); if (stored && uuid.test(stored)) identity = stored } catch { /* Session-only identity when storage is unavailable. */ }
  if (!identity) {
    identity = crypto.randomUUID()
    try { localStorage.setItem(key, identity) } catch { /* Keep the current page isolated. */ }
  }
  return identity
}

export function deviceContext(): Promise<DeviceContext> {
  return Promise.resolve({ device_id: currentDeviceId(), client_kind: 'web' })
}
