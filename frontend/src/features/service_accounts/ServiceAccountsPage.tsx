import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import type { RightInfo } from '../account/rights'
import { Problem } from '../../components/Problem'
import { useConfirm } from '../../components/Confirm'
import { Tip } from '../../components/Tip'

interface Account {
  id: string
  username: string
  is_active: boolean
  follow_owner: boolean
  description: string
}

interface Key {
  id: string
  name: string
  rights: string[]
  revoked: boolean
  read_only: boolean
  last_used_at: string | null
}

// Identities for tools (CI, monitoring…): no password, only keys. Each one is run by the person
// who made it, and by default holds the rights that person holds.
export function ServiceAccountsPage() {
  const { t } = useTranslation(['serviceAccounts', 'errors'])
  const queryClient = useQueryClient()
  const ask = useConfirm()
  const accounts = useQuery({
    queryKey: ['service-accounts'],
    queryFn: () => api.get<Account[]>('/service-accounts'),
  })
  const [form, setForm] = useState({ name: '', description: '' })
  const [open, setOpen] = useState<string | null>(null)
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['service-accounts'] })
  const create = useMutation({
    mutationFn: () => api.post('/service-accounts', { name: form.name, description: form.description, follow_owner: true }),
    onSuccess: () => {
      setForm({ name: '', description: '' })
      return refresh()
    },
  })
  const close = useMutation({ mutationFn: (id: string) => api.del(`/service-accounts/${id}`), onSuccess: refresh })

  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }
  const failed = accounts.error ?? create.error ?? close.error

  return (
    <>
      <header className="page-head">
        <div>
          <h1>{t('serviceAccounts:title')}</h1>
          <p className="hint">{t('serviceAccounts:explain')}</p>
        </div>
      </header>
      {failed != null && (
        <Problem error={failed} />
      )}
      <form onSubmit={submit} aria-label={t('serviceAccounts:new')} className="panel form-grid">
        <label>
          {t('serviceAccounts:name')}
          <input value={form.name} required placeholder="ci-deploy" autoComplete="off" pattern="[a-z0-9][a-z0-9\-]{0,30}" onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label>
          {t('serviceAccounts:description')}
          <input value={form.description} maxLength={200} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </label>
        <div className="wide actions">
          <button type="submit" className="primary" disabled={create.isPending}>
            {t('serviceAccounts:create')}
          </button>
        </div>
      </form>
      {accounts.isPending && <p className="loading">{t('loading')}</p>}
      {accounts.data?.length === 0 && <p className="empty">{t('serviceAccounts:none')}</p>}
      <ul className="links">
        {accounts.data?.map((a) => (
          <li key={a.id}>
            <strong>{a.username}</strong>{' '}
            <span className={`badge ${a.is_active ? 'ok' : 'off'}`}>
              {t(a.is_active ? 'serviceAccounts:active' : 'serviceAccounts:closed')}
            </span>{' '}
            <small>{a.follow_owner ? t('serviceAccounts:follows') : t('serviceAccounts:fixed')}</small>
            {a.description && <p>{a.description}</p>}
            {a.is_active && (
              <div className="actions">
                <button type="button" onClick={() => setOpen(open === a.id ? null : a.id)}>
                  {t('serviceAccounts:keys')}
                </button>
                <button
                  type="button"
                  className="danger-button"
                  title={t('serviceAccounts:closeTip')}
                  onClick={async () => {
                    if (await ask(t('serviceAccounts:closeConfirm', { name: a.username }))) close.mutate(a.id)
                  }}
                >
                  {t('serviceAccounts:close')}
                </button>
              </div>
            )}
            {open === a.id && <Keys accountId={a.id} />}
          </li>
        ))}
      </ul>
    </>
  )
}

function Keys({ accountId }: { accountId: string }) {
  const { t } = useTranslation(['serviceAccounts', 'errors'])
  const queryClient = useQueryClient()
  const ask = useConfirm()
  const base = `/service-accounts/${accountId}/keys`
  const keys = useQuery({ queryKey: ['service-account-keys', accountId], queryFn: () => api.get<Key[]>(base) })
  const catalogue = useQuery({ queryKey: ['rights'], queryFn: () => api.get<RightInfo[]>('/rights') })
  const [name, setName] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [shown, setShown] = useState<string | null>(null)
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['service-account-keys', accountId] })
  const create = useMutation({
    mutationFn: () => api.post<{ key: string }>(base, { name, rights: picked }),
    onSuccess: (made) => {
      setShown(made.key)
      setName('')
      setPicked([])
      return refresh()
    },
  })
  const revoke = useMutation({ mutationFn: (id: string) => api.del(`${base}/${id}`), onSuccess: refresh })
  const offered = (catalogue.data ?? []).filter((r) => !r.sensitive)
  const failed = keys.error ?? create.error ?? revoke.error

  return (
    <div>
      {failed != null && (
        <Problem error={failed} />
      )}
      {shown && (
        <div role="status" className="notice">
          <p>
            <strong>{t('serviceAccounts:shownOnce')}</strong>
          </p>
          <input readOnly aria-label={t('serviceAccounts:keyValue')} value={shown} onFocus={(e) => e.currentTarget.select()} />
          <p className="hint">{t('serviceAccounts:oauthHint')}</p>
          <button type="button" className="primary" onClick={() => setShown(null)}>
            {t('serviceAccounts:copied')}
          </button>
        </div>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          create.mutate()
        }}
        aria-label={t('serviceAccounts:newKey')}
      >
        <label>
          {t('serviceAccounts:keyName')}
          <input value={name} required maxLength={100} onChange={(e) => setName(e.target.value)} />
        </label>
        <fieldset>
          <legend>
            {t('serviceAccounts:rights')}
            <Tip text={t('serviceAccounts:rightsTip')} />
          </legend>
          {offered.map((r) => (
            <label key={r.id}>
              <input
                type="checkbox"
                checked={picked.includes(r.id)}
                onChange={(e) => setPicked(e.target.checked ? [...picked, r.id] : picked.filter((x) => x !== r.id))}
              />
              <code>{r.id}</code>
            </label>
          ))}
        </fieldset>
        <button type="submit" className="primary" disabled={create.isPending || picked.length === 0}>
          {t('serviceAccounts:createKey')}
        </button>
      </form>
      <ul className="links">
        {keys.data?.map((k) => (
          <li key={k.id}>
            {k.name} <small>({k.rights.join(', ')})</small>{' '}
            {k.revoked ? (
              <span className="badge off">{t('serviceAccounts:revoked')}</span>
            ) : (
              <button
                type="button"
                className="danger-button"
                onClick={async () => {
                  if (await ask(t('serviceAccounts:revokeConfirm', { name: k.name }))) revoke.mutate(k.id)
                }}
              >
                {t('serviceAccounts:revoke')}
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
