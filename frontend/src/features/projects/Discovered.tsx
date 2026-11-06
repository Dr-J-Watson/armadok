// Folders of the node that hold a Compose file but are not imported: Armadok lists them, reads
// nothing more, and changes nothing until someone imports them. Importing a project does not
// touch its folder or its containers; neither does stopping to manage it.

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { Tip } from '../../components/Tip'
import { api } from '../../core/api'

export interface Found {
  name: string
  files: string[]
  has_env: boolean
  has_git: boolean
  services: string[]
  error: 'invalid_yaml' | 'too_large' | null
  state: 'discovered' | 'ignored'
}

export function Discovered({ nodeId, found, ignored }: { nodeId: string; found: Found[]; ignored: boolean }) {
  const { t } = useTranslation(['projects', 'errors'])
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/projects`
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [labels, setLabels] = useState('')
  const rows = found.filter((f) => (f.state === 'ignored') === ignored)
  const refresh = async () => {
    setPicked(new Set())
    await client.invalidateQueries({ queryKey: ['projects', nodeId] })
  }
  const importThem = useMutation({
    mutationFn: () => api.post(`${base}/import`, { projects: [...picked], labels: labels.split(',').map((l) => l.trim()).filter(Boolean) }),
    onSuccess: refresh,
  })
  const ignoreThem = useMutation({ mutationFn: () => api.post(`${base}/ignore`, { projects: [...picked] }), onSuccess: refresh })
  const restore = useMutation({ mutationFn: (name: string) => api.post(`${base}/${encodeURIComponent(name)}/unmanage`), onSuccess: refresh })
  const failed = importThem.error ?? ignoreThem.error ?? restore.error

  const toggle = (name: string) => {
    const next = new Set(picked)
    if (next.has(name)) next.delete(name)
    else next.add(name)
    setPicked(next)
  }

  if (rows.length === 0) return <p className="empty">{t(ignored ? 'projects:ignored.none' : 'projects:discovered.none')}</p>
  return (
    <>
      <p className="hint">{t(ignored ? 'projects:ignored.explain' : 'projects:discovered.explain')}</p>
      {failed != null && <Problem error={failed} />}
      <table className="table">
        <thead>
          <tr>
            {!ignored && (
              <th>
                <input
                  type="checkbox"
                  aria-label={t('projects:discovered.all')}
                  checked={picked.size === rows.length}
                  onChange={(e) => setPicked(e.target.checked ? new Set(rows.map((r) => r.name)) : new Set())}
                />
              </th>
            )}
            <th>{t('projects:discovered.name')}</th>
            <th>{t('projects:discovered.services')}</th>
            <th>{t('projects:discovered.contains')}</th>
            {ignored && <th />}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.name}>
              {!ignored && (
                <td>
                  <input type="checkbox" aria-label={r.name} checked={picked.has(r.name)} onChange={() => toggle(r.name)} />
                </td>
              )}
              <td data-label={t('projects:discovered.name')}>
                <strong>{r.name}</strong>
                <div className="hint">{r.files.join(', ')}</div>
              </td>
              <td data-label={t('projects:discovered.services')}>
                {r.error ? <span className="badge danger-badge">{t(`projects:discovered.error.${r.error}`)}</span> : r.services.join(', ') || '—'}
              </td>
              <td data-label={t('projects:discovered.contains')}>
                {r.has_env && <span className="badge">.env</span>}
                {r.has_git && <span className="badge">.git</span>}
              </td>
              {ignored && (
                <td>
                  <button type="button" disabled={restore.isPending} onClick={() => restore.mutate(r.name)}>
                    {t('projects:ignored.restore')}
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {!ignored && (
        <div className="panel">
          <label htmlFor="import-labels">
            {t('projects:discovered.labels')}
            <Tip text={t('projects:discovered.labelsTip')} />
          </label>
          <input id="import-labels" value={labels} placeholder={t('projects:discovered.labelsPlaceholder')} onChange={(e) => setLabels(e.target.value)} />
          <div className="actions">
            <button type="button" className="primary" disabled={picked.size === 0 || importThem.isPending} onClick={() => importThem.mutate()}>
              {t('projects:discovered.import', { count: picked.size })}
            </button>
            <button type="button" disabled={picked.size === 0 || ignoreThem.isPending} title={t('projects:discovered.ignoreTip')} onClick={() => ignoreThem.mutate()}>
              {t('projects:discovered.ignore')}
            </button>
          </div>
          <p className="hint">{t('projects:discovered.safe')}</p>
        </div>
      )}
    </>
  )
}
