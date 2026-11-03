// Themes are sets of CSS variables (see styles.css). No brand colour is imposed:
// light, dark or follow the system. A custom palette will plug into the same variables.

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
