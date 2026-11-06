import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

interface Router {
  id: string
  name: string
  rule: string
  service: string
  entrypoints: string[]
  middlewares: string[]
  tls: boolean
  path: string
}

interface Sites {
  routers: Router[]
  services: { id: string; urls: string[] }[]
  middlewares: { id: string }[]
  errors: { path: string; error: string }[]
  truncated: boolean
}

// What the configuration files define, one row per router (a "site").
export function SitesView({ nodeId, onOpenFile }: { nodeId: string; onOpenFile: (path: string) => void }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const sites = useQuery({
    queryKey: ['traefik-sites', nodeId],
    queryFn: () => api.get<Sites>(`/nodes/${nodeId}/traefik/sites`),
  })

  const [filter, setFilter] = useState('')
  if (sites.isPending) return <p className="loading">{t('loading')}</p>
  if (sites.isError)
    return (
      <Problem error={sites.error} />
    )
  const { routers: all, errors, truncated } = sites.data
  const needle = filter.trim().toLowerCase()
  const routers = needle ? all.filter((r) => `${r.name} ${r.rule} ${r.service}`.toLowerCase().includes(needle)) : all
  return (
    <>
      {truncated && <p className="notice">{t('traefik:truncated')}</p>}
      {errors.map((e) => (
        <p key={e.path} className="notice" role="alert">
          {t('traefik:badFile', { path: e.path })}{' '}
          <button type="button" className="link-button" onClick={() => onOpenFile(e.path)}>
            {t('traefik:openFile')}
          </button>
        </p>
      ))}
      {all.length > 5 && (
        <input type="search" aria-label={t('traefik:searchSites')} placeholder={t('traefik:searchSites')} value={filter} onChange={(e) => setFilter(e.target.value)} />
      )}
      {all.length === 0 ? (
        <p className="empty">{t('traefik:noSites')}</p>
      ) : routers.length === 0 ? (
        <p className="empty">{t('traefik:noSiteMatch')}</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>{t('traefik:columns.name')}</th>
              <th>{t('traefik:columns.rule')}</th>
              <th>{t('traefik:columns.service')}</th>
              <th>{t('traefik:columns.middlewares')}</th>
              <th>{t('traefik:columns.https')}</th>
              <th>{t('traefik:columns.file')}</th>
            </tr>
          </thead>
          <tbody>
            {routers.map((r) => (
              <tr key={`${r.path}:${r.id}`}>
                <td data-label={t('traefik:columns.name')}>
                  {r.name}{' '}
                  <Link to={`/nodes/${nodeId}?tab=map&focus=${encodeURIComponent(`router:${r.id}`)}`}>
                    {t('traefik:relations')}
                  </Link>
                </td>
                <td data-label={t('traefik:columns.rule')}>
                  <code>{r.rule}</code>
                </td>
                <td data-label={t('traefik:columns.service')}>{r.service.replace(/@file$/, '')}</td>
                <td data-label={t('traefik:columns.middlewares')}>
                  {r.middlewares.map((m) => m.replace(/@file$/, '')).join(', ')}
                </td>
                <td data-label={t('traefik:columns.https')}>
                  <span className={`badge ${r.tls ? 'ok' : 'off'}`}>{r.tls ? t('traefik:tls.on') : t('traefik:tls.off')}</span>
                </td>
                <td data-label={t('traefik:columns.file')}>
                  <button type="button" className="link-button" onClick={() => onOpenFile(r.path)}>
                    {r.path}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
