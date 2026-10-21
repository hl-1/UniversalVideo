import { mediaFetch, refreshSession } from '../features/accounts/auth'
import { probeMediaWith } from './mediaPolicy'

export { downloadUrl, mediaFailureMessage } from './mediaPolicy'

export function probeMedia(url: string, signal?: AbortSignal) {
  return probeMediaWith(url, { fetch: mediaFetch, refresh: refreshSession }, signal)
}
