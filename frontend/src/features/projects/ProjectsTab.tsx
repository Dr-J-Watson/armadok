import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { FileEditor } from '../../shell/FileEditor'

interface Project {
  name: string
  files: string[]
}

export function ProjectsTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['projects', 'errors'])
  const [params, setParams] = useSearchParams()
  const projects = useQuery({
    queryKey: ['projects', nodeId],
    queryFn: () => api.get<{ projects: Project[] }>(`/nodes/${nodeId}/projects`),
  })
  // The open file lives in the address, so it can be shared and survives a reload.
  const project = params.get('project')
  const file = params.get('file')

  function open(name: string, fileName: string) {
    const next = new URLSearchParams(params)
    next.set('project', name)
    next.set('file', fileName)
    setParams(next, { replace: true })
  }

  if (projects.isPending) return <p>{t('loading')}</p>
  if (projects.isError)
    return (
      <p role="alert" className="error">
        {errorMessage(t, projects.error)}
      </p>
    )
  if (projects.data.projects.length === 0) return <p className="empty">{t('projects:empty')}</p>

  return (
    <div className="projects-layout">
      <nav aria-label={t('projects:list')}>
        <ul className="links">
          {projects.data.projects.map((p) => (
            <li key={p.name}>
              <strong>{p.name}</strong>
              <ul className="links">
                {p.files.map((f) => (
                  <li key={f}>
                    <button
                      type="button"
                      className="link-button"
                      aria-current={project === p.name && file === f ? 'true' : undefined}
                      onClick={() => open(p.name, f)}
                    >
                      {f}
                    </button>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </nav>
      <div>
        {project && file ? (
          // Changing file starts a fresh editor: no draft leaks from one file to another.
          <FileEditor
            key={`${project}/${file}`}
            path={`/nodes/${nodeId}/projects/${encodeURIComponent(project)}/files/${encodeURIComponent(file)}`}
            title={`${project} / ${file}`}
            fileName={file}
            queryKey={['project-file', nodeId, project, file]}
          />
        ) : (
          <p className="hint">{t('projects:pickOne')}</p>
        )}
      </div>
    </div>
  )
}
