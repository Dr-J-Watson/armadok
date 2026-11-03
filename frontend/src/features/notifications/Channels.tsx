import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { codeMessage, errorMessage } from '../../core/errors'

export interface Channel {
  id: string
  kind: 'webhook' | 'ntfy' | 'discord'
  name: string
  last_status: string | null
}

const KINDS = ['ntfy', 'webhook', 'discord'] as const

export function Channels() {
  const { t } = useTranslation(['notifications', 'errors'])
  const queryClient = useQueryClient()
  const channels = useQuery({ queryKey: ['channels'], queryFn: () => api.get<Channel[]>('/notifications/channels') })
  const [kind, setKind] = useState<(typeof KINDS)[number]>('ntfy')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [topic, setTopic] = useState('')
  const [token, setToken] = useState('')
  // A webhook's signing secret is shown once, here, and kept only in this component's memory.
  const [secret, setSecret] = useState<string | null>(null)
  const [tested, setTested] = useState<string | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['channels'] })
  const add = useMutation({
    mutationFn: () => {
      const settings: Record<string, string> =
        kind === 'ntfy' ? { topic, ...(url ? { server: url } : {}), ...(token ? { token } : {}) } : { url }
      return api.post<{ id: string; secret: string | null }>('/notifications/channels', { kind, name, settings })
    },
    onSuccess: (data) => {
      setSecret(data.secret)
      setName('')
      setUrl('')
      setTopic('')
      setToken('')
      return refresh()
    },
  })
  const remove = useMutation({ mutationFn: (id: string) => api.del(`/notifications/channels/${id}`), onSuccess: refresh })
  const test = useMutation({
    mutationFn: (id: string) => api.post(`/notifications/channels/${id}/test`),
    onSuccess: (_, id) => {
      setTested(id)
      return refresh()
    },
    onError: () => refresh(),
  })
  const failed = channels.error ?? add.error ?? remove.error ?? test.error

  function submit(event: FormEvent) {
    event.preventDefault()
    add.mutate()
  }

  return (
    <>
      <p>{t('notifications:channels.explain')}</p>
      {failed != null && <p role="alert" className="error">{errorMessage(t, failed)}</p>}
      {secret && (
        <div className="notice">
          <p>{t('notifications:channels.secret')}</p>
          <pre>{secret}</pre>
          <button type="button" onClick={() => setSecret(null)}>{t('notifications:channels.secretSaved')}</button>
        </div>
      )}
      {channels.data?.length === 0 && <p className="empty">{t('notifications:channels.empty')}</p>}
      <ul className="cards">
        {channels.data?.map((c) => (
          <li key={c.id}>
            <strong>{c.name}</strong> <span className="badge">{t(`notifications:kinds.${c.kind}`)}</span>
            <p className="hint">
              {c.last_status === null
                ? t('notifications:channels.neverUsed')
                : c.last_status === 'ok'
                  ? t('notifications:channels.ok')
                  : codeMessage(t, c.last_status)}
            </p>
            {tested === c.id && !test.isError && <p className="ok-text" role="status">{t('notifications:channels.tested')}</p>}
            <button type="button" onClick={() => test.mutate(c.id)}>{t('notifications:channels.test')}</button>{' '}
            <button type="button" onClick={() => remove.mutate(c.id)}>{t('notifications:channels.remove')}</button>
          </li>
        ))}
      </ul>

      <h3>{t('notifications:channels.add')}</h3>
      <form className="form" onSubmit={submit}>
        <label htmlFor="ch-kind">{t('notifications:channels.kind')}</label>
        <select id="ch-kind" value={kind} onChange={(e) => setKind(e.target.value as (typeof KINDS)[number])}>
          {KINDS.map((k) => <option key={k} value={k}>{t(`notifications:kinds.${k}`)}</option>)}
        </select>
        <label htmlFor="ch-name">{t('notifications:channels.name')}</label>
        <input id="ch-name" required maxLength={60} value={name} onChange={(e) => setName(e.target.value)} />
        {kind === 'ntfy' ? (
          <>
            <label htmlFor="ch-topic">{t('notifications:channels.topic')}</label>
            <input id="ch-topic" required pattern="[A-Za-z0-9_\-]{1,64}" value={topic} onChange={(e) => setTopic(e.target.value)} />
            <label htmlFor="ch-server">{t('notifications:channels.server')}</label>
            <input id="ch-server" type="url" placeholder="https://ntfy.sh" value={url} onChange={(e) => setUrl(e.target.value)} />
            <label htmlFor="ch-token">{t('notifications:channels.token')}</label>
            <input id="ch-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
          </>
        ) : (
          <>
            <label htmlFor="ch-url">{t('notifications:channels.url')}</label>
            <input id="ch-url" type="url" required value={url} onChange={(e) => setUrl(e.target.value)} />
          </>
        )}
        <p className="hint">{t('notifications:channels.private')}</p>
        <button type="submit" className="primary" disabled={add.isPending}>{t('notifications:channels.create')}</button>
      </form>
    </>
  )
}
