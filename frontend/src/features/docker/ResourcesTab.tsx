import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Item {
  id?: string
  name?: string
  tags?: string[]
  driver?: string
  scope?: string
  size?: number
}

const KINDS = ['images', 'networks', 'volumes'] as const

function label(kind: (typeof KINDS)[number], item: Item): string {
  if (kind === 'images') return item.tags?.length ? item.tags.join(', ') : (item.id ?? '').slice(0, 19)
  return item.name ?? ''
}

function Kind({ nodeId, kind }: { nodeId: string; kind: (typeof KINDS)[number] }) {
  const { t } = useTranslation(['docker', 'errors'])
  const list = useQuery({
    queryKey: ['docker-resources', nodeId, kind],
    queryFn: () => api.get<{ items: Item[]; truncated: boolean }>(`/nodes/${nodeId}/docker/${kind}`),
  })
  return (
    <section aria-label={t(`docker:resources.${kind}`)}>
      <h3>{t(`docker:resources.${kind}`)}</h3>
      {list.isError ? (
        <p role="alert" className="error">
          {errorMessage(t, list.error)}
        </p>
      ) : list.data && list.data.items.length === 0 ? (
        <p className="empty">{t('docker:resources.none')}</p>
      ) : (
        <ul className="links">
          {list.data?.items.map((item, index) => (
            <li key={`${item.id ?? item.name}-${index}`}>
              {label(kind, item)}
              {item.driver && <small> ({item.driver})</small>}
              {item.size ? <small> {Math.round(item.size / 1048576)} MiB</small> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

// What exists on the machine besides containers: images, networks and volumes.
export function ResourcesTab({ nodeId }: { nodeId: string }) {
  return (
    <>
      {KINDS.map((kind) => (
        <Kind key={kind} nodeId={nodeId} kind={kind} />
      ))}
    </>
  )
}
