import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { FileEditor } from '../../shell/FileEditor'
import { NewProject } from './NewProject'
import { ProjectRun } from './ProjectRun'
import { Problem } from '../../components/Problem'
import { useFeatures } from '../../core/useFeatures'

interface Project {
  name: string
  files: string[]
}

export function ProjectsTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['projects', 'errors'])
  const [params, setParams] = useSearchParams()
  const [creating, setCreating] = useState(false)
  const panels = useFeatures().flatMap((f) => f.projectPanels ?? [])
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
      <Problem error={projects.error} />
    )
  const create = creating ? (
    <NewProject
      nodeId={nodeId}
      onCancel={() => setCreating(false)}
      onCreated={(name) => {
        setCreating(false)
        open(name, 'compose.yaml')
      }}
    />
  ) : (
    <div className="actions">
      <button type="button" className="primary" onClick={() => setCreating(true)}>
        {t('projects:new.button')}
      </button>
    </div>
  )
  if (projects.data.projects.length === 0)
    return (
      <>
        <p className="empty">{t('projects:empty')}</p>
        {create}
      </>
    )

  return (
    <>
    {create}
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
        {project && <ProjectRun key={project} nodeId={nodeId} project={project} />}
        {project && panels.map((p) => <div key={p.id}>{p.render({ nodeId, project })}</div>)}
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
    </>
  )
}
