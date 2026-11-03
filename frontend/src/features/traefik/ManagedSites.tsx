import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { SiteForm } from './SiteForm'
import type { SiteSpec } from './SiteForm'

interface Managed {
  name: string
  paused: boolean
  sha256: string
  spec: SiteSpec | null
  modified_by_hand: boolean
}

interface Version {
  version: string
  at: number
  size: number
}

// The sites built with the form: pause, resume, rename, delete, edit, and put back an older
// version. Each button only calls the API; the server decides what is allowed.
export function ManagedSites({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const queryClient = useQueryClient()
  const base = `/nodes/${nodeId}/traefik`
  const [form, setForm] = useState<Managed | 'new' | null>(null)
  const [renaming, setRenaming] = useState<{ name: string; to: string } | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [historyOf, setHistoryOf] = useState<string | null>(null)

  const list = useQuery({
    queryKey: ['traefik-managed', nodeId],
    queryFn: () => api.get<{ sites: Managed[] }>(`${base}/managed`),
  })
  const versions = useQuery({
    queryKey: ['traefik-history', nodeId, historyOf],
    enabled: historyOf !== null,
    queryFn: () => api.get<{ versions: Version[] }>(`${base}/history/sites/${historyOf}.yml`),
  })
  const refresh = async () => {
    setConfirmDelete(null)
    setRenaming(null)
    await queryClient.invalidateQueries({ queryKey: ['traefik-managed', nodeId] })
    await queryClient.invalidateQueries({ queryKey: ['traefik-sites', nodeId] })
    await queryClient.invalidateQueries({ queryKey: ['traefik-history', nodeId] })
  }
  const act = useMutation({
    mutationFn: (run: () => Promise<unknown>) => run(),
    onSuccess: refresh,
  })

  if (form) {
    return (
      <SiteForm
        key={form === 'new' ? 'new' : form.name}
        nodeId={nodeId}
        initial={form === 'new' ? undefined : (form.spec ?? undefined)}
        expectedSha256={form === 'new' ? undefined : form.sha256}
        onDone={() => setForm(null)}
      />
    )
  }
  if (list.isPending) return <p>{t('loading')}</p>
  if (list.isError)
    return (
      <p role="alert" className="error">
        {errorMessage(t, list.error)}
      </p>
    )
  return (
    <>
      <div className="actions">
        <button type="button" className="primary" onClick={() => setForm('new')}>
          {t('traefik:managed.new')}
        </button>
      </div>
      {act.isError && (
        <p role="alert" className="error">
          {errorMessage(t, act.error)}
        </p>
      )}
      {list.data.sites.length === 0 && <p className="empty">{t('traefik:managed.none')}</p>}
      <ul className="links">
        {list.data.sites.map((s) => (
          <li key={s.name}>
            <strong>{s.name}</strong>{' '}
            <span className={`badge ${s.paused ? 'off' : 'ok'}`}>
              {t(s.paused ? 'traefik:managed.paused' : 'traefik:managed.active')}
            </span>{' '}
            {s.spec && <small>{s.spec.hosts.join(', ')}</small>}
            {s.modified_by_hand && <small> — {t('traefik:managed.byHand')}</small>}
            <div className="actions">
              {s.paused ? (
                <button type="button" onClick={() => act.mutate(() => api.post(`${base}/sites/${s.name}/resume`))}>
                  {t('traefik:managed.resume')}
                </button>
              ) : (
                <button type="button" onClick={() => act.mutate(() => api.post(`${base}/sites/${s.name}/pause`))}>
                  {t('traefik:managed.pause')}
                </button>
              )}
              {!s.paused && !s.modified_by_hand && (
                <>
                  <button type="button" onClick={() => setForm(s)}>
                    {t('traefik:managed.edit')}
                  </button>
                  <button type="button" onClick={() => setRenaming({ name: s.name, to: '' })}>
                    {t('traefik:managed.rename')}
                  </button>
                </>
              )}
              <button type="button" onClick={() => setHistoryOf(historyOf === s.name ? null : s.name)}>
                {t('traefik:managed.history')}
              </button>
              {confirmDelete === s.name ? (
                <button
                  type="button"
                  className="danger"
                  onClick={() => act.mutate(() => api.del(`${base}/sites/${s.name}?expected_sha256=${s.sha256}`))}
                >
                  {t('traefik:managed.confirmDelete')}
                </button>
              ) : (
                <button type="button" onClick={() => setConfirmDelete(s.name)}>
                  {t('traefik:managed.delete')}
                </button>
              )}
            </div>
            {renaming?.name === s.name && (
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  act.mutate(() =>
                    api.post(`${base}/sites/${s.name}/rename`, { new_name: renaming.to, expected_sha256: s.sha256 }),
                  )
                }}
              >
                <label>
                  {t('traefik:managed.newName')}
                  <input
                    value={renaming.to}
                    required
                    pattern="[a-z0-9][a-z0-9\-]{0,39}"
                    onChange={(e) => setRenaming({ name: s.name, to: e.target.value })}
                  />
                </label>
                <button type="submit">{t('traefik:managed.rename')}</button>
              </form>
            )}
            {historyOf === s.name && (
              <div>
                {versions.isError ? (
                  <p role="alert" className="error">
                    {errorMessage(t, versions.error)}
                  </p>
                ) : versions.data && versions.data.versions.length === 0 ? (
                  <p className="empty">{t('traefik:managed.noHistory')}</p>
                ) : (
                  <ul className="links">
                    {versions.data?.versions.map((v) => (
                      <li key={v.version}>
                        {new Date(v.at).toLocaleString()}{' '}
                        <button
                          type="button"
                          onClick={() =>
                            act.mutate(() =>
                              api.post(`${base}/restore/sites/${s.name}.yml`, {
                                version: v.version,
                                expected_sha256: s.sha256,
                              }),
                            )
                          }
                        >
                          {t('traefik:managed.restore')}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  )
}
