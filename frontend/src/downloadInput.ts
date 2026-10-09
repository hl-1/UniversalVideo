export function extractHttpUrl(text: string): string | null {
  const match = text.match(/https?:\/\/[^\s<>"']+/i)
  if (!match) return null
  const candidate = match[0].replace(/[.,;!?\uFF0C\u3002\uFF1B\uFF01\uFF1F\u3001\uFF09)\]\u3011]+$/, '')
  try {
    const url = new URL(candidate)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null
  } catch {
    return null
  }
}
