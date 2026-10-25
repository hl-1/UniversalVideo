/** Browser compatibility for optional native integrations. */
export function isAndroidApp(): boolean { return false }
export async function nativeCall<T = any>(_method: string, _params?: unknown, _options?: { timeout?: number }): Promise<T> {
  throw new Error('当前为浏览器环境，请使用网页提供的操作。')
}
