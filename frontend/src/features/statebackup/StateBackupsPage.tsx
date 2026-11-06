import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, downloadWith } from '../../core/api'
import { Problem } from '../../components/Problem'
import { useConfirm } from '../../components/Confirm'

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
  separate_key: boolean
  custom_folder: boolean
}

const stamp = (s: string) => new Date(`${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}T${s.slice(9, 11)}:${s.slice(11, 13)}:${s.slice(13, 15)}Z`).toLocaleString()
const size = (n: number) => (n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KiB` : `${(n / 1048576).toFixed(1)} MiB`)

// Backups of Armadok's own data. They are encrypted with the encryption key, which is the one
// thing they cannot contain: keep it somewhere else. Restoring is a command, not a button.
export function StateBackupsPage() {
  const { t } = useTranslation(['statebackup', 'errors'])
  const confirm = useConfirm()
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
  // The new key is shown once, here, and kept only in this component's memory.
  const [newKey, setNewKey] = useState<string | null>(null)
  const makeKey = useMutation({ mutationFn: () => api.post<{ key: string }>('/statebackups/new-key'), onSuccess: (a) => setNewKey(a.key) })
  const failed = list.error ?? create.error ?? remove.error ?? fetchFile.error ?? makeKey.error
  const data = list.data

  return (
    <main>
      <header className="page-head page-head-split">
        <div>
          <h1>{t('statebackup:title')}</h1>
          <p className="hint">{t('statebackup:explain')}</p>
        </div>
        {data && (
          <button type="button" className="primary" disabled={!data.ready || create.isPending} title={t('statebackup:nowTip')} onClick={() => create.mutate()}>
            {t('statebackup:now')}
          </button>
        )}
      </header>
      {failed != null && (
        <Problem error={failed} />
      )}
      {list.isPending && <p className="loading">{t('loading')}</p>}
      {data && !data.ready && <p className="notice">{t('statebackup:noKey')}</p>}
      {data && (
        <>
          <p>
            {data.every_hours > 0 ? t('statebackup:schedule', { hours: data.every_hours, keep: data.keep }) : t('statebackup:manualOnly', { keep: data.keep })}{' '}
            {!data.custom_folder && <span className="notice">{t('statebackup:sameDisk')}</span>}
          </p>
          <p className={data.separate_key ? '' : 'notice'}>{data.separate_key ? t('statebackup:ownKey') : t('statebackup:sharedKey')}</p>
          {!data.separate_key && (
            <>
              <button type="button" disabled={makeKey.isPending} onClick={() => makeKey.mutate()}>
                {t('statebackup:makeKey')}
              </button>
              {newKey && (
                <div role="status" className="notice">
                  <p>{t('statebackup:keyShown')}</p>
                  <p>
                    <code>{newKey}</code>
                  </p>
                  <p>
                    <code>ARMADOK_STATE_BACKUP_KEY={newKey}</code>
                  </p>
                  <button type="button" onClick={() => setNewKey(null)}>
                    {t('statebackup:keySaved')}
                  </button>
                </div>
              )}
            </>
          )}
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
                <button type="button" className="danger-button" onClick={async () => (await confirm(t('statebackup:confirmDelete'))) && remove.mutate(b)}>
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
