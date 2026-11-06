import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import type { User } from '../../core/auth'
import { stillValid } from '../../core/time'
import { withDependencies, withoutDependents } from './rights'
import type { RightInfo } from './rights'
import { Problem } from '../../components/Problem'
import { useConfirm } from '../../components/Confirm'
import { Tip } from '../../components/Tip'

interface ApiKey {
  id: string
  name: string
  rights: string[]
  read_only: boolean
  expires_at: string | null
  last_used_at: string | null
  revoked: boolean
  created_at: string
}

export function ApiKeys({ me }: { me: User }) {
  const { t } = useTranslation(['account', 'errors'])
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const keys = useQuery({ queryKey: ['apikeys'], queryFn: () => api.get<ApiKey[]>('/apikeys') })
  const catalogue = useQuery({ queryKey: ['rights'], queryFn: () => api.get<RightInfo[]>('/rights') })

  const [name, setName] = useState('')
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [readOnly, setReadOnly] = useState(false)
  const [expires, setExpires] = useState('')
  // The key is shown once, kept only in this component's memory.
  const [created, setCreated] = useState<string | null>(null)

  // A key can only carry rights the account holds now and that are not sensitive.
  const held = new Set(
    me.grants.filter((g) => stillValid(g.expires_at)).map((g) => g.right),
  )
  const offered = (catalogue.data ?? []).filter((r) => held.has(r.id) && !r.sensitive)

  const create = useMutation({
    mutationFn: () =>
      api.post<{ key: string }>('/apikeys', {
        name,
        rights: [...chosen],
        read_only: readOnly,
        expires_at: expires ? new Date(expires).toISOString() : null,
      }),
    onSuccess: async (data) => {
      setCreated(data.key)
      setName('')
      setChosen(new Set())
      setReadOnly(false)
      setExpires('')
      await queryClient.invalidateQueries({ queryKey: ['apikeys'] })
    },
  })
  const revoke = useMutation({
    mutationFn: (id: string) => api.del(`/apikeys/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['apikeys'] }),
  })

  function toggle(right: string) {
    const list = catalogue.data ?? []
    setChosen(chosen.has(right) ? withoutDependents(chosen, list, right) : withDependencies(chosen, list, right))
  }
  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }
  const failed = keys.error ?? create.error ?? revoke.error

  return (
    <section>
      <h2>{t('account:keys.title')}</h2>
      <p className="hint">{t('account:keys.explain')}</p>
      {failed != null && (
        <Problem error={failed} />
      )}

      {created && (
        <div className="notice">
          <h3>{t('account:keys.createdTitle')}</h3>
          <p>{t('account:keys.createdExplain')}</p>
          <pre>{created}</pre>
          <button type="button" className="primary" onClick={() => void navigator.clipboard?.writeText(created)}>
            {t('account:keys.copy')}
          </button>{' '}
          <button type="button" onClick={() => setCreated(null)}>
            {t('account:keys.saved')}
          </button>
        </div>
      )}

      {keys.data?.length === 0 && <p className="empty">{t('account:keys.empty')}</p>}
      <ul className="cards">
        {keys.data?.map((key) => (
          <li key={key.id}>
            <strong>{key.name}</strong>
            {key.revoked && <span className="badge off">{t('account:keys.revoked')}</span>}
            {key.read_only && <span className="badge">{t('account:keys.readOnly')}</span>}
            <p>{key.rights.join(', ')}</p>
            <p className="hint">
              {key.last_used_at
                ? t('account:keys.lastUsed', { date: new Date(key.last_used_at).toLocaleString() })
                : t('account:keys.neverUsed')}
              {key.expires_at && <> · {t('account:keys.expires', { date: new Date(key.expires_at).toLocaleDateString() })}</>}
            </p>
            {!key.revoked && (
              <button
                type="button"
                className="danger-button"
                title={t('account:keys.revokeTip')}
                onClick={async () => {
                  if (await confirm(t('account:keys.revokeConfirm', { name: key.name }))) revoke.mutate(key.id)
                }}
              >
                {t('account:keys.revoke')}
              </button>
            )}
          </li>
        ))}
      </ul>

      <h3>{t('account:keys.newTitle')}</h3>
      <form className="form" onSubmit={submit}>
        <label htmlFor="key-name">{t('account:keys.name')}</label>
        <input id="key-name" required placeholder={t('account:keys.namePlaceholder')} maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
        <fieldset className="legend">
          <legend>{t('account:keys.rights')}</legend>
          {offered.length === 0 && <p className="hint">{t('account:keys.noRights')}</p>}
          {offered.map((right) => (
            <label key={right.id} className="legend-item">
              <input type="checkbox" checked={chosen.has(right.id)} onChange={() => toggle(right.id)} />
              <code>{right.id}</code>
            </label>
          ))}
        </fieldset>
        <p className="hint">{t('account:keys.sensitiveNote')}</p>
        <label className="legend-item">
          <input type="checkbox" checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)} />
          {t('account:keys.readOnlyOption')}
          <Tip text={t('account:keys.readOnlyTip')} />
        </label>
        <label htmlFor="key-expires">
          {t('account:keys.expiresOption')}
          <Tip text={t('account:keys.expiresTip')} />
        </label>
        <input id="key-expires" type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} />
        <button type="submit" className="primary" disabled={create.isPending || chosen.size === 0}>
          {t('account:keys.create')}
        </button>
      </form>
    </section>
  )
}
