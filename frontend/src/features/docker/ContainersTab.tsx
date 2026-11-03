import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Port {
  private: number | null
  public: number | null
  type: string
}

interface Container {
  id: string
  names: string[]
  image: string
  state: string
  status: string
  networks: string[]
  ports: Port[]
}

function publishedPorts(ports: Port[]): string {
  return ports
    .filter((p) => p.public)
    .map((p) => `${p.public}→${p.private}/${p.type}`)
    .join(', ')
}

export function ContainersTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['docker', 'errors'])
  const queryClient = useQueryClient()
  const [logsOf, setLogsOf] = useState<string | null>(null)
  // Removing asks again first: it cannot be undone.
  const [confirming, setConfirming] = useState<string | null>(null)
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) =>
      api.post(`/nodes/${nodeId}/docker/containers/${encodeURIComponent(id)}/action`, { action }),
    onSettled: () => {
      setConfirming(null)
      return queryClient.invalidateQueries({ queryKey: ['containers', nodeId] })
    },
  })
  const logs = useQuery({
    queryKey: ['container-logs', nodeId, logsOf],
    enabled: logsOf !== null,
    queryFn: () => api.get<{ output: string }>(`/nodes/${nodeId}/docker/containers/${encodeURIComponent(logsOf ?? '')}/logs?tail=200`),
  })
  const containers = useQuery({
    queryKey: ['containers', nodeId],
    queryFn: () =>
      api.get<{ containers: Container[]; truncated: boolean }>(`/nodes/${nodeId}/docker/containers`),
    refetchInterval: 10000,
  })

  if (containers.isPending) return <p>{t('loading')}</p>
  if (containers.isError) {
    return (
      <p role="alert" className="error">
        {errorMessage(t, containers.error)}
      </p>
    )
  }
  const { containers: list, truncated } = containers.data
  if (list.length === 0) return <p className="empty">{t('docker:empty')}</p>

  return (
    <>
      {truncated && <p className="notice">{t('docker:truncated')}</p>}
      <table className="table">
        <thead>
          <tr>
            <th>{t('docker:columns.name')}</th>
            <th>{t('docker:columns.image')}</th>
            <th>{t('docker:columns.state')}</th>
            <th>{t('docker:columns.ports')}</th>
            <th>{t('docker:columns.networks')}</th>
            <th>{t('docker:columns.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {list.map((c) => (
            <tr key={c.id}>
              <td data-label={t('docker:columns.name')}>
                {c.names[0] ?? c.id.slice(0, 12)}{' '}
                <Link to={`/nodes/${nodeId}?tab=map&focus=${encodeURIComponent(`container:${c.id}`)}`}>
                  {t('docker:relations')}
                </Link>
              </td>
              <td data-label={t('docker:columns.image')}>{c.image}</td>
              <td data-label={t('docker:columns.state')}>
                <span className={`badge ${c.state === 'running' ? 'ok' : 'off'}`}>{c.state}</span>{' '}
                <small>{c.status}</small>
              </td>
              <td data-label={t('docker:columns.ports')}>{publishedPorts(c.ports)}</td>
              <td data-label={t('docker:columns.networks')}>{c.networks.join(', ')}</td>
              <td data-label={t('docker:columns.actions')}>
                <div className="actions">
                  {(c.state === 'running' ? ['stop', 'restart'] : ['start']).map((action) => (
                    <button
                      key={action}
                      type="button"
                      disabled={act.isPending}
                      onClick={() => act.mutate({ id: c.id, action })}
                    >
                      {t(`docker:action.${action}`)}
                    </button>
                  ))}
                  {c.state !== 'running' &&
                    (confirming === c.id ? (
                      <button type="button" className="danger" onClick={() => act.mutate({ id: c.id, action: 'remove' })}>
                        {t('docker:action.confirmRemove')}
                      </button>
                    ) : (
                      <button type="button" onClick={() => setConfirming(c.id)}>
                        {t('docker:action.remove')}
                      </button>
                    ))}
                  <button type="button" onClick={() => setLogsOf(logsOf === c.id ? null : c.id)}>
                    {t('docker:action.logs')}
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {act.isError && (
        <p role="alert" className="error">
          {errorMessage(t, act.error)}
        </p>
      )}
      {logsOf && (
        <section aria-label={t('docker:logsTitle')}>
          <h3>{t('docker:logsTitle')}</h3>
          {logs.isError ? (
            <p role="alert" className="error">
              {errorMessage(t, logs.error)}
            </p>
          ) : (
            <pre className="output">{logs.data?.output ?? t('loading')}</pre>
          )}
        </section>
      )}
    </>
  )
}
