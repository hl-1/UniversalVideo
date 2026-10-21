export type MediaTransport = {
  fetch: (url: string, options: RequestInit) => Promise<Response>
  refresh: () => Promise<boolean>
}

export function downloadUrl(value: string, base = location.href): string {
  const url = new URL(value, base)
  if (url.origin !== new URL(base).origin) throw new Error('下载地址无效。')
  url.searchParams.set('download', '1')
  return url.pathname + url.search + url.hash
}

export function mediaFailureMessage(status: number, code = 0): string {
  if (status === 401) return '登录已失效，请重新登录后播放。'
  if (status === 403) return '当前账号暂时无法使用此功能，请检查权限或密码修改要求。'
  if (status === 404) return '文件不存在或无权访问。'
  if (status === 410) return '播放地址已过期，请重新获取音源。'
  if (status === 502) return '音源暂时不可用，请稍后重试。'
  if (status === 503) return '服务暂不可用，请稍后重试。'
  if (status >= 200 && status < 300 && [3, 4].includes(code)) return '文件格式无法解码或内容异常，请重新下载。'
  return '媒体连接中断，请检查网络后重试。'
}

export async function probeMediaWith(url: string, transport: MediaTransport, signal?: AbortSignal) {
  const probe = async () => {
    const response = await transport.fetch(url, { headers: { Range: 'bytes=0-0' }, signal })
    await response.body?.cancel().catch(() => undefined)
    return response.status
  }
  let status = await probe()
  let recovered = false
  if (status === 401 && await transport.refresh()) {
    status = await probe()
    recovered = status >= 200 && status < 300
  }
  return { status, recovered }
}
