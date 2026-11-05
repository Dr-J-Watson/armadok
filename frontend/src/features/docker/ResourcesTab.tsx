import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

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
        <Problem error={list.error} />
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

const PRUNE_KINDS = ['containers', 'images', 'networks'] as const

// Manual clean-up: one kind at a time, after an explicit confirmation. The machine keeps
// anything labelled `armadok.protect=true`, never touches volumes, and keeps the network of a
// stopped container.
function Cleanup({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['docker', 'errors'])
  const client = useQueryClient()
  const [asked, setAsked] = useState<(typeof PRUNE_KINDS)[number] | null>(null)
  const prune = useMutation({
    mutationFn: (kind: string) => api.post<{ removed: number; space: number }>(`/nodes/${nodeId}/docker/prune`, { kind }),
    onSuccess: () => {
      setAsked(null)
      return client.invalidateQueries({ queryKey: ['docker-resources', nodeId] })
    },
  })
  return (
    <section aria-label={t('docker:prune.title')}>
      <h3>{t('docker:prune.title')}</h3>
      <p className="hint">{t('docker:prune.hint')}</p>
      <ul className="links">
        {PRUNE_KINDS.map((kind) => (
          <li key={kind}>
            <button type="button" disabled={prune.isPending} onClick={() => setAsked(kind)}>
              {t(`docker:prune.${kind}`)}
            </button>
          </li>
        ))}
      </ul>
      {asked && (
        <div className="notice" role="alertdialog" aria-label={t('docker:prune.confirmTitle')}>
          <p>{t(`docker:prune.confirm.${asked}`)}</p>
          <button type="button" disabled={prune.isPending} onClick={() => prune.mutate(asked)}>
            {t('docker:prune.go')}
          </button>{' '}
          <button type="button" onClick={() => setAsked(null)}>
            {t('docker:prune.cancel')}
          </button>
        </div>
      )}
      {prune.isSuccess && (
        <p role="status">{t('docker:prune.done', { count: prune.data.removed, mib: Math.round(prune.data.space / 1048576) })}</p>
      )}
      {prune.isError && (
        <Problem error={prune.error} />
      )}
    </section>
  )
}

// What exists on the machine besides containers: images, networks and volumes.
export function ResourcesTab({ nodeId }: { nodeId: string }) {
  return (
    <>
      <Cleanup nodeId={nodeId} />
      {KINDS.map((kind) => (
        <Kind key={kind} nodeId={nodeId} kind={kind} />
      ))}
    </>
  )
}
