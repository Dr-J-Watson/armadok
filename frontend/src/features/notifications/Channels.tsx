import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { codeMessage, errorMessage } from '../../core/errors'

export interface Channel {
  id: string
  kind: 'webhook' | 'ntfy' | 'discord' | 'email'
  name: string
  last_status: string | null
  confirmed: boolean
}

const KINDS = ['ntfy', 'webhook', 'discord', 'email'] as const

export function Channels() {
  const { t } = useTranslation(['notifications', 'errors'])
  const queryClient = useQueryClient()
  const channels = useQuery({ queryKey: ['channels'], queryFn: () => api.get<Channel[]>('/notifications/channels') })
  const platform = useQuery({
    queryKey: ['notification-platform'],
    queryFn: () => api.get<{ email_enabled: boolean; can_change: boolean }>('/notifications/platform'),
  })
  const emailOn = platform.data?.email_enabled ?? true
  const kinds = KINDS.filter((k) => k !== 'email' || emailOn)
  const switchEmail = useMutation({
    mutationFn: (enabled: boolean) => api.put('/notifications/platform', { email_enabled: enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notification-platform'] }),
  })
  const [kind, setKind] = useState<(typeof KINDS)[number]>('ntfy')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [topic, setTopic] = useState('')
  const [token, setToken] = useState('')
  const [mail, setMail] = useState({ host: '', port: '587', security: 'starttls', username: '', password: '', sender: '', recipient: '' })
  // A webhook's signing secret is shown once, here, and kept only in this component's memory.
  const [secret, setSecret] = useState<string | null>(null)
  const [tested, setTested] = useState<string | null>(null)
  // The code a mailbox received, typed per channel until it is confirmed.
  const [codes, setCodes] = useState<Record<string, string>>({})

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['channels'] })
  const add = useMutation({
    mutationFn: () => {
      const settings: Record<string, string | number> =
        kind === 'ntfy'
          ? { topic, ...(url ? { server: url } : {}), ...(token ? { token } : {}) }
          : kind === 'email'
            ? { ...mail, port: Number(mail.port) }
            : { url }
      return api.post<{ id: string; secret: string | null }>('/notifications/channels', { kind, name, settings })
    },
    onSuccess: (data) => {
      setSecret(data.secret)
      setName('')
      setUrl('')
      setTopic('')
      setToken('')
      setMail({ host: '', port: '587', security: 'starttls', username: '', password: '', sender: '', recipient: '' })
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
  const confirm = useMutation({
    mutationFn: (id: string) => api.post(`/notifications/channels/${id}/confirm`, { code: codes[id] ?? '' }),
    onSuccess: refresh,
  })
  const failed = channels.error ?? add.error ?? remove.error ?? test.error ?? confirm.error

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
            {c.confirmed ? (
              <>
                <button type="button" onClick={() => test.mutate(c.id)}>{t('notifications:channels.test')}</button>{' '}
              </>
            ) : (
              <form
                className="notice"
                aria-label={t('notifications:channels.confirmTitle')}
                onSubmit={(e) => {
                  e.preventDefault()
                  confirm.mutate(c.id)
                }}
              >
                <p>{t('notifications:channels.confirmExplain')}</p>
                <input
                  aria-label={t('notifications:channels.confirmCode')}
                  inputMode="numeric"
                  pattern="[0-9]{6}"
                  maxLength={6}
                  required
                  autoComplete="one-time-code"
                  value={codes[c.id] ?? ''}
                  onChange={(e) => setCodes({ ...codes, [c.id]: e.target.value })}
                />{' '}
                <button type="submit" disabled={confirm.isPending}>{t('notifications:channels.confirm')}</button>
              </form>
            )}
            <button type="button" onClick={() => remove.mutate(c.id)}>{t('notifications:channels.remove')}</button>
          </li>
        ))}
      </ul>

      {platform.data?.can_change && (
        <div className="notice">
          <label>
            <input
              type="checkbox"
              checked={emailOn}
              disabled={switchEmail.isPending}
              onChange={(e) => switchEmail.mutate(e.target.checked)}
            />{' '}
            {t('notifications:platform.email')}
          </label>
          <p>{t('notifications:platform.explain')}</p>
          {switchEmail.isError && (
            <p role="alert" className="error">{errorMessage(t, switchEmail.error)}</p>
          )}
        </div>
      )}
      {!emailOn && <p>{t('notifications:platform.off')}</p>}

      <h3>{t('notifications:channels.add')}</h3>
      <form className="form" onSubmit={submit}>
        <label htmlFor="ch-kind">{t('notifications:channels.kind')}</label>
        <select id="ch-kind" value={kind} onChange={(e) => setKind(e.target.value as (typeof KINDS)[number])}>
          {kinds.map((k) => <option key={k} value={k}>{t(`notifications:kinds.${k}`)}</option>)}
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
        ) : kind === 'email' ? (
          <>
            <label htmlFor="ch-host">{t('notifications:channels.mailHost')}</label>
            <input id="ch-host" required value={mail.host} onChange={(e) => setMail({ ...mail, host: e.target.value })} />
            <label htmlFor="ch-port">{t('notifications:channels.mailPort')}</label>
            <input id="ch-port" type="number" min={1} max={65535} required value={mail.port} onChange={(e) => setMail({ ...mail, port: e.target.value })} />
            <label htmlFor="ch-security">{t('notifications:channels.mailSecurity')}</label>
            <select id="ch-security" value={mail.security} onChange={(e) => setMail({ ...mail, security: e.target.value })}>
              {['starttls', 'ssl', 'none'].map((m) => <option key={m} value={m}>{t(`notifications:security.${m}`)}</option>)}
            </select>
            <label htmlFor="ch-user">{t('notifications:channels.mailUser')}</label>
            <input id="ch-user" autoComplete="off" value={mail.username} onChange={(e) => setMail({ ...mail, username: e.target.value })} />
            <label htmlFor="ch-pass">{t('notifications:channels.mailPassword')}</label>
            <input id="ch-pass" type="password" autoComplete="off" value={mail.password} onChange={(e) => setMail({ ...mail, password: e.target.value })} />
            <label htmlFor="ch-from">{t('notifications:channels.mailFrom')}</label>
            <input id="ch-from" type="email" required value={mail.sender} onChange={(e) => setMail({ ...mail, sender: e.target.value })} />
            <label htmlFor="ch-to">{t('notifications:channels.mailTo')}</label>
            <input id="ch-to" type="email" required value={mail.recipient} onChange={(e) => setMail({ ...mail, recipient: e.target.value })} />
            {mail.security === 'none' && <p className="notice">{t('notifications:channels.mailNoTls')}</p>}
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
