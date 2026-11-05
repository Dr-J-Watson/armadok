import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { ApiError, api } from '../../core/api'

interface Login {
  host: string
  username: string
}

// Logins for private registries, used only to ask them about this machine's images. The password
// is sent once and never shown again. Hidden for people who may not manage them.
export function RegistryLogins({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['imageupdates', 'errors'])
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/image-updates/registries`
  const key = ['registry-logins', nodeId]
  const [host, setHost] = useState('')
  const [username, setUsername] = useState('')
  const [secret, setSecret] = useState('')
  const list = useQuery({
    queryKey: key,
    queryFn: () => api.get<{ registries: Login[] }>(base),
    retry: false,
  })
  const save = useMutation({
    mutationFn: () => api.put(base, { host, username, secret }),
    onSuccess: () => {
      setHost('')
      setUsername('')
      setSecret('')
      return client.invalidateQueries({ queryKey: key })
    },
  })
  const forget = useMutation({
    mutationFn: (h: string) => api.del(`${base}/${encodeURIComponent(h)}`),
    onSuccess: () => client.invalidateQueries({ queryKey: key }),
  })
  if (list.isError && list.error instanceof ApiError && [403, 404].includes(list.error.status)) return null
  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate()
  }
  return (
    <details className="panel">
      <summary>{t('imageupdates:logins.title')}</summary>
      <p className="hint">{t('imageupdates:logins.explain')}</p>
      {list.isError && <Problem error={list.error} />}
      <ul className="links">
        {(list.data?.registries ?? []).map((l) => (
          <li key={l.host}>
            <strong>{l.host}</strong> <small>{l.username}</small>{' '}
            <button type="button" onClick={() => forget.mutate(l.host)} disabled={forget.isPending}>
              {t('imageupdates:logins.forget')}
            </button>
          </li>
        ))}
      </ul>
      <form onSubmit={submit} autoComplete="off">
        <label>
          {t('imageupdates:logins.host')}
          <input value={host} onChange={(e) => setHost(e.target.value)} placeholder="registry.example.com" required />
        </label>
        <label>
          {t('imageupdates:logins.username')}
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" required />
        </label>
        <label>
          {t('imageupdates:logins.secret')}
          <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="new-password" required />
        </label>
        <div className="actions">
          <button type="submit" className="primary" disabled={save.isPending}>
            {t('imageupdates:logins.save')}
          </button>
        </div>
      </form>
      {(save.isError || forget.isError) && <Problem error={save.error ?? forget.error} />}
    </details>
  )
}
