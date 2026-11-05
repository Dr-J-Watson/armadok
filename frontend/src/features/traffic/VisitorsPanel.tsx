import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api, ApiError } from '../../core/api'

const RANGES = ['1h', '24h', '7d'] as const
type Range = (typeof RANGES)[number]

interface Answer {
  masked: boolean
  visitors: { ip: string; requests: number; errors: number; bytes: number; first_seen: string; last_seen: string }[]
  paths: { path: string; requests: number; errors: number }[]
  classes: Record<string, number>
  kept: number
  reading: boolean
}

// What to add to Traefik's static configuration to write a JSON access log, and what to give
// the worker so it can read the file (mounted read-only).
export const ACCESS_LOG_SNIPPET = `accessLog:
  filePath: /var/log/traefik/access.log
  format: json`

// Who visits the sites, from Traefik's access log. Without the right to see addresses they are
// shown as networks only. Hidden entirely for someone who may not read the log.
export function VisitorsPanel({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traffic', 'errors'])
  const client = useQueryClient()
  const [range, setRange] = useState<Range>('24h')
  // Under the same prefix as the figures, so their Refresh button reloads this too. Reading the
  // log is audited, so it is read when asked for, not on a timer or at every window focus.
  const key = ['traffic', nodeId, 'visitors', range]
  const data = useQuery({
    queryKey: key,
    queryFn: () => api.get<Answer>(`/nodes/${nodeId}/traffic/visitors?range=${range}`),
    refetchOnWindowFocus: false,
    retry: false,
  })
  const purge = useMutation({
    mutationFn: () => api.del(`/nodes/${nodeId}/traffic/log`),
    onSuccess: () => client.invalidateQueries({ queryKey: ['traffic', nodeId, 'visitors'] }),
  })
  if (data.error instanceof ApiError && (data.error.status === 403 || data.error.status === 404)) return null
  const answer = data.data

  return (
    <section aria-label={t('traffic:visitors.title')}>
      <h3>{t('traffic:visitors.title')}</h3>
      <p className="hint">{t('traffic:visitors.privacy')}</p>
      {data.isError && <Problem error={data.error} />}
      {answer && !answer.reading && (
        <div className="notice">
          <p>{t('traffic:visitors.setup1')}</p>
          <pre>{ACCESS_LOG_SNIPPET}</pre>
          <p>{t('traffic:visitors.setup2')}</p>
          <pre>ARMADOK_TRAEFIK_ACCESSLOG: /var/log/traefik/access.log</pre>
        </div>
      )}
      {answer?.reading && (
        <>
          <div className="actions">
            {RANGES.map((r) => (
              <button key={r} type="button" aria-pressed={range === r} className={range === r ? 'primary' : undefined} onClick={() => setRange(r)}>
                {t(`traffic:range.${r}`)}
              </button>
            ))}
          </div>
          {answer.masked && <p className="hint">{t('traffic:visitors.masked')}</p>}
          {answer.visitors.length === 0 ? (
            <p className="empty">{t('traffic:visitors.none')}</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>{answer.masked ? t('traffic:visitors.network') : t('traffic:visitors.address')}</th>
                  <th>{t('traffic:col.requests')}</th>
                  <th>{t('traffic:col.errors')}</th>
                  <th>{t('traffic:visitors.lastSeen')}</th>
                </tr>
              </thead>
              <tbody>
                {answer.visitors.map((v) => (
                  <tr key={v.ip}>
                    <td data-label={t('traffic:visitors.address')}>{v.ip}</td>
                    <td data-label={t('traffic:col.requests')}>{v.requests}</td>
                    <td data-label={t('traffic:col.errors')}>{v.errors}</td>
                    <td data-label={t('traffic:visitors.lastSeen')}>{new Date(v.last_seen).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {answer.paths.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th>{t('traffic:visitors.path')}</th>
                  <th>{t('traffic:col.requests')}</th>
                  <th>{t('traffic:col.errors')}</th>
                </tr>
              </thead>
              <tbody>
                {answer.paths.map((p) => (
                  <tr key={p.path}>
                    <td data-label={t('traffic:visitors.path')}>{p.path}</td>
                    <td data-label={t('traffic:col.requests')}>{p.requests}</td>
                    <td data-label={t('traffic:col.errors')}>{p.errors}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="hint">{t('traffic:visitors.kept', { count: answer.kept })}</p>
          <button
            type="button"
            className="danger"
            disabled={purge.isPending}
            onClick={() => {
              if (window.confirm(t('traffic:visitors.purgeConfirm'))) purge.mutate()
            }}
          >
            {t('traffic:visitors.purge')}
          </button>
          {purge.isError && <Problem error={purge.error} />}
        </>
      )}
    </section>
  )
}
