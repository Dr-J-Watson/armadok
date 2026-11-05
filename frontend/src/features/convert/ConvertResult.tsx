import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

export interface Result {
  compose: string
  env: string | null
  service: string
  warnings: { code: string; detail: string }[]
}

// A copy button that says it worked. The clipboard can be refused (an insecure page): then the
// text is still on screen to select by hand.
function CopyButton({ text, label }: { text: string; label: string }) {
  const { t } = useTranslation('convert')
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true)
          setTimeout(() => setDone(false), 1800)
        })
      }}
    >
      {done ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />} {done ? t('copied') : label}
    </button>
  )
}

// The converted file, the values kept out of it, and what needs a human look.
export function ConvertResult({ result }: { result: Result }) {
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
        <section aria-label="docker-compose.yml">
          <div className="actions between">
            <h2>docker-compose.yml</h2>
            <CopyButton text={result.compose} label={t('convert:copy')} />
          </div>
          <pre>{result.compose}</pre>
        </section>
        {result.env && (
          <section aria-label=".env">
            <div className="actions between">
              <h2>.env</h2>
              <CopyButton text={result.env} label={t('convert:copy')} />
            </div>
            <p className="hint">{t('convert:envHint')}</p>
            <pre>{result.env}</pre>
          </section>
        )}
    </>
  )
}
