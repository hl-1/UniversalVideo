import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import ts from 'typescript'
import * as vue from 'vue'
import * as lists from '../src/features/music/musicList.ts'
const { defaultMusicListFilters, filterMusicList, musicArtistOptions, formatMusicListTime } = lists
const songs = [
  { title: '晴天', artist: '周杰伦', list_time: 100 },
  { title: '晴天 Live', artist: '周杰伦', list_time: 300 },
  { title: '小酒窝', artist: '林俊杰 / 蔡卓妍', list_time: 100 },
  { title: 'Thunderstruck', artist: 'AC/DC', list_time: null },
  { title: 'No Artist', artist: '', list_time: NaN },
]

test('fuzzy and exact search preserve song versions and match normalized full fields', () => {
  const filters = { ...defaultMusicListFilters(), query: ' 晴天 ' }
  assert.deepEqual(filterMusicList(songs, filters), songs.slice(0, 2))
  assert.deepEqual(filterMusicList(songs, { ...filters, match: 'exact' }), songs.slice(0, 1))
  assert.equal(filterMusicList(songs, { ...filters, field: 'artist' }).length, 0)
  assert.equal(filterMusicList(songs, { ...filters, query: 'ＴＨＵＮＤＥＲＳＴＲＵＣＫ', match: 'exact' })[0], songs[3])
})

test('artist dropdown and text search combine and allow an individual collaboration artist', () => {
  assert.deepEqual(filterMusicList(songs, { ...defaultMusicListFilters(), artist: '周杰伦', query: 'Live', field: 'title' }), [songs[1]])
  assert.deepEqual(filterMusicList(songs, { ...defaultMusicListFilters(), artist: '蔡卓妍' }), [songs[2]])
  assert.deepEqual(filterMusicList(songs, { ...defaultMusicListFilters(), query: '蔡卓妍', field: 'artist', match: 'exact' }), [songs[2]])
  assert.deepEqual(filterMusicList(songs, { ...defaultMusicListFilters(), artist: '未知歌手' }), [songs[4]])
})

test('artist options deduplicate normalized names without splitting AC/DC', () => {
  const artists = musicArtistOptions([...songs, { title: 'Other', artist: ' ac/dc ' }])
  assert.ok(artists.includes('AC/DC'))
  assert.ok(artists.includes('蔡卓妍'))
  assert.ok(artists.includes('未知歌手'))
  assert.equal(artists.filter(name => name.toLowerCase() === 'ac/dc').length, 1)
  assert.ok(!artists.includes('AC'))
})

test('both time directions keep missing dates last and equal dates stable', () => {
  assert.deepEqual(filterMusicList(songs, { ...defaultMusicListFilters(), sort: 'newest' }), [songs[1], songs[0], songs[2], songs[3], songs[4]])
  assert.deepEqual(filterMusicList(songs, { ...defaultMusicListFilters(), sort: 'oldest' }), [songs[0], songs[2], songs[1], songs[3], songs[4]])
})

test('filtering and sorting preserve input order, object identity, and default order', () => {
  const original = [...songs]
  const sorted = filterMusicList(Object.freeze([...songs]), { ...defaultMusicListFilters(), sort: 'newest' })
  assert.equal(sorted[0], songs[1])
  assert.deepEqual(songs, original)
  assert.deepEqual(filterMusicList(songs, defaultMusicListFilters()), original)
  assert.deepEqual(filterMusicList([], defaultMusicListFilters()), [])
})

test('custom adapters support editable import rows and filter before pagination', () => {
  const rows = Array.from({ length: 120 }, (_, i) => ({ title: `row ${i}`, artist: '', chosen: { name: `歌曲 ${i}`, singer: i < 60 ? '甲' : '乙', added: 120 - i } }))
  const adapter = { title: (row: typeof rows[number]) => row.chosen.name, artist: (row: typeof rows[number]) => row.chosen.singer, time: (row: typeof rows[number]) => row.chosen.added }
  const result = filterMusicList(rows, { ...defaultMusicListFilters(), artist: '乙', sort: 'oldest' }, adapter)
  assert.equal(result.length, 60)
  assert.equal(result.slice(0, 50)[0], rows[119])
  assert.equal(result.slice(50)[9], rows[60])
})

test('missing and invalid timestamps never become refresh or release dates', () => {
  for (const time of [null, undefined, 0, -1, Infinity, NaN, 9e15]) assert.equal(formatMusicListTime(time), '—')
  assert.ok(formatMusicListTime(1729771200).includes('2024'))
})

test('list preferences survive remounts, stay isolated by scope, and clear on account change', () => {
  const effects = vue.effectScope()
  effects.run(() => {
    const account = vue.ref({ id: 1 })
    const source = fs.readFileSync(new URL('../src/features/music/useMusicList.ts', import.meta.url), 'utf8')
    const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    const exports: any = {}
    new Function('require', 'exports', compiled)((id: string) => {
      if (id === 'vue') return vue
      if (id === '../accounts/auth') return { account }
      if (id === './musicList') return lists
      assert.fail(`Unexpected module: ${id}`)
    }, exports)
    const scope = vue.ref('favorites'), tracks = vue.ref([...songs])
    const list = exports.useMusicList(() => tracks.value, () => scope.value)
    list.filters.value = { ...defaultMusicListFilters(), artist: '周杰伦', sort: 'newest' }
    assert.deepEqual(list.filtered.value.map((song: any) => song.title), ['晴天 Live', '晴天'])
    scope.value = 'local'
    assert.equal(list.filters.value.artist, '')
    scope.value = 'favorites'
    assert.equal(list.filters.value.artist, '周杰伦')
    assert.equal(exports.useMusicList(() => tracks.value, () => 'favorites').filters.value.artist, '周杰伦')
    tracks.value = []
    assert.equal(list.hasTime.value, false)
    account.value = { id: 2 }
    assert.equal(list.filters.value.artist, '')
  })
  effects.stop()
})
