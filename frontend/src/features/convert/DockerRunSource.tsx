import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useState } from 'react'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import type { ProjectSourceProps } from '../../core/registry'
import { draftOf } from './Notes'
import type { Result } from './Notes'

// Paste a `docker run` command: it is only read, never run, and becomes the project's Compose
// text (secret-looking values go to the .env).
export function DockerRunSource({ onDraft }: ProjectSourceProps) {
  const { t } = useTranslation(['convert', 'errors'])
  const [command, setCommand] = useState('')
  const convert = useMutation({
    mutationFn: () => api.post<Result>('/convert/docker-run', { command }),
    onSuccess: (r) => onDraft(draftOf(r)),
  })
  return (
    <div>
      <label htmlFor="docker-run">
        {t('convert:input')}
        <textarea
          id="docker-run"
          className="editor"
          rows={4}
          spellCheck={false}
          placeholder="docker run -d --name blog -p 8080:80 -v blog_data:/data nginx:1.27"
          value={command}
          maxLength={8000}
          onChange={(e) => setCommand(e.target.value)}
        />
      </label>
      <div className="actions">
        <button type="button" disabled={convert.isPending || command.trim() === ''} onClick={() => convert.mutate()}>
          {t('convert:go')}
        </button>
      </div>
      {convert.isError && <Problem error={convert.error} />}
    </div>
  )
}
