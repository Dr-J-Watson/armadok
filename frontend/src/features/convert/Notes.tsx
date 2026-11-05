import { useTranslation } from 'react-i18next'
import type { ProjectDraft } from '../../core/registry'

export interface Result {
  compose: string
  env: string | null
  service: string
  warnings: { code: string; detail: string }[]
}

// What a conversion wants the person to check, and where the secrets went.
export function Notes({ result }: { result: Result }) {
  const { t } = useTranslation('convert')
  return (
    <>
      {result.warnings.length > 0 && (
        <div className="notice" role="note" aria-label={t('convert:warnings')}>
          <h3>{t('convert:warnings')}</h3>
          <ul className="plain">
            {result.warnings.map((w, i) => (
              <li key={i}>{t(`convert:warning.${w.code}`, { detail: w.detail, defaultValue: w.code })}</li>
            ))}
          </ul>
        </div>
      )}
      {result.env && <p className="hint">{t('convert:envHint')}</p>}
    </>
  )
}

// A conversion becomes the starting point of a new project.
export function draftOf(result: Result): ProjectDraft {
  return { name: result.service, compose: result.compose, env: result.env, notes: <Notes result={result} /> }
}
