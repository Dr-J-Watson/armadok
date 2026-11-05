import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

interface Issue {
  severity: 'error' | 'warning' | 'info'
  code: string
  subject: string
  path: string
  detail: string
}

interface Answer {
  issues: Issue[]
  errors: { path: string; error: string }[]
  truncated: boolean
}

// Whether the configuration files agree with each other: references to things that do not
// exist, middleware chains that loop, the same site twice, and leftovers nobody uses.
export function Coherence({ nodeId, onOpenFile }: { nodeId: string; onOpenFile: (path: string) => void }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const check = useQuery({ queryKey: ['traefik-coherence', nodeId], queryFn: () => api.get<Answer>(`/nodes/${nodeId}/traefik/coherence`) })
  const data = check.data
  const count = (severity: Issue['severity']) => data?.issues.filter((i) => i.severity === severity).length ?? 0

  return (
    <>
      <p>{t('traefik:coherence.explain')}</p>
      <button type="button" disabled={check.isFetching} onClick={() => void check.refetch()}>
        {t('traefik:coherence.recheck')}
      </button>
      {check.isError && (
        <Problem error={check.error} />
      )}
      {data && (
        <>
          <p role="status" className={data.issues.some((i) => i.severity === 'error') ? 'error' : ''}>
            {data.issues.length === 0
              ? t('traefik:coherence.clean')
              : t('traefik:coherence.summary', { errors: count('error'), warnings: count('warning'), infos: count('info') })}
          </p>
          {data.truncated && <p className="notice">{t('traefik:coherence.truncated')}</p>}
          {data.errors.map((e) => (
            <p key={e.path} className="error">
              {t('traefik:coherence.unreadable', { path: e.path })}
            </p>
          ))}
          <ul className="links">
            {data.issues.map((i) => (
              <li key={`${i.code}:${i.subject}:${i.path}:${i.detail}`}>
                <span className={`badge ${i.severity === 'info' ? '' : 'off'}`}>{t(`traefik:coherence.severity.${i.severity}`)}</span>{' '}
                <strong>{i.subject}</strong> — {t(`traefik:coherence.codes.${i.code}`, { detail: i.detail, defaultValue: i.code })}{' '}
                <button type="button" className="link-button" onClick={() => onOpenFile(i.path)}>
                  {i.path}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  )
}
