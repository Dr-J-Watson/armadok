import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Certificate {
  resolver: string
  names: string[]
  issuer: string
  not_before: string
  not_after: string
  serial: string
  days_left: number
  state: 'ok' | 'expiring' | 'expired'
}

interface HostState {
  host: string
  serial: string | null
  state: 'ok' | 'expiring' | 'expired' | 'missing'
}

// The certificates Traefik obtained (public facts only: the private keys never leave the
// machine) and, for each host the sites use, whether a valid certificate covers it.
export function CertsTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['certs', 'errors'])
  const certs = useQuery({
    queryKey: ['certs', nodeId],
    queryFn: () => api.get<{ certificates: Certificate[]; hosts: HostState[]; truncated: boolean }>(`/nodes/${nodeId}/certs`),
  })

  if (certs.isPending) return <p>{t('loading')}</p>
  if (certs.isError) return <p role="alert" className="error">{errorMessage(t, certs.error)}</p>
  const { certificates, hosts, truncated } = certs.data
  const problems = hosts.filter((h) => h.state !== 'ok')

  return (
    <>
      <p>{t('certs:explain')}</p>
      {truncated && <p className="notice">{t('certs:truncated')}</p>}
      {problems.length > 0 && (
        <section className="notice" aria-label={t('certs:problems.title')}>
          <h2>{t('certs:problems.title')}</h2>
          <ul className="links">
            {problems.map((h) => (
              <li key={h.host}>
                <strong>{h.host}</strong> <span className="badge off">{t(`certs:states.${h.state}`)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <h2>{t('certs:list.title')}</h2>
      {certificates.length === 0 && <p className="empty">{t('certs:list.empty')}</p>}
      {certificates.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>{t('certs:columns.names')}</th>
              <th>{t('certs:columns.issuer')}</th>
              <th>{t('certs:columns.expires')}</th>
              <th>{t('certs:columns.resolver')}</th>
            </tr>
          </thead>
          <tbody>
            {certificates.map((c) => (
              <tr key={`${c.resolver}:${c.serial}`}>
                <td data-label={t('certs:columns.names')}>
                  {c.names.map((n) => (
                    <div key={n}>{n}</div>
                  ))}
                </td>
                <td data-label={t('certs:columns.issuer')}>{c.issuer}</td>
                <td data-label={t('certs:columns.expires')}>
                  <span className={`badge ${c.state === 'ok' ? 'ok' : 'off'}`}>
                    {c.state === 'expired' ? t('certs:expired') : t('certs:daysLeft', { count: c.days_left })}
                  </span>{' '}
                  <small>{new Date(c.not_after).toLocaleDateString()}</small>
                </td>
                <td data-label={t('certs:columns.resolver')}>{c.resolver}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>{t('certs:hosts.title')}</h2>
      {hosts.length === 0 && <p className="empty">{t('certs:hosts.empty')}</p>}
      {hosts.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>{t('certs:hosts.host')}</th>
              <th>{t('certs:hosts.state')}</th>
            </tr>
          </thead>
          <tbody>
            {hosts.map((h) => (
              <tr key={h.host}>
                <td data-label={t('certs:hosts.host')}>{h.host}</td>
                <td data-label={t('certs:hosts.state')}>
                  <span className={`badge ${h.state === 'ok' ? 'ok' : 'off'}`}>{t(`certs:states.${h.state}`)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
