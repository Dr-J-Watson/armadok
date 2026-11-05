import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import { ConvertResult } from './ConvertResult'
import type { Result } from './ConvertResult'

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
      {result && <ConvertResult result={result} />}
    </>
  )
}
