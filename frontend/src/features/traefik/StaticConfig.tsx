import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { lineDiff } from './lineDiff'

interface StaticFile {
  configured: boolean
  name?: string
  content?: string
  sha256?: string
}

interface Version {
  version: string
  at: number
}

interface Container {
  id: string
  names: string[]
  image: string
}

// Traefik's static configuration. Changing it does nothing until Traefik restarts, so after a
// save the person is offered the restart, with a confirmation, on the container they choose.
export function StaticConfig({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/traefik/static`
  const key = ['traefik-static', nodeId]
  const loaded = useQuery({ queryKey: key, queryFn: () => api.get<StaticFile>(base), staleTime: Infinity })
  const versions = useQuery({
    queryKey: [...key, 'history'],
    enabled: loaded.data?.configured === true,
    queryFn: () => api.get<{ versions: Version[] }>(`${base}/history`),
  })
  const [draft, setDraft] = useState<string | null>(null)
  const [reviewing, setReviewing] = useState(false)
  const [saved, setSaved] = useState(false)

  const current = loaded.data?.content ?? ''
  const text = draft ?? current
  const dirty = draft !== null && draft !== current
  const diff = reviewing && dirty ? lineDiff(current, text) : null

  const save = useMutation({
    mutationFn: () => api.put(base, { content: text, expected_sha256: loaded.data?.sha256 }),
    onSuccess: async () => {
      setSaved(true)
      setReviewing(false)
      setDraft(null)
      await client.invalidateQueries({ queryKey: key })
    },
  })
  const open = useMutation({
    mutationFn: (version: string) => api.get<{ content: string }>(`${base}/history/${version}`),
    onSuccess: (old) => {
      setDraft(old.content)
      setReviewing(true)
      setSaved(false)
    },
  })
  const failed = loaded.error ?? save.error ?? open.error

  if (loaded.isPending) return <p>{t('loading')}</p>
  if (failed) {
    return (
      <p role="alert" className="error">
        {errorMessage(t, failed)}
      </p>
    )
  }
  if (!loaded.data?.configured) {
    return (
      <section aria-label={t('traefik:static.title')}>
        <h2>{t('traefik:static.title')}</h2>
        <p className="notice">{t('traefik:static.notConfigured')}</p>
      </section>
    )
  }
  return (
    <section aria-label={t('traefik:static.title')}>
      <h2>{t('traefik:static.title')}</h2>
      <p className="hint">{t('traefik:static.explain')}</p>
      <label>
        {t('traefik:static.text', { name: loaded.data.name })}
        <textarea
          className="editor"
          rows={20}
          spellCheck={false}
          value={text}
          onChange={(e) => {
            setDraft(e.target.value)
            setReviewing(false)
            setSaved(false)
          }}
        />
      </label>
      <div className="actions">
        <button type="button" disabled={!dirty} onClick={() => setReviewing(true)}>
          {t('traefik:static.review')}
        </button>
      </div>
      {reviewing && dirty && (
        <div className="notice">
          {diff ? (
            <pre aria-label={t('traefik:static.changes')}>
              {diff
                .filter((l) => l.kind !== 'same')
                .map((l, i) => (
                  <span key={i} className={l.kind === 'add' ? 'diff-add' : 'diff-del'}>
                    {l.kind === 'add' ? '+ ' : '- '}
                    {l.text}
                    {'\n'}
                  </span>
                ))}
            </pre>
          ) : (
            <p>{t('traefik:static.tooLong')}</p>
          )}
          <button type="button" className="primary" disabled={save.isPending} onClick={() => save.mutate()}>
            {t('traefik:static.save')}
          </button>
        </div>
      )}
      {saved && <Restart nodeId={nodeId} />}
      {versions.data && versions.data.versions.length > 0 && (
        <details>
          <summary>{t('traefik:static.history')}</summary>
          <ul className="links">
            {versions.data.versions.map((v) => (
              <li key={v.version}>
                {new Date(v.at).toLocaleString()}{' '}
                <button type="button" onClick={() => open.mutate(v.version)}>
                  {t('traefik:static.load')}
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

// The restart that makes the saved file count: the person names the Traefik container.
function Restart({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const containers = useQuery({
    queryKey: ['docker-containers', nodeId],
    queryFn: () => api.get<{ containers: Container[] }>(`/nodes/${nodeId}/docker/containers`),
  })
  const [chosen, setChosen] = useState('')
  const [done, setDone] = useState(false)
  const restart = useMutation({
    mutationFn: () => api.post(`/nodes/${nodeId}/docker/containers/${chosen}/action`, { action: 'restart' }),
    onSuccess: () => setDone(true),
  })
  const all = containers.data?.containers ?? []
  // Likely candidates first: anything that looks like Traefik.
  const sorted = [...all].sort((x, y) => Number(/traefik/i.test(y.image + y.names.join())) - Number(/traefik/i.test(x.image + x.names.join())))
  return (
    <div className="notice" role="group" aria-label={t('traefik:static.restartTitle')}>
      <p>{t('traefik:static.saved')}</p>
      <label>
        {t('traefik:static.which')}
        <select value={chosen} onChange={(e) => setChosen(e.target.value)}>
          <option value="">—</option>
          {sorted.map((c) => (
            <option key={c.id} value={c.id}>
              {c.names[0]?.replace(/^\//, '') ?? c.id.slice(0, 12)} ({c.image})
            </option>
          ))}
        </select>
      </label>{' '}
      <button
        type="button"
        disabled={chosen === '' || restart.isPending}
        onClick={() => window.confirm(t('traefik:static.confirmRestart')) && restart.mutate()}
      >
        {t('traefik:static.restart')}
      </button>
      {done && <p role="status">{t('traefik:static.restarted')}</p>}
      {restart.isError && (
        <p role="alert" className="error">
          {errorMessage(t, restart.error)}
        </p>
      )}
    </div>
  )
}
