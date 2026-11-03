import { useQuery } from '@tanstack/react-query'
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
          </tr>
        </thead>
        <tbody>
          {list.map((c) => (
            <tr key={c.id}>
              <td data-label={t('docker:columns.name')}>{c.names[0] ?? c.id.slice(0, 12)}</td>
              <td data-label={t('docker:columns.image')}>{c.image}</td>
              <td data-label={t('docker:columns.state')}>
                <span className={`badge ${c.state === 'running' ? 'ok' : 'off'}`}>{c.state}</span>{' '}
                <small>{c.status}</small>
              </td>
              <td data-label={t('docker:columns.ports')}>{publishedPorts(c.ports)}</td>
              <td data-label={t('docker:columns.networks')}>{c.networks.join(', ')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
