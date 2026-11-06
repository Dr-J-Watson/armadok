import { useInfiniteQuery, useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, download } from '../../core/api'
import { useCan } from '../../core/auth'
import { Problem } from '../../components/Problem'
import { Download, ShieldCheck, X } from 'lucide-react'

interface Entry {
  id: number
  at: string
  actor_name: string
  event: string
  object_type: string | null
  object_id: string | null
  outcome: 'success' | 'denied' | 'failure'
  details: Record<string, string | number | boolean | null>
}

interface Page {
  entries: Entry[]
  next_before_id: number | null
}

// The audit log, optionally limited to one node. The server decides what the viewer may see.
export function AuditList({ nodeId }: { nodeId?: string }) {
  const { t } = useTranslation(['audit', 'errors'])
  const canVerify = useCan('audit.read')
  const [actor, setActor] = useState('')
  const [event, setEvent] = useState('')
  const [outcome, setOutcome] = useState('')

  const filters = new URLSearchParams()
  if (nodeId) filters.set('node_id', nodeId)
  if (actor.trim()) filters.set('actor', actor.trim())
  if (event.trim()) filters.set('event', event.trim())
  if (outcome) filters.set('outcome', outcome)
  const query = filters.toString()

  const log = useInfiniteQuery({
    queryKey: ['audit', query],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams(filters)
      if (pageParam) params.set('before_id', String(pageParam))
      return api.get<Page>(`/audit?${params}`)
    },
    getNextPageParam: (last) => last.next_before_id,
  })
  const verify = useMutation({
    mutationFn: () => api.get<{ intact: boolean; signed: boolean; checked: number; first_bad_id: number | null }>('/audit/verify'),
  })
  const exportAs = useMutation({
    mutationFn: (format: 'json' | 'csv') => download(`/audit/export?format=${format}&${query}`, `audit.${format}`),
  })

  const entries = log.data?.pages.flatMap((p) => p.entries) ?? []
  const label = (e: Entry) => t(`audit:event.${e.event}`, { defaultValue: e.event })

  return (
    <>
      <form className="filters" onSubmit={(e) => e.preventDefault()}>
        <label>
          <span className="visually-hidden">{t('audit:filters.actor')}</span>
          <input placeholder={t('audit:filters.actor')} value={actor} onChange={(e) => setActor(e.target.value)} />
        </label>
        <label>
          <span className="visually-hidden">{t('audit:filters.event')}</span>
          <input placeholder={t('audit:filters.event')} value={event} onChange={(e) => setEvent(e.target.value)} />
        </label>
        <label>
          <span className="visually-hidden">{t('audit:filters.outcome')}</span>
          <select value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label={t('audit:filters.outcome')}>
            <option value="">{t('audit:outcome.all')}</option>
            {(['success', 'denied', 'failure'] as const).map((o) => (
              <option key={o} value={o}>
                {t(`audit:outcome.${o}`)}
              </option>
            ))}
          </select>
        </label>
        {(actor || event || outcome) && (
          <button type="button" onClick={() => { setActor(''); setEvent(''); setOutcome('') }} title={t('audit:filters.clearTip')}>
            <X size={14} aria-hidden="true" /> {t('audit:filters.clear')}
          </button>
        )}
        <button type="button" title={t('audit:export.tip')} onClick={() => exportAs.mutate('csv')}>
          <Download size={14} aria-hidden="true" /> {t('audit:export.csv')}
        </button>
        <button type="button" title={t('audit:export.tip')} onClick={() => exportAs.mutate('json')}>
          <Download size={14} aria-hidden="true" /> {t('audit:export.json')}
        </button>
        {!nodeId && canVerify && (
          <button type="button" title={t('audit:verify.tip')} disabled={verify.isPending} onClick={() => verify.mutate()}>
            <ShieldCheck size={14} aria-hidden="true" /> {t('audit:verify.button')}
          </button>
        )}
      </form>

      {verify.data && (
        <p role="status" className={verify.data.intact ? 'ok-text' : 'error'}>
          {verify.data.intact
            ? t('audit:verify.intact', { count: verify.data.checked })
            : t('audit:verify.altered', { id: verify.data.first_bad_id })}
          {verify.data.signed ? '' : ` ${t('audit:verify.unsigned')}`}
        </p>
      )}
      {(log.error ?? verify.error ?? exportAs.error) != null && (
        <Problem error={log.error ?? verify.error ?? exportAs.error} />
      )}
      {log.isPending && <p className="loading">{t('loading')}</p>}
      {log.isSuccess && entries.length === 0 && <p className="empty">{t('audit:empty')}</p>}

      {entries.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>{t('audit:columns.when')}</th>
              <th>{t('audit:columns.who')}</th>
              <th>{t('audit:columns.what')}</th>
              <th>{t('audit:columns.result')}</th>
              <th>{t('audit:columns.details')}</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id}>
                <td data-label={t('audit:columns.when')}>
                  <time dateTime={e.at} title={e.at}>{new Date(e.at).toLocaleString()}</time>
                </td>
                <td data-label={t('audit:columns.who')}>{e.actor_name}</td>
                <td data-label={t('audit:columns.what')}>{label(e)}</td>
                <td data-label={t('audit:columns.result')}>
                  <span className={`badge ${e.outcome === 'success' ? 'ok' : 'danger-badge'}`}>{t(`audit:outcome.${e.outcome}`)}</span>
                </td>
                <td data-label={t('audit:columns.details')}>
                  <small>
                    {Object.entries(e.details)
                      .map(([k, v]) => `${k}: ${String(v)}`)
                      .join(' · ')}
                  </small>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {log.hasNextPage && (
        <button type="button" disabled={log.isFetchingNextPage} onClick={() => void log.fetchNextPage()}>
          {t('audit:more')}
        </button>
      )}
    </>
  )
}
