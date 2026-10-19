export async function api<T = any>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, options)
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const body = await response.json()
      message = typeof body.detail === 'string' ? body.detail : body.detail?.[0]?.msg || message
    } catch { /* Non-JSON proxy errors still have an HTTP status. */ }
    throw new Error(message)
  }
  return response.json()
}
export function jsonRequest(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

export async function readApiError(response: Response) {
  try {
    const body = await response.json()
    if (Array.isArray(body.detail)) return body.detail[0]?.msg || '请求失败，请稍后再试。'
    return body.detail?.message || body.detail || '请求失败，请稍后再试。'
  } catch {
    return '请求失败，请稍后再试。'
  }
}
