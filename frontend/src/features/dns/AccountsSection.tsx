import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'
import { useConfirm } from '../../components/Confirm'

export interface Provider {
  name: string
  capabilities: string[]
  fields: string[]
}

interface Account {
  id: string
  provider: string
  name: string
}

// Fields that hold a key or token are typed as passwords: never shown on screen.
const isSecret = (field: string) => /key|secret|token|password/i.test(field)

export function AccountsSection({ providers }: { providers: Provider[] }) {
  const { t } = useTranslation(['dns', 'errors'])
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const accounts = useQuery({ queryKey: ['dns-accounts'], queryFn: () => api.get<Account[]>('/dns/accounts') })
  // Local DNS has no account; the other providers do.
  const withAccount = providers.filter((p) => p.fields.length > 0)
  const [provider, setProvider] = useState(withAccount[0]?.name ?? '')
  const [name, setName] = useState('')
  const [values, setValues] = useState<Record<string, string>>({})

  const current = withAccount.find((p) => p.name === provider)
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['dns-accounts'] })
  const add = useMutation({
    mutationFn: () => api.post('/dns/accounts', { provider, name, credentials: values }),
    onSuccess: () => {
      setName('')
      setValues({}) // the keys are not kept in the page once sent
      return refresh()
    },
  })
  const remove = useMutation({ mutationFn: (id: string) => api.del(`/dns/accounts/${id}`), onSuccess: refresh })
  const failed = accounts.error ?? add.error ?? remove.error

  function submit(event: FormEvent) {
    event.preventDefault()
    add.mutate()
  }

  return (
    <section>
      <h2>{t('dns:accounts.title')}</h2>
      <p>{t('dns:accounts.explain')}</p>
      {failed != null && <Problem error={failed} />}
      {accounts.data?.length === 0 && <p className="empty">{t('dns:accounts.empty')}</p>}
      <ul className="cards">
        {accounts.data?.map((a) => (
          <li key={a.id}>
            <strong>{a.name}</strong> <span className="badge">{t(`dns:providers.${a.provider}`, { defaultValue: a.provider })}</span>
            <p>
              <button
                type="button"
                className="danger-button"
                onClick={async () => {
                  if (await confirm(t('dns:accounts.removeConfirm', { name: a.name }))) remove.mutate(a.id)
                }}
              >
                {t('dns:accounts.remove')}
              </button>
            </p>
          </li>
        ))}
      </ul>
      {withAccount.length > 0 && (
        <form className="form" onSubmit={submit}>
          <h3>{t('dns:accounts.add')}</h3>
          <label htmlFor="dns-provider">{t('dns:accounts.provider')}</label>
          <select id="dns-provider" value={provider} onChange={(e) => { setProvider(e.target.value); setValues({}) }}>
            {withAccount.map((p) => <option key={p.name} value={p.name}>{t(`dns:providers.${p.name}`, { defaultValue: p.name })}</option>)}
          </select>
          <label htmlFor="dns-account-name">{t('dns:accounts.name')}</label>
          <input id="dns-account-name" required maxLength={60} value={name} onChange={(e) => setName(e.target.value)} />
          {current?.fields.map((field) => (
            <div key={field} className="form">
              <label htmlFor={`dns-field-${field}`}>{t(`dns:fields.${field}`, { defaultValue: field })}</label>
              {field === 'endpoint' ? (
                <select id={`dns-field-${field}`} value={values[field] ?? 'ovh-eu'} onChange={(e) => setValues({ ...values, [field]: e.target.value })}>
                  {['ovh-eu', 'ovh-ca', 'ovh-us'].map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              ) : (
                <input
                  id={`dns-field-${field}`}
                  required
                  autoComplete="off"
                  type={isSecret(field) ? 'password' : 'text'}
                  value={values[field] ?? ''}
                  onChange={(e) => setValues({ ...values, [field]: e.target.value })}
                />
              )}
            </div>
          ))}
          <p className="hint">{t('dns:accounts.private')}</p>
          <button type="submit" className="primary" disabled={add.isPending}>{t('dns:accounts.create')}</button>
        </form>
      )}
    </section>
  )
}
