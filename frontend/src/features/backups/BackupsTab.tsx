import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Archive {
  name: string
  volume: string
  stamp: string
  size: number
}

interface Job {
  id: string
  kind: 'backup' | 'restore'
  volume: string
  archive: string | null
  size: number | null
  state: 'running' | 'success' | 'failed'
  error: string | null
  created_at: string
}

interface Answer {
  configured: boolean
  archives: Archive[]
  jobs: Job[]
  can_create: boolean
  can_manage: boolean
  default_keep: number
  truncated: boolean
}

// 20260101T031500Z -> a readable local date
const stamp = (s: string) => new Date(`${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}T${s.slice(9, 11)}:${s.slice(11, 13)}:${s.slice(13, 15)}Z`).toLocaleString()
const megabytes = (n: number) => (n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KiB` : `${(n / 1048576).toFixed(1)} MiB`)

// Archives are made and kept on the machine itself; this screen only starts, lists and removes
// them. A restore needs the volume's name typed again, because it overwrites files.
export function BackupsTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['backups', 'errors'])
  const client = useQueryClient()
  const [volume, setVolume] = useState('')
  const [keep, setKeep] = useState('7')
  const [restoring, setRestoring] = useState<Archive | null>(null)
  const [target, setTarget] = useState('')
  const [typed, setTyped] = useState('')
  const base = `/nodes/${nodeId}/backups`
  const list = useQuery({
    queryKey: ['backups', nodeId],
    queryFn: () => api.get<Answer>(base),
    // A running job ends on its own; follow it without waiting for a signal.
    refetchInterval: (q) => (q.state.data?.jobs.some((j) => j.state === 'running') ? 5000 : false),
  })
  const volumes = useQuery({
    queryKey: ['docker-resources', nodeId, 'volumes'],
    queryFn: () => api.get<{ items: { name: string; driver: string }[] }>(`/nodes/${nodeId}/docker/volumes`),
    enabled: list.data?.can_create === true,
    retry: false,
  })
  const refresh = () => client.invalidateQueries({ queryKey: ['backups', nodeId] })
  const backup = useMutation({
    mutationFn: () => api.post(`${base}/volumes/${encodeURIComponent(volume)}`, { keep: Number(list.data?.can_manage ? keep : (list.data?.default_keep ?? 7)) }),
    onSuccess: refresh,
  })
  const restore = useMutation({
    mutationFn: () => api.post(`${base}/restore`, { archive: restoring!.name, volume: target, confirm: typed, other: target !== restoring!.volume }),
    onSuccess: () => {
      setRestoring(null)
      setTyped('')
      refresh()
    },
  })
  const remove = useMutation({
    mutationFn: (a: Archive) => api.del(`${base}/archives/${encodeURIComponent(a.name)}`),
    onSuccess: refresh,
  })

  const failed = list.error ?? backup.error ?? restore.error ?? remove.error
  const data = list.data
  function submitBackup(event: FormEvent) {
    event.preventDefault()
    backup.mutate()
  }
  function submitRestore(event: FormEvent) {
    event.preventDefault()
    restore.mutate()
  }

  return (
    <>
      <p>{t('backups:explain')}</p>
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
      {list.isPending && <p>{t('loading')}</p>}
      {data && !data.configured && <p className="notice">{t('backups:notConfigured')}</p>}
      {data?.configured && (
        <>
          {data.can_create && (
            <form onSubmit={submitBackup} aria-label={t('backups:new')}>
              <h2>{t('backups:new')}</h2>
              <label>
                {t('backups:volume')}
                <select value={volume} required onChange={(e) => setVolume(e.target.value)}>
                  <option value="" />
                  {(volumes.data?.items ?? [])
                    .filter((v) => v.driver === 'local')
                    .map((v) => (
                      <option key={v.name} value={v.name}>
                        {v.name}
                      </option>
                    ))}
                </select>
              </label>
              {data.can_manage && (
                <label>
                  {t('backups:keep')}
                  <input type="number" min={1} max={365} value={keep} required onChange={(e) => setKeep(e.target.value)} />
                </label>
              )}
              <button type="submit" disabled={backup.isPending}>
                {t('backups:start')}
              </button>
            </form>
          )}
          <h2>{t('backups:archives')}</h2>
          {data.truncated && <p className="notice">{t('backups:truncated')}</p>}
          {data.archives.length === 0 && <p className="empty">{t('backups:none')}</p>}
          <ul className="links">
            {data.archives.map((a) => (
              <li key={a.name}>
                <strong>{a.volume}</strong> — {stamp(a.stamp)} — {megabytes(a.size)}
                {data.can_manage && (
                  <span>
                    {' '}
                    <button
                      type="button"
                      onClick={() => {
                        setRestoring(a)
                        setTarget(a.volume)
                        setTyped('')
                      }}
                    >
                      {t('backups:restore')}
                    </button>{' '}
                    <button type="button" onClick={() => window.confirm(t('backups:confirmDelete')) && remove.mutate(a)}>
                      {t('backups:delete')}
                    </button>
                  </span>
                )}
              </li>
            ))}
          </ul>
          {restoring && (
            <form onSubmit={submitRestore} aria-label={t('backups:restoreTitle')} className="notice">
              <h2>{t('backups:restoreTitle')}</h2>
              <p>{t('backups:restoreWarn', { archive: restoring.name })}</p>
              <label>
                {t('backups:restoreInto')}
                <input value={target} required pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}" onChange={(e) => setTarget(e.target.value)} />
              </label>
              <label>
                {t('backups:typeName')}
                <input value={typed} required autoComplete="off" onChange={(e) => setTyped(e.target.value)} />
              </label>
              <button type="submit" disabled={restore.isPending || typed !== target}>
                {t('backups:restore')}
              </button>{' '}
              <button type="button" onClick={() => setRestoring(null)}>
                {t('backups:cancel')}
              </button>
            </form>
          )}
        </>
      )}
      {data && data.jobs.length > 0 && (
        <>
          <h2>{t('backups:jobs')}</h2>
          <ul className="links">
            {data.jobs.map((j) => (
              <li key={j.id}>
                {t(`backups:kind.${j.kind}`)} <strong>{j.volume}</strong>{' '}
                <span className={`badge ${j.state === 'success' ? 'ok' : j.state === 'failed' ? 'danger-badge' : 'off'}`}>{t(`backups:state.${j.state}`)}</span>
                {j.error && <span className="error"> — {t(`errors:${j.error}`, { defaultValue: j.error })}</span>}
                <small> {new Date(j.created_at).toLocaleString()}</small>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  )
}
