import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

interface DnsHost {
  host: string
  status: 'present' | 'missing' | 'unmanaged' | 'no_domain' | 'error'
}

interface CertHost {
  host: string
  state: 'ok' | 'expiring' | 'expired' | 'missing'
}

// What comes after saving a site: its DNS records, then its certificate. Each step uses the
// feature's own API (and its own rights), so someone without DNS rights simply sees the step as
// unavailable instead of the whole flow failing. Nothing here is required: the site already exists.
export function SiteFollowUp({ nodeId, hosts, https, onDone }: { nodeId: string; hosts: string[]; https: boolean; onDone: () => void }) {
  const { t } = useTranslation(['traefik', 'dns', 'certs', 'errors'])
  const queryClient = useQueryClient()
  const dns = useQuery({
    queryKey: ['dns-report', nodeId],
    queryFn: () => api.get<{ hosts: DnsHost[] }>(`/nodes/${nodeId}/dns/report`),
    retry: false,
  })
  const certs = useQuery({
    queryKey: ['certs', nodeId],
    enabled: https,
    queryFn: () => api.get<{ hosts: CertHost[] }>(`/nodes/${nodeId}/certs`),
    retry: false,
    // Traefik asks for the certificate once the name resolves: look again until it is there.
    refetchInterval: (query) =>
      hosts.every((h) => query.state.data?.hosts.find((x) => x.host === h)?.state === 'ok') ? false : 10000,
  })
  const fix = useMutation({
    mutationFn: (host: string) => api.post(`/nodes/${nodeId}/dns/fix`, { host }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['dns-report', nodeId] }),
  })

  const dnsOf = (host: string) => dns.data?.hosts.find((h) => h.host === host)
  const certOf = (host: string) => certs.data?.hosts.find((h) => h.host === host)

  return (
    <section aria-label={t('traefik:followUp.title')}>
      <h3>{t('traefik:followUp.title')}</h3>
      <p role="status">{t('traefik:followUp.created')}</p>
      <table className="table">
        <thead>
          <tr>
            <th>{t('dns:report.host')}</th>
            <th>{t('dns:report.status')}</th>
            {https && <th>{t('certs:hosts.state')}</th>}
          </tr>
        </thead>
        <tbody>
          {hosts.map((host) => {
            const d = dnsOf(host)
            const c = certOf(host)
            return (
              <tr key={host}>
                <td data-label={t('dns:report.host')}>{host}</td>
                <td data-label={t('dns:report.status')}>
                  {dns.isError ? (
                    <span className="muted">{t('traefik:followUp.unavailable')}</span>
                  ) : d ? (
                    <>
                      <span className={`badge ${d.status === 'present' ? 'ok' : 'off'}`}>{t(`dns:report.states.${d.status}`)}</span>{' '}
                      {d.status === 'missing' && (
                        <button type="button" disabled={fix.isPending} onClick={() => fix.mutate(host)}>
                          {t('dns:report.fix')}
                        </button>
                      )}
                    </>
                  ) : (
                    <span className="muted">…</span>
                  )}
                </td>
                {https && (
                  <td data-label={t('certs:hosts.state')}>
                    {certs.isError ? (
                      <span className="muted">{t('traefik:followUp.unavailable')}</span>
                    ) : c ? (
                      <span className={`badge ${c.state === 'ok' ? 'ok' : 'off'}`}>{t(`certs:states.${c.state}`)}</span>
                    ) : (
                      <span className="muted">…</span>
                    )}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
      {fix.isError && (
        <Problem error={fix.error} />
      )}
      {https && <p className="hint">{t('traefik:followUp.certHint')}</p>}
      <div className="actions">
        <button type="button" className="primary" onClick={onDone}>
          {t('traefik:followUp.done')}
        </button>
      </div>
    </section>
  )
}
