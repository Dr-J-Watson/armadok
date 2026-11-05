import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import type { ProjectSourceProps } from '../../core/registry'
import { GitLogins } from './GitLogins'

// Make a project from a repository's branch. Nothing is started: deploy from the project page.
export function GitSource({ nodeId, onCreated }: ProjectSourceProps) {
  const { t } = useTranslation(['git', 'errors'])
  const [url, setUrl] = useState('')
  const [branch, setBranch] = useState('main')
  const [name, setName] = useState('')
  // The name follows the address until the person types their own.
  const [named, setNamed] = useState(false)
  const clone = useMutation({
    mutationFn: () => api.post(`/nodes/${nodeId}/git/clone`, { name, url, branch }),
    onSuccess: () => onCreated(name),
  })
  const guess = (u: string) => {
    setUrl(u)
    const last = u.replace(/\/+$/, '').split('/').pop() ?? ''
    if (!named) setName(last.replace(/\.git$/, '').toLowerCase().replace(/[^a-z0-9_.-]+/g, '-').slice(0, 64))
  }
  return (
    <div>
      <p className="hint">{t('git:source.explain')}</p>
      <label>
        {t('git:source.url')}
        <input value={url} onChange={(e) => guess(e.target.value)} placeholder="https://git.example.com/org/app" />
      </label>
      <label>
        {t('git:source.branch')}
        <input value={branch} onChange={(e) => setBranch(e.target.value)} />
      </label>
      <label>
        {t('git:source.name')}
        <input
          value={name}
          onChange={(e) => {
            setNamed(true)
            setName(e.target.value)
          }}
          maxLength={64}
        />
      </label>
      <div className="actions">
        <button type="button" className="primary" disabled={clone.isPending || !url || !name || !branch} onClick={() => clone.mutate()}>
          {t('git:source.clone')}
        </button>
      </div>
      {clone.isError && <Problem error={clone.error} />}
      <GitLogins nodeId={nodeId} />
    </div>
  )
}
