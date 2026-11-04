import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { codeMessage, errorMessage } from '../../core/errors'
import { Sparkline } from './Sparkline'

const RANGES = ['1h', '24h', '7d', '30d'] as const
type Range = (typeof RANGES)[number]

interface Service {
  service: string
  requests: number
  per_second: number
  errors_5xx: number
  errors_4xx: number
  error_rate: number
  previous_requests: number
  bytes_in: number
  bytes_out: number
  p50: number | null
  p95: number | null
  p99: number | null
  series: number[][]
}

interface Answer {
  range: Range
  services: Service[]
  status: { last_ok: string | null; last_error: string | null; reload_ok: boolean | null; servers: Record<string, { up: number; down: number }> } | null
}

// What to add to Traefik's static configuration to publish its metrics on a private entry point.
// The entry point must stay unpublished (not in the ports of the container): only the worker
// reads it, from the same Docker network.
export const SNIPPET = `entryPoints:
  metrics:
    address: ":8082"
metrics:
  prometheus:
    entryPoint: metrics
    addEntryPointsLabels: true
    addServicesLabels: true`

const seconds = (value: number | null) => (value === null ? '—' : value < 1 ? `${Math.round(value * 1000)} ms` : `${value.toFixed(2)} s`)
const bytes = (n: number) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MiB` : `${Math.round(n / 1024)} KiB`)

// Volumes, errors and response times of the sites, read from Traefik's metrics. No visitor
// address is involved: Traefik's metrics never hold one.
export function TrafficTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traffic', 'errors'])
  const client = useQueryClient()
  const [range, setRange] = useState<Range>('24h')
  const base = `/nodes/${nodeId}/traffic`
  const stats = useQuery({
    queryKey: ['traffic', nodeId, range],
    queryFn: () => api.get<Answer>(`${base}?range=${range}`),
    refetchInterval: 60_000,
  })
  const test = useMutation({
    mutationFn: () => api.post<{ ok: boolean; error: string | null }>(`${base}/test`),
    onSuccess: () => client.invalidateQueries({ queryKey: ['traffic', nodeId] }),
  })
  const status = stats.data?.status
  const unconfigured = status?.last_error === 'worker.metrics_not_configured' || status === null

  return (
    <section aria-label={t('traffic:title')}>
      <h2>{t('traffic:title')}</h2>
      <p className="hint">{t('traffic:privacy')}</p>
      {stats.isError && (
        <p role="alert" className="error">
          {errorMessage(t, stats.error)}
        </p>
      )}
      <div className="actions">
        {RANGES.map((r) => (
          <button key={r} type="button" aria-pressed={range === r} className={range === r ? 'primary' : undefined} onClick={() => setRange(r)}>
            {t(`traffic:range.${r}`)}
          </button>
        ))}
        <button type="button" disabled={test.isPending} onClick={() => test.mutate()}>
          {t('traffic:test')}
        </button>
      </div>
      {test.data && (
        <p role="status">{test.data.ok ? t('traffic:testOk') : codeMessage(t, test.data.error ?? 'common.unknown')}</p>
      )}
      {status && !status.last_error && (
        <p className="hint">
          {t('traffic:lastRead', { at: status.last_ok ? new Date(status.last_ok).toLocaleString() : '—' })}
          {status.reload_ok === false && ` — ${t('traffic:reloadFailed')}`}
        </p>
      )}
      {status?.last_error && !unconfigured && <p className="error">{codeMessage(t, status.last_error)}</p>}
      {unconfigured && (
        <div className="notice">
          <h3>{t('traffic:setup.title')}</h3>
          <p>{t('traffic:setup.step1')}</p>
          <pre>{SNIPPET}</pre>
          <p>{t('traffic:setup.step2')}</p>
          <pre>ARMADOK_TRAEFIK_METRICS_URL: http://traefik:8082/metrics</pre>
          <p>{t('traffic:setup.step3')}</p>
        </div>
      )}
      {stats.data && stats.data.services.length === 0 && !unconfigured && <p className="empty">{t('traffic:none')}</p>}
      {stats.data && stats.data.services.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>{t('traffic:col.service')}</th>
              <th>{t('traffic:col.requests')}</th>
              <th>{t('traffic:col.errors')}</th>
              <th>{t('traffic:col.latency')}</th>
              <th>{t('traffic:col.bytes')}</th>
              <th>{t('traffic:col.trend')}</th>
            </tr>
          </thead>
          <tbody>
            {stats.data.services.map((s) => (
              <tr key={s.service}>
                <td data-label={t('traffic:col.service')}>
                  <strong>{s.service.replace(/@.*$/, '')}</strong>
                </td>
                <td data-label={t('traffic:col.requests')}>
                  {s.requests}
                  {s.previous_requests > 0 && <small> ({s.requests >= s.previous_requests ? '+' : ''}{Math.round(((s.requests - s.previous_requests) / s.previous_requests) * 100)} %)</small>}
                </td>
                <td data-label={t('traffic:col.errors')} className={s.error_rate > 0.05 ? 'error' : undefined}>
                  {(s.error_rate * 100).toFixed(1)} % <small>({s.errors_5xx} × 5xx, {s.errors_4xx} × 4xx)</small>
                </td>
                <td data-label={t('traffic:col.latency')}>
                  p50 {seconds(s.p50)} · p95 {seconds(s.p95)} · p99 {seconds(s.p99)}
                </td>
                <td data-label={t('traffic:col.bytes')}>
                  ↓ {bytes(s.bytes_in)} · ↑ {bytes(s.bytes_out)}
                </td>
                <td data-label={t('traffic:col.trend')}>
                  <Sparkline series={s.series} label={t('traffic:trend', { service: s.service })} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <button type="button" className="link-button" onClick={() => client.invalidateQueries({ queryKey: ['traffic', nodeId] })}>
        {t('traffic:refresh')}
      </button>
    </section>
  )
}
