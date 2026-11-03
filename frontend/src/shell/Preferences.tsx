import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { LANGUAGES, setLanguage } from '../core/i18n'
import type { Language } from '../core/i18n'
import { THEMES, applyTheme, currentTheme } from '../core/theme'
import type { Theme } from '../core/theme'

// Language and theme pickers, available before and after login.
export function Preferences() {
  const { t, i18n } = useTranslation()
  const [theme, setTheme] = useState<Theme>(currentTheme())

  return (
    <div className="prefs">
      <label>
        <span className="visually-hidden">{t('prefs.language')}</span>
        <select
          value={i18n.language}
          onChange={(e) => setLanguage(e.target.value as Language)}
          aria-label={t('prefs.language')}
        >
          {LANGUAGES.map((lang) => (
            <option key={lang} value={lang}>
              {t(`prefs.languages.${lang}`)}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="visually-hidden">{t('prefs.theme')}</span>
        <select
          value={theme}
          onChange={(e) => {
            setTheme(e.target.value as Theme)
            applyTheme(e.target.value as Theme)
          }}
          aria-label={t('prefs.theme')}
        >
          {THEMES.map((value) => (
            <option key={value} value={value}>
              {t(`prefs.themes.${value}`)}
            </option>
          ))}
        </select>
      </label>
    </div>
  )
}
