import { computed, nextTick, onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import { api, jsonRequest } from '../../shared/api/client'
import { changeNotice, feedNotice, type DailyFeed } from './dailyFeed'
import { expose } from './discoveryEvents'
import type { MusicTrack } from './musicLibrary'
export function useDailyFeed() {
  const feed = ref<DailyFeed | null>(null), busy = ref(false), feedbackBusy = ref(false), loading = ref(true), error = ref(''), operationNotice = ref('')
  const cooldown = ref(0)
  let disposed = false, request = 0, timer: ReturnType<typeof setInterval> | undefined
  const notice = computed(() => operationNotice.value || feedNotice(feed.value))
  const canChange = computed(() => !busy.value && !feedbackBusy.value && cooldown.value <= Date.now())
  function accept(value: DailyFeed) {
    if (value.feed_id !== feed.value?.feed_id) operationNotice.value = ''
    feed.value = value; cooldown.value = Date.now() + value.retry_after_seconds * 1000
  }
  async function load(prepare = false) {
    if (busy.value || feedbackBusy.value || disposed) return
    const revision = ++request
    try {
      const value = await api<DailyFeed>(`/api/music/discovery/${prepare ? 'prepare' : 'feed'}`, prepare ? jsonRequest('POST', {}) : undefined)
      if (!disposed && revision === request) { accept(value); error.value = '' }
    } catch (failure) { if (!disposed && revision === request) error.value = failure instanceof Error ? failure.message : '暂时无法获取推荐。' }
    finally { loading.value = false }
  }
  async function change(payload: Record<string, unknown> = {}) {
    if (!canChange.value) return
    busy.value = true; error.value = ''; operationNotice.value = ''; ++request
    const previous = feed.value
    try {
      const value = await api<DailyFeed>('/api/music/discovery/feed/next', {
        ...jsonRequest('POST', payload), headers: { ...(jsonRequest('POST', {}).headers as Record<string, string>), 'Idempotency-Key': crypto.randomUUID() } })
      if (!disposed) { accept(value); operationNotice.value = changeNotice(previous, value) }
    } catch (failure) { error.value = failure instanceof Error ? failure.message : '换歌失败，请稍后重试。' }
    finally { busy.value = false }
  }
  async function reduceRecommendation(track: MusicTrack, reason: string, scope = 'recording') {
    if (busy.value || feedbackBusy.value || disposed) return null
    feedbackBusy.value = true; ++request; error.value = ''; operationNotice.value = ''
    try {
      const result = await api<{ notice: string; feed: DailyFeed }>('/api/music/discovery/feedback', jsonRequest('PUT', { track_key: track.key, reason, scope }))
      if (!disposed) accept(result.feed)
      return result
    } finally { feedbackBusy.value = false }
  }
  function refresh() { void load() }
  onMounted(() => {
    void load(true)
    let ticks = 0
    timer = setInterval(() => { if (cooldown.value && cooldown.value <= Date.now()) cooldown.value = 0; if (++ticks % 5 === 0) void load() }, 1000)
    window.addEventListener('videodream-discovery-ready', refresh)
  })
  onUnmounted(() => { disposed = true; ++request; clearInterval(timer); window.removeEventListener('videodream-discovery-ready', refresh) })
  return { feed, busy, feedbackBusy, loading, error, notice, canChange, load, change, reduceRecommendation }
}

export function useFeedExposure(root: Ref<HTMLElement | null>, tracks: () => MusicTrack[], feedId: () => string | null | undefined) {
  let observer: IntersectionObserver | undefined, disposed = false
  const seen = new Set<string>(), pending = new Map<Element, ReturnType<typeof setTimeout>>()
  watch(() => `${feedId()}:${tracks().map(t => t.key).join(',')}`, async () => {
    await nextTick()
    if (disposed) return
    observer?.disconnect(); for (const timer of pending.values()) clearTimeout(timer); pending.clear()
    observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        const key = (entry.target as HTMLElement).dataset.key!, id = `${feedId()}:${key}`
        if (entry.isIntersecting && entry.intersectionRatio >= .5 && !seen.has(id)) {
          if (pending.has(entry.target)) continue
          pending.set(entry.target, setTimeout(() => {
            const track = tracks().find(t => t.key === key)
            if (track && document.visibilityState === 'visible') { expose(track); seen.add(id) }
            pending.delete(entry.target)
          }, 1000))
        } else { clearTimeout(pending.get(entry.target)); pending.delete(entry.target) }
      }
    }, { threshold: [.5] })
    root.value?.querySelectorAll('[data-key]').forEach(el => observer!.observe(el))
  }, { flush: 'post' })
  onUnmounted(() => { disposed = true; observer?.disconnect(); for (const timer of pending.values()) clearTimeout(timer) })
}
