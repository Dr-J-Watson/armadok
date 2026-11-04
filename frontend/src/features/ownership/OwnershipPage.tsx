import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Offer {
  id: string
  node_id: string
  node_name: string
  from_name: string | null
  to_name: string | null
  with_notes: boolean
  items: { type: string; id: string }[]
}

// The gifts waiting for an answer: those received (accept or refuse) and those sent (withdraw).
export function OwnershipPage() {
  const { t } = useTranslation(['ownership', 'errors'])
  const client = useQueryClient()
  const offers = useQuery({ queryKey: ['ownership', 'offers'], queryFn: () => api.get<{ sent: Offer[]; received: Offer[] }>('/ownership/offers') })
  const refresh = () => client.invalidateQueries({ queryKey: ['ownership'] })
  const accept = useMutation({ mutationFn: (id: string) => api.post(`/ownership/offers/${id}/accept`), onSuccess: refresh })
  const refuse = useMutation({ mutationFn: (id: string) => api.post(`/ownership/offers/${id}/refuse`), onSuccess: refresh })
  const withdraw = useMutation({ mutationFn: (id: string) => api.del(`/ownership/offers/${id}`), onSuccess: refresh })
  const failed = offers.error ?? accept.error ?? refuse.error ?? withdraw.error

  const list = (items: Offer[], received: boolean) => (
    <ul className="notes-list">
      {items.map((o) => (
        <li key={o.id} className="note-wrap">
          <p>
            <Link to={`/nodes/${o.node_id}`}>{o.node_name}</Link>{' '}
            {received ? t('ownership:from', { name: o.from_name ?? '?' }) : t('ownership:to', { name: o.to_name ?? '?' })}
          </p>
          <ul className="links">
            {o.items.map((i) => (
              <li key={`${i.type}:${i.id}`}>
                {i.type} · {i.id}
              </li>
            ))}
          </ul>
          {o.with_notes && <p className="hint">{t('ownership:notesIncluded')}</p>}
          {received ? (
            <>
              <button type="button" onClick={() => accept.mutate(o.id)}>
                {t('ownership:accept')}
              </button>{' '}
              <button type="button" onClick={() => refuse.mutate(o.id)}>
                {t('ownership:refuse')}
              </button>
            </>
          ) : (
            <button type="button" onClick={() => withdraw.mutate(o.id)}>
              {t('ownership:withdraw')}
            </button>
          )}
        </li>
      ))}
    </ul>
  )

  return (
    <>
      <h1>{t('ownership:page')}</h1>
      <p className="hint">{t('ownership:intro')}</p>
      {failed && <p role="alert" className="error">{errorMessage(t, failed)}</p>}
      <h2>{t('ownership:received')}</h2>
      {offers.data?.received.length === 0 && <p className="empty">{t('ownership:noneReceived')}</p>}
      {offers.data && list(offers.data.received, true)}
      <h2>{t('ownership:sent')}</h2>
      {offers.data?.sent.length === 0 && <p className="empty">{t('ownership:noneSent')}</p>}
      {offers.data && list(offers.data.sent, false)}
    </>
  )
}
