import { computed, reactive, watch } from 'vue'
import { account } from '../accounts/auth'
import { defaultMusicListFilters, filterMusicList, musicArtistOptions, musicListAccessors, musicListTimestamp, type MusicListAdapter, type MusicListFilters, type MusicListSong } from './musicList'

// Session-only preferences. Account changes discard all previous list state.
const states = reactive(new Map<string, MusicListFilters>())
watch(() => account.value?.id, () => states.clear(), { flush: 'sync' })

export function useMusicList<T extends MusicListSong>(items: () => readonly T[], scope: () => string, adapter: MusicListAdapter<T> = {}) {
  const filters = computed({
    get() {
      const key = scope()
      if (!states.has(key)) states.set(key, defaultMusicListFilters())
      return states.get(key)!
    },
    set(value: MusicListFilters) { states.set(scope(), { ...value }) },
  })
  const artists = computed(() => musicArtistOptions(items(), adapter))
  const filtered = computed(() => filterMusicList(items(), filters.value, adapter))
  const hasTime = computed(() => items().some(item => musicListTimestamp(musicListAccessors(adapter).time(item)) !== null))
  const active = computed(() => Boolean(filters.value.artist || filters.value.query.trim() || filters.value.sort !== 'default'))
  function cycleSort() {
    filters.value = { ...filters.value, sort: filters.value.sort === 'default' ? 'newest' : filters.value.sort === 'newest' ? 'oldest' : 'default' }
  }
  return { filters, artists, filtered, hasTime, active, cycleSort }
}
