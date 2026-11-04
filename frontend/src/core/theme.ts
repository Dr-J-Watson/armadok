// Themes are sets of CSS variables (see styles.css). No brand colour is imposed:
// light, dark or follow the system, plus an accent colour of the person's choice.

import { readPref, writePref } from './storage'

export const THEMES = ['system', 'light', 'dark'] as const
export type Theme = (typeof THEMES)[number]

export function currentTheme(): Theme {
  const saved = readPref('theme')
  return (THEMES as readonly string[]).includes(saved ?? '') ? (saved as Theme) : 'system'
}

export function applyTheme(theme: Theme): void {
  writePref('theme', theme)
  if (theme === 'system') delete document.documentElement.dataset.theme
  else document.documentElement.dataset.theme = theme
}

// The accent: the one colour a person may choose. Everything else follows the light or dark set.
export const ACCENT_PRESETS = ['#2f6fdd', '#1a7f6b', '#7b4bd6', '#c5560c', '#c2306a', '#4b5563'] as const
const HEX = /^#[0-9a-f]{6}$/

// Black or white text, whichever reads better on this colour (WCAG relative luminance).
export function textOn(hex: string): string {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const channel = parseInt(hex.slice(i, i + 2), 16) / 255
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? '#0b1220' : '#ffffff'
}

export function currentAccent(): string | null {
  const saved = readPref('accent')
  return saved && HEX.test(saved) ? saved : null
}

// `null` goes back to the theme's own accent. Anything that is not a plain #rrggbb is ignored.
export function applyAccent(hex: string | null): void {
  const root = document.documentElement.style
  if (hex === null || !HEX.test(hex)) {
    root.removeProperty('--accent')
    root.removeProperty('--accent-text')
    writePref('accent', '')
    return
  }
  root.setProperty('--accent', hex)
  root.setProperty('--accent-text', textOn(hex))
  writePref('accent', hex)
}
