import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { FileEditor } from '../../shell/FileEditor'
import { Coherence } from './Coherence'
import { DomainSwitch } from './DomainSwitch'
import { StaticConfig } from './StaticConfig'
import { Problem } from '../../components/Problem'

type View = 'coherence' | 'static' | 'domains' | 'files'

// The less common Traefik pages: what the files say about each other, the static configuration,
// pausing a whole domain, and the raw files. Sites and middlewares have pages of their own.
export function TraefikAdvanced({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const [params, setParams] = useSearchParams()
  // View and open file live in the address: shareable, and kept on reload.
  const asked = params.get('view')
  const view: View = asked === 'files' || asked === 'domains' || asked === 'static' ? asked : 'coherence'
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
        {(['coherence', 'static', 'domains', 'files'] as const).map((name) => (
          <button
            key={name}
            type="button"
            aria-pressed={view === name}
            className={view === name ? 'primary' : undefined}
            title={t(`traefik:viewHints.${name}`)}
            onClick={() => go({ view: name })}
          >
            {t(`traefik:views.${name}`)}
          </button>
        ))}
      </div>
      {view === 'domains' ? (
        <DomainSwitch nodeId={nodeId} />
      ) : view === 'static' ? (
        <StaticConfig nodeId={nodeId} />
      ) : view === 'coherence' ? (
        <Coherence nodeId={nodeId} onOpenFile={(path) => go({ view: 'files', file: path })} />
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
  if (files.isPending) return <p className="loading">{t('loading')}</p>
  if (files.isError)
    return (
      <Problem error={files.error} />
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
