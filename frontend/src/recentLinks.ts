export const RECENT_LINKS_KEY = 'videodream.recentLinks'
export const RECENT_LINKS_LIMIT = 5

type LinkStorage = {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

function normalizeLink(link: string) {
  return link.trim()
}

function uniqueRecentLinks(links: string[]) {
  const seen = new Set<string>()
  const result: string[] = []

  for (const link of links) {
    const normalized = normalizeLink(link)
    if (!normalized || seen.has(normalized)) continue
    seen.add(normalized)
    result.push(normalized)
    if (result.length >= RECENT_LINKS_LIMIT) break
  }

  return result
}

export function getRecentLinks(storage: LinkStorage, key = RECENT_LINKS_KEY) {
  try {
    const raw = storage.getItem(key)
    const parsed = raw ? JSON.parse(raw) : []
    return uniqueRecentLinks(Array.isArray(parsed) ? parsed.filter((item) => typeof item === 'string') : [])
  } catch {
    return []
  }
}

export function saveRecentLink(storage: LinkStorage, link: string, key = RECENT_LINKS_KEY) {
  const normalized = normalizeLink(link)
  if (!normalized) return getRecentLinks(storage, key)

  const links = uniqueRecentLinks([normalized, ...getRecentLinks(storage, key)])

  try {
    storage.setItem(key, JSON.stringify(links))
  } catch {
    return links
  }

  return links
}
