import type { FilePickerHandle, FilePickerWritable } from './types.ts'

const SAVE_IDLE_TIMEOUT_MS = 60_000
const WRITE_BATCH_BYTES = 1024 * 1024

// Bound each stalled operation, rather than the duration of the whole file.
export function waitForFileSave<T>(operation: () => Promise<T>, controller: AbortController, timeoutMs = SAVE_IDLE_TIMEOUT_MS): Promise<T> {
  const { signal } = controller
  return new Promise<T>((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return }
    const timer = setTimeout(() => controller.abort(new Error('保存超过 60 秒没有进展，请重试另存为。')), timeoutMs)
    const aborted = () => { cleanup(); reject(signal.reason) }
    function cleanup() { clearTimeout(timer); signal.removeEventListener('abort', aborted) }
    signal.addEventListener('abort', aborted, { once: true })
    Promise.resolve().then(() => {
      signal.throwIfAborted()
      return operation()
    }).then(value => { cleanup(); resolve(value) }, error => { cleanup(); reject(error) })
  })
}

export async function saveResponseToFile(
  response: Response,
  handle: FilePickerHandle,
  controller: AbortController,
  onProgress: (written: number, total: number | null) => void,
  onClosing: () => void,
  timeoutMs = SAVE_IDLE_TIMEOUT_MS,
) {
  let writable: FilePickerWritable | undefined
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  const wait = <T>(operation: () => Promise<T>) => waitForFileSave(operation, controller, timeoutMs)
  const encoding = response.headers.get('Content-Encoding')
  const length = Number(response.headers.get('Content-Length'))
  const total = (!encoding || encoding === 'identity') && Number.isSafeInteger(length) && length > 0 ? length : null
  let written = 0
  try {
    writable = await wait(async () => {
      const stream = await handle.createWritable()
      // createWritable itself cannot be aborted; discard a late result.
      if (controller.signal.aborted) {
        void stream.abort?.().catch(() => {})
        controller.signal.throwIfAborted()
      }
      return stream
    })
    reader = response.body?.getReader()
    onProgress(0, total)
    if (reader) {
      const buffer = new Uint8Array(WRITE_BATCH_BYTES)
      let buffered = 0
      const flush = async () => {
        if (!buffered) return
        await wait(() => writable!.write(buffer.subarray(0, buffered)))
        written += buffered
        buffered = 0
        onProgress(written, total)
      }
      while (true) {
        const { done, value } = await wait(() => reader!.read())
        if (done) break
        let offset = 0
        while (offset < value.byteLength) {
          const count = Math.min(buffer.byteLength - buffered, value.byteLength - offset)
          buffer.set(value.subarray(offset, offset + count), buffered)
          buffered += count
          offset += count
          if (buffered === buffer.byteLength) await flush()
        }
      }
      await flush()
    } else {
      const blob = await wait(() => response.blob())
      await wait(() => writable!.write(blob))
      written = blob.size
      onProgress(written, total)
    }
    if (!written || (total !== null && written !== total)) throw new Error('保存的数据不完整，请重试另存为。')
    onClosing()
    await wait(() => writable!.close())
  } catch (error) {
    // Cleanup must not leave the UI blocked behind a stalled read/write.
    void reader?.cancel().catch(() => {})
    if (!reader) void response.body?.cancel().catch(() => {})
    void writable?.abort?.().catch(() => {})
    throw error
  } finally {
    try { reader?.releaseLock() } catch { /* A cancelled reader may still be settling. */ }
  }
}
