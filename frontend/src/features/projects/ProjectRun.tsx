import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Service {
  service: string
  name: string
  state: string
  status: string
  image: string
}

interface Task {
  task_id: string
  project: string
  action: string
  state: 'running' | 'success' | 'failed'
  exit_code: number | null
  output?: string
}

const ACTIONS = ['up', 'down', 'stop', 'start', 'restart', 'pull', 'build'] as const

// Run Compose actions on one project, see its services and read its logs. A long action runs
// in the background on the machine: the screen polls the task until it ends.
export function ProjectRun({ nodeId, project }: { nodeId: string; project: string }) {
  const { t } = useTranslation(['projects', 'errors'])
  const queryClient = useQueryClient()
  const base = `/nodes/${nodeId}/projects/${encodeURIComponent(project)}`
  const [taskId, setTaskId] = useState<string | null>(null)
  const [showLogs, setShowLogs] = useState(false)

  const status = useQuery({
    queryKey: ['project-status', nodeId, project],
    queryFn: () => api.get<{ services: Service[] }>(`${base}/status`),
  })
  const task = useQuery({
    queryKey: ['project-task', nodeId, taskId],
    enabled: taskId !== null,
    queryFn: () => api.get<Task>(`/nodes/${nodeId}/tasks/${taskId}`),
    refetchInterval: (query) => (query.state.data?.state === 'running' ? 2000 : false),
  })
  const logs = useQuery({
    queryKey: ['project-logs', nodeId, project],
    enabled: showLogs,
    queryFn: () => api.get<{ output: string }>(`${base}/logs?tail=200`),
  })
  const run = useMutation({
    mutationFn: (action: string) => api.post<{ task_id: string }>(`${base}/actions`, { action }),
    onSuccess: (started) => setTaskId(started.task_id),
  })
  const ended = task.data && task.data.state !== 'running'
  if (ended) void queryClient.invalidateQueries({ queryKey: ['project-status', nodeId, project] })

  return (
    <section aria-label={t('projects:run.title', { project })}>
      <h3>{t('projects:run.title', { project })}</h3>
      <div className="actions">
        {ACTIONS.map((action) => (
          <button
            key={action}
            type="button"
            disabled={run.isPending || task.data?.state === 'running'}
            onClick={() => run.mutate(action)}
          >
            {t(`projects:run.action.${action}`)}
          </button>
        ))}
        <button type="button" onClick={() => setShowLogs((v) => !v)}>
          {t(showLogs ? 'projects:run.hideLogs' : 'projects:run.showLogs')}
        </button>
      </div>
      {run.isError && (
        <p role="alert" className="error">
          {errorMessage(t, run.error)}
        </p>
      )}
      {task.data && (
        <div role="status">
          <p>{t(`projects:run.state.${task.data.state}`, { action: task.data.action })}</p>
          {task.data.output && <pre className="output">{task.data.output}</pre>}
        </div>
      )}
      {task.isError && (
        <p role="alert" className="error">
          {errorMessage(t, task.error)}
        </p>
      )}
      <h4>{t('projects:run.services')}</h4>
      {status.isError ? (
        <p role="alert" className="error">
          {errorMessage(t, status.error)}
        </p>
      ) : status.data && status.data.services.length > 0 ? (
        <ul className="links">
          {status.data.services.map((s) => (
            <li key={s.name}>
              <strong>{s.service}</strong> — {s.state} ({s.status}) <code>{s.image}</code>
            </li>
          ))}
        </ul>
      ) : (
        status.data && <p className="empty">{t('projects:run.noServices')}</p>
      )}
      {showLogs && (
        <>
          <h4>{t('projects:run.logs')}</h4>
          {logs.isError ? (
            <p role="alert" className="error">
              {errorMessage(t, logs.error)}
            </p>
          ) : (
            <pre className="output">{logs.data?.output ?? t('loading')}</pre>
          )}
        </>
      )}
    </section>
  )
}
