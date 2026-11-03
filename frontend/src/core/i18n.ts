// Translations: every user-visible text lives in `locales/<lang>/<namespace>.json`.
// Adding a language = adding a folder; adding a feature's texts = adding a namespace file.
// Keys are flat strings that may contain dots (`rights.missing`), so no key separator.

import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import { readPref, writePref } from './storage'

export const LANGUAGES = ['en', 'fr'] as const
export type Language = (typeof LANGUAGES)[number]

type Catalogue = Record<string, Record<string, Record<string, string>>>

const files = import.meta.glob<Record<string, string>>('../locales/*/*.json', {
  eager: true,
  import: 'default',
})

// "../locales/fr/common.json" -> resources.fr.common
export const resources: Catalogue = {}
for (const [path, content] of Object.entries(files)) {
  const [, lang, file] = /locales\/([^/]+)\/([^/]+)\.json$/.exec(path) ?? []
  if (lang && file) (resources[lang] ??= {})[file] = content
}

export function initialLanguage(): Language {
  const saved = readPref('lang')
  if (saved && (LANGUAGES as readonly string[]).includes(saved)) return saved as Language
  const browser = navigator.language.slice(0, 2)
  return (LANGUAGES as readonly string[]).includes(browser) ? (browser as Language) : 'en'
}

export function setLanguage(lang: Language): void {
  writePref('lang', lang)
  void i18n.changeLanguage(lang)
  document.documentElement.lang = lang
}

void i18n.use(initReactI18next).init({
  resources,
  lng: initialLanguage(),
  fallbackLng: 'en',
  ns: ['common'],
  defaultNS: 'common',
  fallbackNS: 'common',
  keySeparator: false,
  nsSeparator: ':',
  interpolation: { escapeValue: false }, // React already escapes
})
document.documentElement.lang = i18n.language

export default i18n
