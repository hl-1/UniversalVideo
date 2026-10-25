import type { DownloadProgress, SaveResult } from '../downloads'
export async function saveWithAndroid<T = SaveResult>(_method: string, _params: unknown, _signal?: AbortSignal, _onProgress?: (progress: DownloadProgress) => void): Promise<T> {
  throw new Error('浏览器下载请使用网页保存入口。')
}
export async function saveBlobWithAndroid(_blob: Blob, _name: string): Promise<SaveResult> {
  throw new Error('浏览器下载请使用网页保存入口。')
}
