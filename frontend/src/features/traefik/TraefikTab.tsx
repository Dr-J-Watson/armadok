import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { FileEditor } from '../../shell/FileEditor'
import { Coherence } from './Coherence'
import { DomainSwitch } from './DomainSwitch'
import { ManagedSites } from './ManagedSites'
import { Middlewares } from './Middlewares'
import { SitesView } from './SitesView'
import { StaticConfig } from './StaticConfig'

type View = 'sites' | 'managed' | 'middlewares' | 'coherence' | 'static' | 'files'

export function TraefikTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const [params, setParams] = useSearchParams()
  // View and open file live in the address: shareable, and kept on reload.
  const asked = params.get('view')
  const view: View = asked === 'files' || asked === 'managed' || asked === 'middlewares' || asked === 'coherence' || asked === 'static' ? asked : 'sites'
  const file = params.get('file')

  function go(next: Record<string, string | null>) {
    const merged = new URLSearchParams(params)
    for (const [key, value] of Object.entries(next)) {
      if (value === null) merged.delete(key)
      else merged.set(key, value)
    }
    setParams(merged, { replace: true })
  }

  return (
    <>
      <div className="actions">
        {(['sites', 'managed', 'middlewares', 'coherence', 'static', 'files'] as const).map((name) => (
          <button
            key={name}
            type="button"
            aria-pressed={view === name}
            className={view === name ? 'primary' : undefined}
            onClick={() => go({ view: name })}
          >
            {t(`traefik:views.${name}`)}
          </button>
        ))}
      </div>
      {view === 'managed' ? (
        <>
          <ManagedSites nodeId={nodeId} />
          <DomainSwitch nodeId={nodeId} />
        </>
      ) : view === 'middlewares' ? (
        <Middlewares nodeId={nodeId} />
      ) : view === 'static' ? (
        <StaticConfig nodeId={nodeId} />
      ) : view === 'coherence' ? (
        <Coherence nodeId={nodeId} onOpenFile={(path) => go({ view: 'files', file: path })} />
      ) : view === 'sites' ? (
        <SitesView nodeId={nodeId} onOpenFile={(path) => go({ view: 'files', file: path })} />
      ) : (
        <FilesView nodeId={nodeId} file={file} onPick={(path) => go({ file: path })} />
      )}
    </>
  )
}

function FilesView({ nodeId, file, onPick }: { nodeId: string; file: string | null; onPick: (path: string) => void }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const files = useQuery({
    queryKey: ['traefik-files', nodeId],
    queryFn: () => api.get<{ files: { path: string; size: number }[]; truncated: boolean }>(`/nodes/${nodeId}/traefik/files`),
  })
  if (files.isPending) return <p>{t('loading')}</p>
  if (files.isError)
    return (
      <p role="alert" className="error">
        {errorMessage(t, files.error)}
      </p>
    )
  if (files.data.files.length === 0) return <p className="empty">{t('traefik:noFiles')}</p>

  return (
    <div className="projects-layout">
      <nav aria-label={t('traefik:fileList')}>
        <ul className="links">
          {files.data.files.map((f) => (
            <li key={f.path}>
              <button
                type="button"
                className="link-button"
                aria-current={file === f.path ? 'true' : undefined}
                onClick={() => onPick(f.path)}
              >
                {f.path}
              </button>
            </li>
          ))}
        </ul>
      </nav>
      <div>
        {file ? (
          <FileEditor
            key={file}
            path={`/nodes/${nodeId}/traefik/files/${file.split('/').map(encodeURIComponent).join('/')}`}
            title={file}
            queryKey={['traefik-file', nodeId, file]}
          />
        ) : (
          <p className="hint">{t('traefik:pickOne')}</p>
        )}
      </div>
    </div>
  )
}
