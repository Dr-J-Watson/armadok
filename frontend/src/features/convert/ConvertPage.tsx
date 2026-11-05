import { useMutation } from '@tanstack/react-query'
import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'

interface Result {
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

// Paste a `docker run` command, get the Compose file that does the same. Nothing is run and
// nothing is saved: the command is only read.
export function ConvertPage() {
  const { t } = useTranslation(['convert', 'errors'])
  const [command, setCommand] = useState('')
  const convert = useMutation({ mutationFn: () => api.post<Result>('/convert/docker-run', { command }) })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    convert.mutate()
  }
  const result = convert.data
  return (
    <>
      <header className="page-head">
        <div>
          <h1>{t('convert:title')}</h1>
          <p className="hint">{t('convert:subtitle')}</p>
        </div>
      </header>
      <form onSubmit={submit} className="panel">
        <label htmlFor="docker-run">
          {t('convert:input')}
          <textarea
            id="docker-run"
            className="editor"
            rows={5}
            spellCheck={false}
            placeholder="docker run -d --name blog -p 8080:80 -v blog_data:/data nginx:1.27"
            value={command}
            maxLength={8000}
            onChange={(e) => setCommand(e.target.value)}
          />
        </label>
        <div className="actions">
          <button type="submit" className="primary" disabled={convert.isPending || command.trim() === ''}>
            {t('convert:go')}
          </button>
        </div>
      </form>
      {convert.isError && <Problem error={convert.error} />}
      {result && (
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
      )}
    </>
  )
}
