import { useState } from 'react'
import { api } from '../core/api'
import { useAuth } from '../core/auth'
import { useTranslation } from 'react-i18next'
import { LANGUAGES, setLanguage } from '../core/i18n'
import type { Language } from '../core/i18n'
import { ACCENT_PRESETS, THEMES, applyAccent, applyTheme, currentAccent, currentTheme } from '../core/theme'
import type { Theme } from '../core/theme'

// Language, theme and accent colour, available before and after sign-in. The accent is chosen
// from a few swatches or with any colour; the choice stays in this browser only.
export function Preferences() {
  const { t, i18n } = useTranslation()
  const [theme, setTheme] = useState<Theme>(currentTheme())
  const [accent, setAccent] = useState<string | null>(currentAccent())
  const choose = (value: string | null) => {
    setAccent(value)
    applyAccent(value)
  }
  const { state } = useAuth()

  return (
    <div className="prefs">
      <label className="pref-row">
        <span>{t('prefs.language')}</span>
        <select
          value={i18n.language}
          onChange={(e) => {
            setLanguage(e.target.value as Language)
            // Signed in: the server also writes notifications in this language.
            if (state.status === 'authenticated')
              void api.patch('/auth/language', { language: e.target.value }).catch(() => undefined)
          }}
        >
          {LANGUAGES.map((lang) => (
            <option key={lang} value={lang}>
              {t(`prefs.languages.${lang}`)}
            </option>
          ))}
        </select>
      </label>
      <div className="pref-row" role="group" aria-label={t('prefs.theme')}>
        <span>{t('prefs.theme')}</span>
        <span className="segmented">
          {THEMES.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={theme === value}
              onClick={() => {
                setTheme(value)
                applyTheme(value)
              }}
            >
              {t(`prefs.themes.${value}`)}
            </button>
          ))}
        </span>
      </div>
      <div className="pref-row" role="group" aria-label={t('prefs.accent')}>
        <span>{t('prefs.accent')}</span>
        <span className="swatches">
          {ACCENT_PRESETS.map((color) => (
            <button
              key={color}
              type="button"
              className="swatch-button"
              style={{ background: color }}
              aria-label={color}
              aria-pressed={accent === color}
              onClick={() => choose(color)}
            />
          ))}
          <input type="color" value={accent ?? ACCENT_PRESETS[0]} onChange={(e) => choose(e.target.value)} aria-label={t('prefs.accent')} className="color-input" />
          {accent && (
            <button type="button" className="link-button" onClick={() => choose(null)} title={t('prefs.accentReset')}>
              {t('prefs.accentReset')}
            </button>
          )}
        </span>
      </div>
    </div>
  )
}
