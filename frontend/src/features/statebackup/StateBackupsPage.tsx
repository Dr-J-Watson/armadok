import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, downloadWith } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Backup {
  name: string
  stamp: string
  size: number
}

interface Answer {
  backups: Backup[]
  keep: number
  every_hours: number
  ready: boolean
  custom_folder: boolean
}

const stamp = (s: string) => new Date(`${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}T${s.slice(9, 11)}:${s.slice(11, 13)}:${s.slice(13, 15)}Z`).toLocaleString()
const size = (n: number) => (n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KiB` : `${(n / 1048576).toFixed(1)} MiB`)

// Backups of Armadok's own data. They are encrypted with the encryption key, which is the one
// thing they cannot contain: keep it somewhere else. Restoring is a command, not a button.
export function StateBackupsPage() {
  const { t } = useTranslation(['statebackup', 'errors'])
  const client = useQueryClient()
  const [downloading, setDownloading] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const list = useQuery({ queryKey: ['statebackups'], queryFn: () => api.get<Answer>('/statebackups') })
  const refresh = () => client.invalidateQueries({ queryKey: ['statebackups'] })
  const create = useMutation({ mutationFn: () => api.post('/statebackups'), onSuccess: refresh })
  const remove = useMutation({ mutationFn: (b: Backup) => api.del(`/statebackups/${b.name}`), onSuccess: refresh })
  const fetchFile = useMutation({
    mutationFn: (name: string) => downloadWith(`/statebackups/${name}/download`, { code }, name),
    onSuccess: () => setDownloading(null),
    // A code is for one try: it is cleared whatever the outcome.
    onSettled: () => setCode(''),
  })
  const failed = list.error ?? create.error ?? remove.error ?? fetchFile.error
  const data = list.data

  return (
    <main>
      <h1>{t('statebackup:title')}</h1>
      <p>{t('statebackup:explain')}</p>
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
      {list.isPending && <p>{t('loading')}</p>}
      {data && !data.ready && <p className="notice">{t('statebackup:noKey')}</p>}
      {data && (
        <>
          <p>
            {data.every_hours > 0 ? t('statebackup:schedule', { hours: data.every_hours, keep: data.keep }) : t('statebackup:manualOnly', { keep: data.keep })}{' '}
            {!data.custom_folder && <span className="notice">{t('statebackup:sameDisk')}</span>}
          </p>
          <button type="button" disabled={!data.ready || create.isPending} onClick={() => create.mutate()}>
            {t('statebackup:now')}
          </button>
          {data.backups.length === 0 && <p className="empty">{t('statebackup:none')}</p>}
          <ul className="links">
            {data.backups.map((b) => (
              <li key={b.name}>
                {stamp(b.stamp)} — {size(b.size)}{' '}
                <button type="button" onClick={() => {
                    setCode('')
                    setDownloading(b.name)
                  }}>
                  {t('statebackup:download')}
                </button>{' '}
                <button type="button" onClick={() => window.confirm(t('statebackup:confirmDelete')) && remove.mutate(b)}>
                  {t('statebackup:delete')}
                </button>
                {downloading === b.name && (
                  <form
                    aria-label={t('statebackup:download')}
                    onSubmit={(e) => {
                      e.preventDefault()
                      fetchFile.mutate(b.name)
                    }}
                  >
                    <label>
                      {t('statebackup:code')}
                      <input value={code} inputMode="numeric" autoComplete="one-time-code" required onChange={(e) => setCode(e.target.value)} />
                    </label>
                    <button type="submit" disabled={fetchFile.isPending}>
                      {t('statebackup:download')}
                    </button>
                  </form>
                )}
              </li>
            ))}
          </ul>
          <h2>{t('statebackup:restoreTitle')}</h2>
          <p>{t('statebackup:restoreHow')}</p>
          <pre className="output">python -m armadok.restore_state /path/to/armadok-state-….tar.gz.enc</pre>
        </>
      )}
    </main>
  )
}
