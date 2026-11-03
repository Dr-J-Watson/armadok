import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Notice {
  id: string
  admin: string
  reason: string
  started_at: string
  expires_at: string
  ended_at: string | null
  open: boolean
  ended_by_owner?: boolean
  total_actions?: number
  truncated?: boolean
  acknowledged: boolean
  actions: { at: string; event: string; outcome: string }[]
}

// What the owner of a machine is told when an administrator came in through an emergency
// session: who, why, when, and what they did. A notice can be acknowledged, never erased.
export function EmergencyTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['emergency', 'errors'])
  const queryClient = useQueryClient()
  const notices = useQuery({
    queryKey: ['emergency', nodeId],
    queryFn: () => api.get<Notice[]>(`/nodes/${nodeId}/emergency`),
    retry: false,
  })
  const cut = useMutation({
    mutationFn: (id: string) => api.del(`/nodes/${nodeId}/emergency/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['emergency', nodeId] }),
  })
  const ack = useMutation({
    mutationFn: (id: string) => api.post(`/nodes/${nodeId}/emergency/${id}/acknowledge`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['emergency', nodeId] }),
  })
  if (notices.isPending) return <p>{t('loading')}</p>
  if (notices.isError) return <p className="hint">{t('emergency:ownerOnly')}</p>
  if (notices.data.length === 0) return <p className="empty">{t('emergency:none')}</p>
  return (
    <>
      <p>{t('emergency:explain')}</p>
      {(ack.isError || cut.isError) && (
        <p role="alert" className="error">
          {errorMessage(t, ack.error ?? cut.error)}
        </p>
      )}
      <ul className="links">
        {notices.data.map((n) => (
          <li key={n.id}>
            <strong>{n.admin}</strong>{' '}
            <span className={`badge ${n.open ? 'off' : 'ok'}`}>{t(n.open ? 'emergency:open_now' : 'emergency:over')}</span>
            <br />
            <small>
              {new Date(n.started_at).toLocaleString()} — {n.reason}
            </small>
            {n.ended_by_owner && <small> — {t('emergency:cutByOwner')}</small>}
            {n.open && (
              <button type="button" onClick={() => cut.mutate(n.id)}>
                {t('emergency:cut')}
              </button>
            )}
            <details>
              <summary>{t('emergency:actions', { count: n.total_actions ?? n.actions.length })}</summary>
              {n.truncated && <p className="notice">{t('emergency:truncated', { total: n.total_actions })}</p>}
              <ul>
                {n.actions.map((a, i) => (
                  <li key={`${a.at}-${i}`}>
                    {new Date(a.at).toLocaleTimeString()} <code>{a.event}</code> ({a.outcome})
                  </li>
                ))}
              </ul>
            </details>
            {n.acknowledged ? (
              <small>{t('emergency:acknowledged')}</small>
            ) : (
              <button type="button" onClick={() => ack.mutate(n.id)}>
                {t('emergency:acknowledge')}
              </button>
            )}
          </li>
        ))}
      </ul>
    </>
  )
}
