import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import { ConvertResult } from './ConvertResult'
import type { Result } from './ConvertResult'

interface Container {
  id: string
  names: string[]
  image: string
  state: string
}

// Tick containers that already run on a machine and get the Compose file that recreates them.
// Their settings (and environment variables) are read, never changed; nothing is saved.
export function FromContainers({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['convert', 'errors'])
  const [picked, setPicked] = useState<string[]>([])
  const list = useQuery({
    queryKey: ['containers', nodeId],
    queryFn: () => api.get<{ containers: Container[] }>(`/nodes/${nodeId}/docker/containers`),
  })
  const convert = useMutation({
    mutationFn: () => api.post<Result>(`/nodes/${nodeId}/convert/containers`, { ids: picked }),
  })
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]))
  if (list.isPending) return <p>{t('loading')}</p>
  if (list.isError) return <Problem error={list.error} />
  return (
    <>
      <p className="hint">{t('convert:fromContainers.explain')}</p>
      <fieldset>
        <legend>{t('convert:fromContainers.pick')}</legend>
        {list.data.containers.map((c) => (
          <label key={c.id} className="legend-item">
            <input
              type="checkbox"
              checked={picked.includes(c.id)}
              disabled={!picked.includes(c.id) && picked.length >= 20}
              onChange={() => toggle(c.id)}
            />{' '}
            {c.names[0] ?? c.id.slice(0, 12)} <small>{c.image}</small>
          </label>
        ))}
      </fieldset>
      <div className="actions">
        <button type="button" className="primary" disabled={picked.length === 0 || convert.isPending} onClick={() => convert.mutate()}>
          {t('convert:fromContainers.go', { count: picked.length })}
        </button>
      </div>
      {convert.isError && <Problem error={convert.error} />}
      {convert.data && <ConvertResult result={convert.data} />}
    </>
  )
}
