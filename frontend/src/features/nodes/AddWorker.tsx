import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import type { Node, NodeType } from './types'

// What this node's worker is allowed to do. A node starts with the kind of worker it was
// created for; here a Docker node can be given the network side (Traefik, DNS) and the other
// way round, which turns it into a "full" node. Only adding is offered: nothing a node already
// does is taken away from this panel.
const SIDES: { side: 'docker' | 'network'; has: NodeType[]; becomes: Record<string, NodeType> }[] = [
  { side: 'docker', has: ['docker', 'full'], becomes: { network: 'full' } },
  { side: 'network', has: ['network', 'full'], becomes: { docker: 'full' } },
]

export function AddWorker({ node }: { node: Node }) {
  const { t } = useTranslation(['nodes', 'errors'])
  const client = useQueryClient()
  const [asking, setAsking] = useState<'docker' | 'network' | null>(null)
  const change = useMutation({
    mutationFn: (type: NodeType) => api.patch(`/nodes/${node.id}`, { type }),
    onSuccess: async () => {
      setAsking(null)
      await client.invalidateQueries({ queryKey: ['nodes'] })
    },
  })
  return (
    <div className="panel">
      <h2>{t('nodes:workers.title')}</h2>
      <p className="hint">{t('nodes:workers.hint')}</p>
      <ul className="plain-list">
        {SIDES.map(({ side, has, becomes }) => {
          const active = has.includes(node.type)
          return (
            <li key={side}>
              <strong>{t(`nodes:workers.${side}`)}</strong>{' '}
              {active ? (
                <span className="badge ok">{t('nodes:workers.active')}</span>
              ) : asking === side ? (
                <>
                  <span>{t('nodes:workers.confirm', { name: t(`nodes:workers.${side}`) })}</span>{' '}
                  <button type="button" className="primary" disabled={change.isPending} onClick={() => change.mutate(becomes[node.type])}>
                    {t('nodes:workers.yes')}
                  </button>{' '}
                  <button type="button" onClick={() => setAsking(null)}>
                    {t('nodes:delete.cancel')}
                  </button>
                </>
              ) : (
                <button type="button" onClick={() => setAsking(side)}>
                  {t(`nodes:workers.add.${side}`)}
                </button>
              )}
            </li>
          )
        })}
      </ul>
      {change.isError && <Problem error={change.error} />}
    </div>
  )
}
