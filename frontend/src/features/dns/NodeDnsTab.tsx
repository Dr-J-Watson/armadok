import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

interface Host {
  host: string
  domain_id: string | null
  status: 'present' | 'missing' | 'unmanaged' | 'no_domain' | 'error'
  resolves: string[]
}

// The hosts the sites of a node use, against the DNS: present, missing (with a one-click fix),
// or not checkable (local DNS, no domain added, provider unreachable).
export function NodeDnsTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['dns', 'errors'])
  const queryClient = useQueryClient()
  const report = useQuery({
    queryKey: ['dns-report', nodeId],
    queryFn: () => api.get<{ hosts: Host[] }>(`/nodes/${nodeId}/dns/report`),
  })
  const fix = useMutation({
    mutationFn: (host: string) => api.post(`/nodes/${nodeId}/dns/fix`, { host }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dns-report', nodeId] }),
  })

  if (report.isPending) return <p className="loading">{t('loading')}</p>
  if (report.isError) return <Problem error={report.error} />
  const hosts = report.data.hosts
  const missing = hosts.filter((h) => h.status === 'missing').length
  if (hosts.length === 0) return <p className="empty">{t('dns:report.empty')}</p>

  return (
    <>
      <p className="hint">{t('dns:report.explain')}</p>
      {missing > 0 && <p className="notice" role="status">{t('dns:report.missing', { count: missing })}</p>}
      {fix.isError && <Problem error={fix.error} />}
      <table className="table">
        <thead>
          <tr><th>{t('dns:report.host')}</th><th>{t('dns:report.status')}</th><th>{t('dns:report.resolves')}</th><th /></tr>
        </thead>
        <tbody>
          {hosts.map((h) => (
            <tr key={h.host}>
              <td data-label={t('dns:report.host')}>{h.host}</td>
              <td data-label={t('dns:report.status')}>
                <span className={`badge ${h.status === 'present' ? 'ok' : 'off'}`}>{t(`dns:report.states.${h.status}`)}</span>
              </td>
              <td data-label={t('dns:report.resolves')}>
                {h.resolves.length ? h.resolves.join(', ') : <span className="muted">{t('dns:report.noAnswer')}</span>}
              </td>
              <td>
                {h.status === 'missing' && (
                  <button type="button" disabled={fix.isPending} title={t('dns:report.fixTip')} onClick={() => fix.mutate(h.host)}>{t('dns:report.fix')}</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
