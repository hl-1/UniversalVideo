import { ref } from 'vue'
import { api, jsonRequest } from '../../../shared/api/client'

export type MusicTheme = {
  mode: 'light' | 'dark'
  accent: string
  background: string
  text: string
  background_image: string | null
  fit: 'cover' | 'contain' | 'tile'
  position: 'center' | 'top' | 'bottom'
  overlay: number
  blur: number
}
export const defaultTheme: MusicTheme = { mode: 'light', accent: '#109b6d', background: '#fafbf9', text: '#252c29',
  background_image: null, fit: 'cover', position: 'center', overlay: 88, blur: 0 }
export const musicTheme = ref<MusicTheme>({ ...defaultTheme })
export const themeError = ref('')

function rgb(color: string) { return [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16)) }
function mix(first: string, second: string, ratio: number) {
  const a = rgb(first), b = rgb(second)
  return `rgb(${a.map((value, index) => Math.round(value * (1 - ratio) + b[index]! * ratio)).join(',')})`
}
function luminance(color: string) {
  return rgb(color).map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
    .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index]!, 0)
}
function readableAccent(theme: MusicTheme) {
  const background = luminance(theme.background)
  const first = rgb(theme.accent), second = rgb(theme.text)
  for (let step = 0; step <= 10; step++) {
    const color = '#' + first.map((value, index) => Math.round(value * (1 - step / 10) + second[index]! * step / 10).toString(16).padStart(2, '0')).join('')
    const foreground = luminance(color)
    if ((Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05) >= 4.5) return color
  }
  return theme.text
}
export function validThemeColors(theme: MusicTheme) {
  if (![theme.accent, theme.background, theme.text].every(color => /^#[a-f\d]{6}$/i.test(color))) return false
  const values = [luminance(theme.background), luminance(theme.text)].sort((a, b) => b - a)
  return (values[0]! + .05) / (values[1]! + .05) >= 4.5
}
export function applyMusicTheme(theme: MusicTheme) {
  if (![theme.accent, theme.background, theme.text].every(color => /^#[a-f\d]{6}$/i.test(color))) return
  const root = document.documentElement
  root.dataset.musicTheme = theme.mode
  root.toggleAttribute('data-music-background', Boolean(theme.background_image))
  const white = 1.05 / (luminance(theme.accent) + .05)
  const black = (luminance(theme.accent) + .05) / .05
  const values: Record<string, string> = {
    'accent': theme.accent, 'highlight': readableAccent(theme), 'bg': theme.background, 'text': theme.text,
    'bg-rgb': rgb(theme.background).join(','), 'surface': mix(theme.background, theme.text, .045),
    'soft': mix(theme.background, theme.text, .085), 'line': mix(theme.background, theme.text, .15),
    'muted': mix(theme.background, theme.text, .7), 'accent-soft': mix(theme.background, theme.accent, .13),
    'accent-text': white >= black ? '#ffffff' : '#111111',
    'image': theme.background_image ? `url("${theme.background_image}")` : 'none',
    'fit': theme.fit === 'tile' ? 'auto' : theme.fit, 'repeat': theme.fit === 'tile' ? 'repeat' : 'no-repeat',
    'position': theme.position, 'overlay': String(theme.overlay / 100), 'blur': `${theme.blur}px`,
  }
  for (const [key, value] of Object.entries(values)) root.style.setProperty(`--music-${key}`, value)
}
export async function loadMusicTheme() {
  applyMusicTheme(musicTheme.value)
  try {
    musicTheme.value = await api<MusicTheme>('/api/library/theme')
    applyMusicTheme(musicTheme.value)
    themeError.value = ''
  } catch (failure) { themeError.value = failure instanceof Error ? failure.message : '主题加载失败。' }
}
export async function saveMusicTheme(theme: MusicTheme) {
  musicTheme.value = await api<MusicTheme>('/api/library/theme', jsonRequest('PUT', theme))
  applyMusicTheme(musicTheme.value)
}
