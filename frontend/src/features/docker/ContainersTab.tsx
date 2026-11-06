import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'
import { Tip } from '../../components/Tip'
import { useConfirm } from '../../components/Confirm'

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

interface Stat {
  id: string
  cpu_percent: number
  memory_used: number
  memory_limit: number
}

const mebi = (n: number) => `${Math.round(n / 1048576)} MiB`

function publishedPorts(ports: Port[]): string {
  return ports
    .filter((p) => p.public)
    .map((p) => `${p.public}→${p.private}/${p.type}`)
    .join(', ')
}

export function ContainersTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['docker', 'errors'])
  const queryClient = useQueryClient()
  const ask = useConfirm()
  const [filter, setFilter] = useState('')
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
  // Measuring takes a few seconds on the machine: only while the box is ticked.
  const [live, setLive] = useState(false)
  const stats = useQuery({
    queryKey: ['container-stats', nodeId],
    enabled: live,
    queryFn: () => api.get<{ stats: Stat[]; truncated: boolean }>(`/nodes/${nodeId}/docker/stats`),
    refetchInterval: 6000,
  })
  const usage = new Map((stats.data?.stats ?? []).map((x) => [x.id, x]))
  const containers = useQuery({
    queryKey: ['containers', nodeId],
    queryFn: () =>
      api.get<{ containers: Container[]; truncated: boolean }>(`/nodes/${nodeId}/docker/containers`),
    refetchInterval: 10000,
  })

  if (containers.isPending) return <p className="loading">{t('loading')}</p>
  if (containers.isError) {
    return (
      <Problem error={containers.error} />
    )
  }
  const { containers: all, truncated } = containers.data
  const needle = filter.trim().toLowerCase()
  const list = needle ? all.filter((c) => `${c.names.join(' ')} ${c.image}`.toLowerCase().includes(needle)) : all
  const running = all.filter((c) => c.state === 'running').length
  if (all.length === 0) return <p className="empty">{t('docker:empty')}</p>

  return (
    <>
      {truncated && <p className="notice">{t('docker:truncated')}</p>}
      <div className="actions between">
        <p className="hint">{t('docker:summary', { running, total: all.length })}</p>
        <div className="actions">
          <input
            type="search"
            aria-label={t('docker:search')}
            placeholder={t('docker:search')}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <label>
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} /> {t('docker:live.toggle')}
            <Tip text={t('docker:live.tip')} />
          </label>
        </div>
      </div>
      {list.length === 0 && <p className="empty">{t('docker:noMatch')}</p>}
      {live && stats.isError && (
        <Problem error={stats.error} />
      )}
      {live && stats.data?.truncated && <p className="notice">{t('docker:live.truncated')}</p>}
      <table className="table" hidden={list.length === 0}>
        <thead>
          <tr>
            <th>{t('docker:columns.name')}</th>
            <th>{t('docker:columns.image')}</th>
            <th>{t('docker:columns.state')}</th>
            {live && <th>{t('docker:columns.usage')}</th>}
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
              {live && (
                <td data-label={t('docker:columns.usage')}>
                  {usage.has(c.id)
                    ? `${usage.get(c.id)!.cpu_percent}% · ${mebi(usage.get(c.id)!.memory_used)}${usage.get(c.id)!.memory_limit ? ` / ${mebi(usage.get(c.id)!.memory_limit)}` : ''}`
                    : c.state === 'running' && stats.isPending
                      ? '…'
                      : ''}
                </td>
              )}
              <td data-label={t('docker:columns.ports')}>{publishedPorts(c.ports)}</td>
              <td data-label={t('docker:columns.networks')}>{c.networks.join(', ')}</td>
              <td data-label={t('docker:columns.actions')}>
                <div className="actions">
                  {(c.state === 'running' ? ['stop', 'restart'] : ['start']).map((action) => (
                    <button
                      key={action}
                      type="button"
                      disabled={act.isPending}
                      title={t(`docker:actionTip.${action}`)}
                      onClick={async () => {
                        if (action !== 'stop' || (await ask(t('docker:stopConfirm', { name: c.names[0] ?? c.id.slice(0, 12) }), { danger: false }))) act.mutate({ id: c.id, action })
                      }}
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
                      <button type="button" title={t('docker:actionTip.remove')} onClick={() => setConfirming(c.id)}>
                        {t('docker:action.remove')}
                      </button>
                    ))}
                  <button type="button" aria-pressed={logsOf === c.id} title={t('docker:actionTip.logs')} onClick={() => setLogsOf(logsOf === c.id ? null : c.id)}>
                    {t('docker:action.logs')}
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {act.isError && (
        <Problem error={act.error} />
      )}
      {logsOf && (
        <section aria-label={t('docker:logsTitle')}>
          <h3>{t('docker:logsTitle')}</h3>
          {logs.isError ? (
            <Problem error={logs.error} />
          ) : (
            <pre className="output">{logs.data?.output ?? t('loading')}</pre>
          )}
        </section>
      )}
    </>
  )
}
