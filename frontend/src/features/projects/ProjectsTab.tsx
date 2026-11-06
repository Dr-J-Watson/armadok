import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { FileEditor } from '../../shell/FileEditor'
import { Discovered } from './Discovered'
import type { Found } from './Discovered'
import { NewProject } from './NewProject'
import { ProjectRun } from './ProjectRun'
import { Problem } from '../../components/Problem'
import { useFeatures } from '../../core/useFeatures'
import { useConfirm } from '../../components/Confirm'

interface Project {
  name: string
  files: string[]
  labels?: string[]
}

type View = 'managed' | 'import' | 'ignored'

export function ProjectsTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['projects', 'errors'])
  const [params, setParams] = useSearchParams()
  const [creating, setCreating] = useState(false)
  const [filter, setFilter] = useState('')
  const client = useQueryClient()
  const ask = useConfirm()
  const panels = useFeatures().flatMap((f) => f.projectPanels ?? [])
  const projects = useQuery({
    queryKey: ['projects', nodeId],
    queryFn: () => api.get<{ projects: Project[] }>(`/nodes/${nodeId}/projects`),
  })
  // Folders that could be imported: only for those allowed to (the server answers 403/404 to
  // anyone else, and then the extra views simply do not exist).
  const discovered = useQuery({
    queryKey: ['projects', nodeId, 'discovered'],
    queryFn: () => api.get<{ projects: Found[] }>(`/nodes/${nodeId}/projects/discovered`),
    retry: false,
  })
  const unmanage = useMutation({
    mutationFn: (name: string) => api.post(`/nodes/${nodeId}/projects/${encodeURIComponent(name)}/unmanage`),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['projects', nodeId] })
    },
  })
  const found = discovered.data?.projects ?? []
  const toImport = found.filter((f) => f.state === 'discovered').length
  const setAside = found.filter((f) => f.state === 'ignored').length
  const asked = params.get('view')
  const view: View = (asked === 'import' || asked === 'ignored') && discovered.isSuccess ? asked : 'managed'
  // The open file lives in the address, so it can be shared and survives a reload.
  const project = params.get('project')
  const file = params.get('file')

  function open(name: string, fileName: string) {
    const next = new URLSearchParams(params)
    next.set('project', name)
    next.set('file', fileName)
    setParams(next, { replace: true })
  }

  if (projects.isPending) return <p className="loading">{t('loading')}</p>
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
    <div className="actions between">
      <p className="hint">{t('projects:count', { count: projects.data.projects.length })}</p>
      <button type="button" className="primary" title={t('projects:new.tip')} onClick={() => setCreating(true)}>
        {t('projects:new.button')}
      </button>
    </div>
  )
  const views = discovered.isSuccess && (
    <div className="segmented" role="group" aria-label={t('projects:views.title')}>
      {(['managed', 'import', 'ignored'] as const).map((v) => (
        <button key={v} type="button" aria-pressed={view === v} onClick={() => setParams({ view: v }, { replace: true })}>
          {t(`projects:views.${v}`, { count: v === 'managed' ? projects.data.projects.length : v === 'import' ? toImport : setAside })}
        </button>
      ))}
    </div>
  )
  if (view !== 'managed')
    return (
      <>
        {views}
        <Discovered nodeId={nodeId} found={found} ignored={view === 'ignored'} />
      </>
    )
  if (projects.data.projects.length === 0)
    return (
      <>
        {views}
        <p className="empty">{t(toImport > 0 ? 'projects:emptyWithFound' : 'projects:empty', { count: toImport })}</p>
        {create}
      </>
    )

  return (
    <>
    {views}
    {create}
    <div className="projects-layout">
      <nav aria-label={t('projects:list')}>
        {projects.data.projects.length > 6 && (
          <input type="search" aria-label={t('projects:search')} placeholder={t('projects:search')} value={filter} onChange={(e) => setFilter(e.target.value)} />
        )}
        <ul className="links">
          {projects.data.projects.filter((p) => p.name.toLowerCase().includes(filter.trim().toLowerCase())).map((p) => (
            <li key={p.name}>
              <strong>{p.name}</strong>
              {(p.labels ?? []).map((l) => (
                <span key={l} className="badge">{l}</span>
              ))}
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
        {project && discovered.isSuccess && (
          <div className="actions">
            <button
              type="button"
              title={t('projects:unmanage.tip')}
              onClick={async () => {
                if (await ask(t('projects:unmanage.confirm', { project }), { danger: false })) {
                  unmanage.mutate(project)
                  setParams({}, { replace: true })
                }
              }}
            >
              {t('projects:unmanage.button')}
            </button>
          </div>
        )}
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
