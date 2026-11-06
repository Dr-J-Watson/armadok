import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import { codeMessage } from '../../core/errors'
import { useConfirm } from '../../components/Confirm'

interface Info {
  source: { url: string; branch: string } | null
  status: { commit: string; subject: string; dirty: boolean; remote: string } | null
  error: string | null
}

// Where a project came from, whether its branch has moved on, and the buttons to follow it.
// Updating replaces the project's files; deploying stays a deliberate second step (or the
// "update and deploy" button, which does both).
export function GitPanel({ nodeId, project }: { nodeId: string; project: string }) {
  const { t } = useTranslation(['git', 'errors'])
  const client = useQueryClient()
  const ask = useConfirm()
  const base = `/nodes/${nodeId}/git/projects/${encodeURIComponent(project)}`
  const key = ['git-source', nodeId, project]
  const info = useQuery({ queryKey: key, queryFn: () => api.get<Info>(base) })
  const update = useMutation({
    mutationFn: async (deploy: boolean) => {
      await api.post(`${base}/update`)
      if (deploy) await api.post(`/nodes/${nodeId}/projects/${encodeURIComponent(project)}/actions`, { action: 'up' })
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: key })
      void client.invalidateQueries({ queryKey: ['project-file', nodeId, project] })
    },
  })
  const forget = useMutation({
    mutationFn: () => api.del(base),
    onSuccess: () => client.invalidateQueries({ queryKey: key }),
  })
  if (!info.data?.source) return info.isError ? <Problem error={info.error} /> : null
  const { source, status, error } = info.data
  const behind = status !== null && status.commit !== status.remote
  return (
    <section className="panel" aria-label={t('git:panel.title')}>
      <h3>{t('git:panel.title')}</h3>
      <p>
        <code>{source.url}</code> · <code>{source.branch}</code>
        {status && (
          <>
            {' '}
            · <code>{status.commit.slice(0, 7)}</code> <small>{status.subject}</small>
          </>
        )}
      </p>
      {error && <p className="notice">{codeMessage(t, error)}</p>}
      {status && (
        <p>
          <span className={`badge ${behind ? 'danger-badge' : 'ok'}`}>{behind ? t('git:panel.behind') : t('git:panel.current')}</span>
          {status.dirty && <span className="notice"> {t('git:panel.dirty')}</span>}
        </p>
      )}
      <div className="actions">
        <button type="button" title={t('git:panel.checkTip')} onClick={() => info.refetch()} disabled={info.isFetching}>
          {t('git:panel.check')}
        </button>
        <button type="button" title={t('git:panel.updateTip')} onClick={() => update.mutate(false)} disabled={update.isPending || !status}>
          {t('git:panel.update')}
        </button>
        <button type="button" className="primary" title={t('git:panel.updateDeployTip')} onClick={() => update.mutate(true)} disabled={update.isPending || !status}>
          {t('git:panel.updateDeploy')}
        </button>
        <button
          type="button"
          className="danger-button"
          title={t('git:panel.forgetTip')}
          onClick={async () => {
            if (await ask(t('git:panel.forgetConfirm'))) forget.mutate()
          }}
          disabled={forget.isPending}
        >
          {t('git:panel.forget')}
        </button>
      </div>
      {update.isSuccess && <p className="hint">{t('git:panel.updated')}</p>}
      {(update.isError || forget.isError) && <Problem error={update.error ?? forget.error} />}
    </section>
  )
}
